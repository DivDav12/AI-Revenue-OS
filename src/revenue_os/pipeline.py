"""One-cycle agent pipeline for a qualified opportunity candidate.

Chains the relevant roster agents so each one receives its predecessors'
REAL output. Order:

  opportunity_finder -> product_researcher -> competitor_analyzer
  -> supplier_finder -> copywriter -> content_creator -> designer
  -> store_builder -> quality_control -> HUMAN GATE

Deterministic agents run for real through `agent_runner.run_agent`
(zero cost, zero network). The three LLM-only agents (research,
analyze_competition, write_copy) are enrichment: the pipeline consumes an
existing real output from `agent_outputs.json` if one is present,
otherwise it records the step as `skipped` and continues. It NEVER
triggers an API call and NEVER fabricates a result.

  hand-off data  : data/agent_outputs.json  (via run_agent, keyed by capability)
  run state      : data/pipeline.json       (per candidate, restart-safe)

The pipeline publishes nothing, sends nothing, spends nothing. Success =
status `prepared` plus a human-gate summary. store_builder / developer /
automation_engineer / ads_manager / campaign_optimizer / budget_allocator
stay human-gated - the pipeline runs store_builder only to produce its
draft spec; a human does the real build.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from . import agent_runner, roster
from .store import CandidateStore, now_iso

logger = logging.getLogger(__name__)

_QUALIFIED_STATUSES = ("validated", "launched", "earning")
_LLM_ONLY = {"research", "analyze_competition", "write_copy"}
_GOAL_FLAG = {"research": "--research llm", "analyze_competition": "--competition llm",
              "write_copy": "--copywriter llm"}

# (capability, required, needs) - order is the pipeline
_STEPS: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    ("select", True, ()),
    ("research", False, ("select",)),
    ("analyze_competition", False, ("select",)),
    ("find_suppliers", True, ("select",)),
    ("write_copy", False, ("select",)),
    ("package_deliverable", True, ("select",)),
    ("design_assets", True, ("select",)),
    ("build_store", True, ("package_deliverable", "design_assets")),
    ("quality_check", True, ("package_deliverable", "design_assets", "build_store")),
)
_STEP_CAPS = [c for c, _, _ in _STEPS]
STEP_ORDER = tuple(_STEP_CAPS)   # public: dashboard renders steps in this order

_HUMAN_GATED_NEXT = (
    "store_builder - review the page spec, then build/deploy the real store",
    "developer - implement any technical components",
    "automation_engineer - wire the workflow",
    "ads_manager - draft campaigns (a human launches and funds them)",
    "campaign_optimizer - once real campaign metrics exist",
    "budget_allocator - once a human approves a budget",
)

_SUMMARY_DROP = {"landing_html", "readme"}


def _summary(output: dict) -> dict:
    """A compact, real slice of an agent's output for the run log -
    scalars and small collections only, big blobs dropped."""
    out: dict = {}
    for k, v in (output or {}).items():
        if k in _SUMMARY_DROP:
            out[k] = f"<{len(str(v))} chars>"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, dict)):
            out[k] = f"{len(v)} item(s)"
    return out


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

class PipelineState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {"candidate": None, "status": "idle", "started_at": None,
                "updated_at": None, "steps": {}, "human_gate": None, "error": None}

    @classmethod
    def load(cls, path: str | Path) -> "PipelineState":
        s = cls(path)
        if s.path.exists():
            try:
                raw = json.loads(s.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    s.data = {**s._blank(), **raw}
            except json.JSONDecodeError:
                logger.warning("corrupt pipeline state - starting fresh")
        return s

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.data, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- mutations ---------------------------------------------------
    def reset(self, candidate: str) -> None:
        self.data = self._blank()
        self.data["candidate"] = candidate
        self.data["status"] = "running"
        self.data["started_at"] = now_iso()

    def mark(self, cap: str, status: str, **extra) -> None:
        entry = {"status": status, "ts": now_iso()}
        entry.update({k: v for k, v in extra.items() if v not in (None, "", {}, [])})
        self.data["steps"][cap] = entry

    def fail(self, cap: str, reason: str) -> None:
        self.mark(cap, "failed", reason=reason)
        self.data["status"] = "failed"
        self.data["error"] = f"{cap}: {reason}"

    def block(self, qc_output: dict) -> None:
        self.data["status"] = "blocked"
        self.data["human_gate"] = {
            "reason": "Quality Control returned qc_status=block - pipeline stopped",
            "blocking_issues": qc_output.get("blocking_issues", []),
            "failed_checks": qc_output.get("failed_checks", []),
        }

    def prepare(self, qc_output: dict) -> None:
        self.data["status"] = "prepared"
        self.data["human_gate"] = {
            "reason": "pipeline complete - QC passed; awaiting a human decision",
            "qc_status": qc_output.get("qc_status"),
            "warnings": qc_output.get("warnings", []),
            "human_gated_next": list(_HUMAN_GATED_NEXT),
            "not_done": ["nothing published", "no ads launched", "no money spent",
                         "no messages / emails sent"],
        }

    # --- view ------------------------------------------------------
    def report(self) -> dict:
        steps = [
            {"step": cap, "agent": (roster.by_capability(cap).name
                                    if roster.by_capability(cap) else cap),
             **self.data["steps"].get(cap, {"status": "pending"})}
            for cap in _STEP_CAPS
        ]
        return {
            "candidate": self.data.get("candidate"),
            "status": self.data.get("status"),
            "started_at": self.data.get("started_at"),
            "updated_at": self.data.get("updated_at"),
            "steps": steps,
            "human_gate": self.data.get("human_gate"),
            "error": self.data.get("error"),
        }


def _state(data_dir) -> PipelineState:
    return PipelineState.load(Path(data_dir) / "pipeline.json")


# ---------------------------------------------------------------------------
# payloads - each agent gets the real output of its predecessors
# ---------------------------------------------------------------------------

def _copy_draft(outs: dict) -> dict:
    wc = outs.get("write_copy") or {}
    return dict(wc.get("launch_draft") or {})


def _payload(capability: str, cand, outs: dict) -> dict:
    opp = {"name": cand.name, "title": cand.description, "description": cand.description}
    offer = dict(cand.offer or {})
    copy = _copy_draft(outs)
    if capability == "select":
        return {"scored": [{"name": cand.name, "total": float(cand.total or 0.0)}],
                "min_score": 0.0, "shortlist_n": 1}
    if capability == "find_suppliers":
        return {"opportunity": opp, "target_market": offer.get("positioning", ""),
                "known_suppliers": list(offer.get("suppliers", []))}
    if capability == "package_deliverable":
        return {"candidate": {"name": cand.name, "description": cand.description},
                "offer": offer, "draft": copy, "plan": dict(cand.plan or {})}
    if capability == "design_assets":
        return {"opportunity": opp, "offer": offer, "copy": copy}
    if capability == "build_store":
        return {"opportunity": opp, "offer": offer, "copy": copy,
                "design": dict(outs.get("design_assets") or {})}
    if capability == "quality_check":
        pkg = outs.get("package_deliverable") or {}
        return {"offer": offer, "copy": copy,
                "landing_page": pkg.get("landing_html", ""),
                "launch_plan": dict(cand.plan or {}),
                "agent_results": [{"output": v} for v in outs.values()
                                  if isinstance(v, dict)],
                "expected_business_email": ""}
    return {}


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------

def run_pipeline(data_dir, candidate_name: str, *, restart: bool = False) -> dict:
    """Advance the qualified candidate through the agent chain as far as it
    can without a human. Idempotent and restart-safe: a step already
    `ok`/`skipped` with its output still on disk is not re-run."""
    data_dir = Path(data_dir)
    store = CandidateStore.load(data_dir / "candidates.json")
    cand = store.get(candidate_name)
    if cand is None:
        raise ValueError(f"unknown candidate: {candidate_name!r}")

    st = _state(data_dir)
    if restart or st.data.get("candidate") != candidate_name:
        st.reset(candidate_name)
    else:
        st.data["status"] = "running"
        st.data["error"] = None

    # --- entry gate --------------------------------------------------
    if cand.status not in _QUALIFIED_STATUSES:
        st.fail("entry", f"candidate status {cand.status!r} is not qualified "
                f"(need one of {list(_QUALIFIED_STATUSES)})")
        st.save()
        return st.report()
    if not cand.offer:
        st.fail("entry", "candidate has no offer - run `prepare-launch` first")
        st.save()
        return st.report()
    st.data["steps"].pop("entry", None)

    outs: dict[str, dict] = {}

    for cap, required, needs in _STEPS:
        spec = roster.by_capability(cap)
        prev = st.data["steps"].get(cap, {})
        existing = agent_runner.last_output(data_dir, cap)

        # --- idempotent / restart-safe skip --------------------------
        if prev.get("status") == "ok" and existing is not None:
            outs[cap] = existing
            continue
        if prev.get("status") == "skipped" and cap in _LLM_ONLY:
            if existing is not None:                       # a real output appeared since
                outs[cap] = existing
                st.mark(cap, "ok", note="reused an existing real output",
                        summary=_summary(existing))
            continue

        # --- dependency checks (before every run) --------------------
        unmet = roster.unmet_dependencies(spec) if spec else ("<no roster spec>",)
        if unmet:
            st.fail(cap, f"roster dependencies not live: {list(unmet)}")
            st.save()
            return st.report()
        failed_pred = [n for n in needs
                       if st.data["steps"].get(n, {}).get("status") == "failed"]
        if failed_pred:
            st.fail(cap, f"required predecessor failed: {failed_pred}")
            st.save()
            return st.report()
        incomplete_pred = [n for n in needs
                           if st.data["steps"].get(n, {}).get("status")
                           not in ("ok", "skipped")]
        if incomplete_pred:
            st.fail(cap, f"predecessor(s) not complete: {incomplete_pred}")
            st.save()
            return st.report()

        # --- LLM-only steps: consume real output or skip -------------
        if cap in _LLM_ONLY:
            if existing is not None:
                outs[cap] = existing
                st.mark(cap, "ok", note="consumed an existing real output",
                        summary=_summary(existing))
            else:
                st.mark(cap, "skipped",
                        reason=(f"{spec.name} is LLM-only - the pipeline does not "
                                f"call it (no cost / no network). Run it separately "
                                f"with `agent-goal {_GOAL_FLAG[cap]}` + budget, then "
                                f"re-run the pipeline to pick up its output."))
            st.save()
            continue

        # --- deterministic step: run for real -----------------------
        try:
            res = agent_runner.run_agent(
                data_dir, cap, _payload(cap, cand, outs),
                objective=f"pipeline: {spec.name} for {cand.name}")
        except Exception as exc:
            st.fail(cap, f"dispatch error: {exc}")
            st.save()
            return st.report()
        if res.status != "ok":
            st.fail(cap, res.error or "agent returned an error")
            st.save()
            return st.report()

        out = agent_runner.last_output(data_dir, cap) or dict(res.output)
        outs[cap] = out

        if cap == "select" and cand.name not in (out.get("kept") or []):
            st.fail("select", "candidate did not pass Opportunity Finder (min_score)")
            st.save()
            return st.report()

        if cap == "quality_check" and out.get("qc_status") == "block":
            st.mark(cap, "ok", summary=_summary(out))
            st.block(out)
            st.save()
            return st.report()

        st.mark(cap, "ok", summary=_summary(out))
        st.save()

    st.prepare(outs.get("quality_check") or {})
    st.save()
    return st.report()


def pipeline_status(data_dir, candidate_name: str | None = None) -> dict:
    st = _state(data_dir)
    if candidate_name and st.data.get("candidate") not in (None, candidate_name):
        return {"candidate": candidate_name, "status": "no run",
                "note": f"last pipeline run was for {st.data.get('candidate')!r}"}
    return st.report()

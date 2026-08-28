import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.dashboard import render_html
from revenue_os.report import pipeline_report
from revenue_os.revenue import RevenueLedger, mark_launched, record_payment
from revenue_os.spend import SpendLedger
from revenue_os.store import Candidate, CandidateStore

_FIXED_TS = "2026-08-28T00:00:00+00:00"


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _report(store, d):
    return pipeline_report(
        store,
        RevenueLedger(Path(d) / "revenue.json"),
        SpendLedger(Path(d) / "spend.json"),
    )


class RenderHtmlTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.store = CandidateStore(Path(self.d) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_empty_store_renders(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("No candidates.", html)
        self.assertIn("No agent activity yet.", html)
        self.assertIn("No agent decisions recorded.", html)
        self.assertIn("Nothing awaiting a human.", html)
        self.assertIn(_FIXED_TS, html)

    def test_agents_and_decisions_sections(self):
        agent_log = [
            {"ts": "t0", "action": "session_start", "reason": "interval=0s"},
            {"ts": "t1", "action": "discover", "reason": "cold start"},
            {"ts": "t2", "action": "research", "reason": "3 not researched"},
        ]
        spend = [
            {"ts": "t2", "activity": "research", "model": "claude-sonnet-5",
             "api_calls": 3, "cost_usd": 0.01, "cache_hits": 0, "cache_misses": 3},
        ]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=agent_log, spend_entries=spend,
        )
        self.assertIn("<h2>Agents</h2>", html)
        self.assertIn("operator (CEO)", html)
        self.assertIn("researcher", html)
        self.assertIn("research &mdash; 3 not researched", html)
        self.assertIn("class='marker'>session_start", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("https://", html)

    def test_session_line_running_and_ended(self):
        base = _report(self.store, self.d)
        running = render_html(base, _FIXED_TS, session={
            "ticks": 2, "cycles": 5, "last_tick_at": "t9", "ended_at": None,
        })
        self.assertIn("session running: 2 tick(s), 5 cycle(s)", running)
        ended = render_html(base, _FIXED_TS, session={
            "ticks": 3, "cycles": 4, "ended_at": "tX", "end_reason": "max-ticks",
        })
        self.assertIn("session ended: max-ticks after 3 tick(s)", ended)
        self.assertNotIn("session running", render_html(base, _FIXED_TS))

    def test_populated_store_shows_counts_and_names(self):
        self.store.put(Candidate(name="alpha", status="shortlisted", total=3.1, verdict="hold"))
        self.store.put(Candidate(name="beta", status="investigating", total=2.0, verdict="hold"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("alpha", html)
        self.assertIn("beta", html)
        self.assertIn("approve or reject", html)
        self.assertIn("record validation outcome", html)

    def test_no_external_resource_references(self):
        self.store.put(Candidate(name="alpha", status="discovered"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"src\s*=")
        self.assertNotIn("<script", html)

    def test_candidate_text_is_html_escaped(self):
        self.store.put(
            Candidate(name="<script>alert(1)</script>", status="discovered")
        )
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_deterministic_given_fixed_timestamp(self):
        self.store.put(Candidate(name="alpha", status="shortlisted", total=3.1))
        r = _report(self.store, self.d)
        self.assertEqual(render_html(r, _FIXED_TS), render_html(r, _FIXED_TS))

    def test_candidate_breakdown_in_details_block(self):
        from revenue_os.opportunity import CRITERIA

        breakdown = {name: 3.0 for name in CRITERIA}
        breakdown["demand"] = 1.0
        self.store.put(
            Candidate(name="alpha", status="shortlisted", total=2.75,
                      verdict="hold", breakdown=breakdown)
        )
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("<details>", html)
        self.assertIn("<summary>", html)
        for name in CRITERIA:
            self.assertIn(name, html)
        self.assertNotIn("<script", html)
        self.assertNotIn("https://", html)

    def test_candidate_with_empty_breakdown_renders(self):
        self.store.put(Candidate(name="alpha", status="discovered"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("No score breakdown.", html)
        self.assertIn("No rationale.", html)
        self.assertIn("[keyword]", html)

    def test_candidate_shows_llm_source_and_rationale(self):
        self.store.put(Candidate(
            name="alpha", status="shortlisted", total=3.0, verdict="hold",
            estimate_source="llm", rationale="niche but real demand",
        ))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("[llm]", html)
        self.assertIn("niche but real demand", html)
        self.assertNotIn("<script", html)

    def test_command_center_shell(self):
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "t", "action": "discover", "reason": "x"}],
        )
        self.assertIn("command center", html)
        self.assertIn("class='shell'", html)          # sidebar + main grid
        self.assertIn("class='rail'", html)           # left nav rail
        self.assertIn("class='topbar'", html)         # top metric cards
        self.assertIn("class='agrid'", html)          # agent workstation grid
        self.assertIn("--bg:#070b14", html)           # dark navy theme
        self.assertIn("prefers-color-scheme: light", html)
        self.assertIn("<svg viewBox", html)           # inline SVG avatars/icons
        # sidebar nav items
        for label in ("Dashboard", "Agents", "Tasks", "Opportunities",
                      "Finances", "Automations", "Logs", "Settings"):
            self.assertIn(label, html)
        # top-bar metrics
        for k in ("Main goal", "Session", "Active agents", "Awaiting you",
                  "LLM spend", "Revenue"):
            self.assertIn(k, html)

    def test_command_center_visual_treatment(self):
        # HUD backdrop, glow markers, brand subtitle - pure CSS, no data
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "t", "action": "discover", "reason": "x"}],
        )
        self.assertIn("background-attachment:fixed", html)   # HUD grid backdrop
        self.assertIn("h2::before", html)                    # glowing section marker
        self.assertIn("multi-agent runtime", html)           # truthful brand subtitle
        self.assertIn("@keyframes scan", html)               # active-card scanline
        self.assertIn("@keyframes pulse", html)
        # no external resources sneaked in via the new styling
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"src\s*=")
        self.assertNotIn("url(", html)                       # no external/asset urls

    def test_agent_card_carries_status_state_class(self):
        base = _report(self.store, self.d)
        spend = [{"ts": "t", "activity": "research", "model": "m", "api_calls": 1,
                  "cost_usd": 0.01, "cache_hits": 0, "cache_misses": 1}]
        log = [{"ts": "t", "action": "research", "reason": "go"}]
        running = render_html(base, _FIXED_TS, agent_log=log, spend_entries=spend,
                              session={"ticks": 1, "cycles": 1, "ended_at": None},
                              goal={"research": "llm"})
        self.assertIn("class='acard cs-working'", running)
        idle = render_html(base, _FIXED_TS, agent_log=log)
        self.assertIn("cs-idle", idle)
        # the state class must not fabricate progress inside the card
        card = idle.split("class='acard")[1].split("</div></div>")[0]
        self.assertNotIn("class='progress'", card)

    def test_visual_pass_keeps_deterministic_output(self):
        self.store.put(Candidate(name="alpha", status="shortlisted", total=3.1))
        r = _report(self.store, self.d)
        log = [{"ts": "t", "action": "discover", "reason": "x"}]
        self.assertEqual(
            render_html(r, _FIXED_TS, agent_log=log),
            render_html(r, _FIXED_TS, agent_log=log),
        )

    def test_agent_cards_reflect_goal_modes(self):
        base = _report(self.store, self.d)
        log = [{"ts": "t", "action": "discover", "reason": "cold start"}]
        # deterministic goal -> planner/offer/decision show "deterministic",
        # research "disabled"
        html = render_html(base, _FIXED_TS, agent_log=log, goal={
            "evaluator": "keyword", "research": "off", "planner": "template",
            "proposer": "template", "decision_policy": "rules", "sources": ["static"],
        })
        self.assertIn("class='acard cs-", html)
        self.assertIn("operator (CEO)", html)
        self.assertIn("Research Agent", html)
        self.assertIn("mode: keyword", html)
        self.assertIn("st-disabled", html)       # research off
        self.assertIn("st-deterministic", html)  # template planner / rules
        self.assertIn("sources: static", html)

    def test_agent_card_working_and_active_states(self):
        base = _report(self.store, self.d)
        spend = [{"ts": "t", "activity": "research", "model": "m", "api_calls": 2,
                  "cost_usd": 0.02, "cache_hits": 1, "cache_misses": 1}]
        log = [{"ts": "t", "action": "research", "reason": "3 not researched"}]
        # session running -> "working"
        running = render_html(base, _FIXED_TS, agent_log=log, spend_entries=spend,
                              session={"ticks": 1, "cycles": 2, "ended_at": None},
                              goal={"research": "llm"})
        self.assertIn("st-working", running)
        self.assertIn("WORKING", running)
        # session ended -> the worker that ran shows "active", not "working"
        ended = render_html(base, _FIXED_TS, agent_log=log, spend_entries=spend,
                            session={"ticks": 1, "cycles": 2, "ended_at": "x",
                                     "end_reason": "max-ticks"},
                            goal={"research": "llm"})
        self.assertIn("st-active", ended)
        self.assertIn("$0.02", ended)
        self.assertIn("cache <b>1/2</b>", ended)  # hits / (hits+misses)

    def test_agent_card_cost_ceiling_is_error_state(self):
        spend = [{"ts": "t", "activity": "evaluate", "model": "m", "api_calls": 1,
                  "cost_usd": 0.5, "cache_hits": 0, "cache_misses": 1,
                  "ceiling_hit": True}]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "t", "action": "discover", "reason": "x"}],
            spend_entries=spend, goal={"evaluator": "llm"},
        )
        self.assertIn("st-error", html)
        self.assertIn("BLOCKED", html)

    def test_no_fake_progress_bar_on_agent_cards(self):
        # an agent card must not contain a progress bar (no real per-agent metric)
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "t", "action": "discover", "reason": "x"}],
            spend_entries=[{"ts": "t", "activity": "research", "api_calls": 1,
                            "cost_usd": 0.01}],
            goal={"research": "llm"},
        )
        # progress bars only appear in the goal/budget contexts, never inside .acard
        card = html.split("class='acard")[1].split("</div></div>")[0]
        self.assertNotIn("class='progress'", card)

    def test_goal_target_progress_is_real(self):
        for i in range(2):
            self.store.put(Candidate(name=f"v{i}", status="validated"))
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           goal={"target_validated": 5})
        self.assertIn("2 / 5 validated", html)          # top bar
        self.assertIn("Validated toward goal", html)    # ROI panel progress bar

    def test_human_action_required_strip(self):
        self.store.put(Candidate(name="alpha", status="shortlisted"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("Human action required", html)
        self.assertIn("approve or reject", html)

    def test_roi_table_appears_after_payment(self):
        self.store.put(Candidate(name="alpha", status="validated"))
        mark_launched(self.store, "alpha", actor="o")
        rev = RevenueLedger(Path(self.d) / "revenue.json")
        record_payment(self.store, rev, "alpha", 29.0, actor="o")
        report = pipeline_report(
            self.store, rev, SpendLedger(Path(self.d) / "spend.json")
        )
        html = render_html(report, _FIXED_TS)
        self.assertIn("29.0", html)


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_writes_default_path_and_leaves_stores_untouched(self):
        _run(["run", "--source", "static", "--data-dir", self.data])
        before = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        code, out = _run(["dashboard", "--data-dir", self.data])
        after = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        html_path = Path(self.data) / "dashboard.html"
        self.assertTrue(html_path.exists())
        self.assertIn("<!doctype html>", html_path.read_text(encoding="utf-8"))
        self.assertIn("dashboard written", out)
        self.assertEqual(before, after)

    def test_out_flag_honored_from_clean_data_dir(self):
        target = Path(self.data) / "nested" / "page.html"
        code, _ = _run(
            ["dashboard", "--data-dir", self.data, "--out", str(target)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        self.assertNotIn("https://", target.read_text(encoding="utf-8"))

    def test_dashboard_shows_operator_decisions(self):
        _run(["agent-run", "--data-dir", self.data])
        _run(["dashboard", "--data-dir", self.data])
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("operator (CEO)", html)
        self.assertIn("no discovery has run yet", html)

    def test_agent_loop_dashboard_flag(self):
        _run(["agent-loop", "--interval", "0", "--max-ticks", "2",
              "--dashboard", "--data-dir", self.data])
        html_path = Path(self.data) / "dashboard.html"
        self.assertTrue(html_path.exists())
        self.assertIn("session ended", html_path.read_text(encoding="utf-8"))

    def test_agent_loop_without_dashboard_flag(self):
        _run(["agent-loop", "--interval", "0", "--max-ticks", "1",
              "--data-dir", self.data])
        self.assertFalse((Path(self.data) / "dashboard.html").exists())


if __name__ == "__main__":
    unittest.main()

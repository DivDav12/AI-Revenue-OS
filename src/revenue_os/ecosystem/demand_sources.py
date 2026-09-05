"""Demand Sources adapter (spec: Demand-to-Revenue plan, Steps 2 + 3).

Turns a real, already-fetched `acquisition_sources.AcqRecord` into an
ecosystem `OpportunityDraft`, scored by the Step-1 Demand Quality Layer
(`demand_signal.py`), and (Step 3) exposes that as a real
`sources.OpportunitySource` (`DemandDiscoverySource` /
`build_demand_source()`) that `DiscoveryEngine`/`sources.build_source()`
can run unmodified - see `ecosystem/sources.py`'s small `build_source()`
extension and `discovery.py`'s two small, additive persistence/dedupe
extensions (module docstrings there explain exactly what changed and
why). This module still never touches `verification.py`.

Reuse, not a parallel system:
  - network fetching stays entirely in `acquisition_sources.py`
    (`HNAlgoliaSource` / `StackExchangeSource` / `LobstersSource` /
    `LemmySource`, all sharing the same
    `.search(query, limit, since_ts=) -> list[AcqRecord]` interface).
    This module never opens a socket.
  - `acquisition.canonical_url()` (already real, tested, used by the
    existing lead-gen pipeline) normalises the URL for dedup/provenance.
  - `demand_signal.build_demand_evidence()` / `score_demand_quality()`
    (Step 1) do all evidence extraction and scoring - this module adds
    no new intent/budget/audience heuristics of its own.
  - `task_signal.task_fingerprint()` (already built for the TASK
    discovery-quality layer) is reused, unmodified, for in-batch dedupe
    and repeat-signal counting - see `discover_demand_signals()`.

Untrusted content: `record.title`/`record.text` are external, untrusted
text (spec 5.7 of the Human-Fed Task Source work applies the same way
here). This module never feeds them to an LLM or renders them into a
file - it only carries them as structured `OpportunityDraft` fields. A
future caller that DOES render/prompt with them must wrap them with
`llm_normalize.wrap_untrusted()`, exactly like `task_adapters.py`'s
`_render_task_solution()` already does for TASK deliverables.

No credentials, no login, no CAPTCHA, no posting, no PayPal/checkout/
delivery/execution change of any kind - this module only ever produces
`OpportunityDraft` objects in memory.
"""

from __future__ import annotations

import itertools
from typing import Protocol

from ..acquisition import canonical_url
from ..acquisition_sources import AcqRecord
from . import demand_ranking, demand_signal, model, product_intent, task_signal
from .demand_signal import DemandEvidence, DemandQualityScore
from .model import OpportunityDraft, SourceMeta

#: buyer-intent / product-demand queries - deliberately DIFFERENT from
#: acquisition.SEARCH_QUERIES (which targets "founder needs customers",
#: i.e. leads for an existing service offer). These are SEARCH TERMS used
#: only to retrieve candidate posts - they never themselves become
#: evidence or influence classification; `demand_signal.py` still scores
#: the real, independently-fetched post text on its own merits, so a
#: broader query list here cannot manufacture purchase intent (spec:
#: Demand Validation phase, "Keine Query darf künstlich Kaufabsicht
#: erzeugen"). Expanded from the Step-3 set (real-run finding: the
#: original 9 queries under-covered concrete automation/switching-cost/
#: price-sensitivity pain, per the user's own examples) - kept ADDITIVE,
#: nothing removed, so existing recall is a strict subset of the new one.
DEFAULT_QUERIES: tuple[str, ...] = (
    "is there a tool that",
    "is there a tool for",
    "is there a service that",
    "looking for a tool that",
    "looking for a tool for",
    "looking for software that",
    "does anyone know a tool for",
    "recommend a tool for",
    "what tool do you use for",
    "i would pay for a tool",
    "i would pay for a service",
    "would pay for a tool",
    "paying for a tool",
    "need a way to automate",
    "how do i automate",
    "alternative to",
    "is too expensive",
)

#: search terms for the two buy-recommendation-focused demand sources
#: ('demand-stackexchange-recs' / 'demand-lemmy-buying' - see
#: build_demand_source()). Kept SEPARATE from DEFAULT_QUERIES (not merged
#: in) so the four existing demand sources' query count/request volume
#: stays byte-for-byte unchanged - only the two new sources use this list.
#: Deliberately PRODUCT-AGNOSTIC (no product name, no product category)
#: per spec section 8 ("nicht auf ein einzelnes Produkt hartcodiert") -
#: these must work for the JBL microphone offer exactly as well as any
#: future affiliate offer in any category.
BUYING_ADVICE_QUERIES: tuple[str, ...] = (
    "what should i buy",
    "which one should i buy",
    "which one should i get",
    "should i buy",
    "should i get",
    "worth buying",
    "before you buy",
    "any recommendations for",
    "in the market for",
    "looking to buy",
    "can you recommend a",
    "what would you recommend for",
    "recommend a good",
)


class AcqSearchable(Protocol):
    """The uniform interface every acquisition_sources.py fetcher already
    implements (HNAlgoliaSource, StackExchangeSource, LobstersSource,
    LemmySource, StaticAcqSource, FileAcqSource). Anything satisfying
    this can be passed to `discover_demand_signals()` - no new fetcher
    type is introduced here."""

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        ...


class IntentFilteredSource:
    """Wraps another `AcqSearchable` and drops any record that carries no
    genuine purchase-intent or stated-problem evidence (as `demand_signal.
    classify_purchase_intent()` - the SAME, unmodified classifier used
    everywhere else - already independently decides), BEFORE the record
    ever reaches `acq_record_to_draft()`/the normal ranking process.

    Why this exists (spec: Demand Discovery expansion, "lieber 50
    hochwertige Signale als 10000 Noise-Signale"): a source whose own
    community/API is not already narrowly scoped to on-topic
    recommendation content (e.g. a general Q&A community that ALSO
    carries plain discussion, news, or meme content) can return far more
    raw records than are worth scoring. This is a PURE PRE-FILTER, not a
    new scoring/marker system - it reuses the exact same
    `classify_purchase_intent()` call every downstream draft is scored
    with, so a record that passes this filter is scored identically to
    one that reached `acq_record_to_draft()` directly (no double
    standard, no new intent vocabulary introduced here).

    A record with no title/text (HELP_REQUEST/NONE) is dropped; EXPLICIT
    or PROBLEM level survives. One failing inner `.search()` call
    propagates unchanged - the caller's existing per-query try/except
    (`discover_demand_signals`) already isolates that, exactly like every
    other source."""

    def __init__(self, inner: AcqSearchable) -> None:
        self._inner = inner

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        out: list[AcqRecord] = []
        for rec in self._inner.search(query, limit, since_ts=since_ts):
            blob = f"{rec.title} {rec.text}".strip()
            level, _ = demand_signal.classify_purchase_intent(blob)
            if level in (demand_signal.INTENT_EXPLICIT, demand_signal.INTENT_PROBLEM):
                out.append(rec)
        return out


#: additional curated Lemmy communities for consumer buy-recommendation
#: demand (spec: physical/hardware product demand, e.g. "which USB mic
#: should I buy") - kept SEPARATE from LemmySource's own default
#: tool/software-relevant set (sources.py's default `LemmySource()` /
#: 'demand-lemmy' are completely untouched). AskLemmy is a general Q&A
#: community (not single-topic like c/selfhosted), so it is only ever
#: used behind `IntentFilteredSource` above - never bare. A community
#: that does not exist (or was renamed) simply yields nothing
#: (LemmySource._resolve_community_id already fails soft for that - see
#: acquisition_sources.py) - never an error.
LEMMY_BUYING_COMMUNITIES: tuple[str, ...] = ("asklemmy", "buildapc", "hardware", "frugal")


# ---------------------------------------------------------------------------
# AcqRecord -> OpportunityDraft
# ---------------------------------------------------------------------------

def _infer_opportunity_type(evidence: DemandEvidence) -> str:
    """The ONLY classification decision this module makes, and it mirrors
    the existing pattern in `sources.HackerNewsDemandSource` (which
    already decides `opportunity_type` at draft-construction time from
    keyword evidence, not inside verification.py). This NEVER reads the
    demand SCORE (a float) - only the structural evidence fields - so a
    signal cannot become a product "just because it scores well" (spec:
    Step 3, "nicht allein deshalb ... weil es kommerziell interessant
    klingt").

    Fails closed to TYPE_OTHER (the existing, safe, unopinionated default
    every weak/ambiguous signal in this codebase already gets) unless
    there is a genuinely solid basis:
      - EXPLICIT purchase intent, and the need is not flagged
        non-productizable (hardware/medical/legal/bespoke work), OR
      - PROBLEM interest (e.g. "is there a tool that...") AND the need is
        independently, positively confirmed as digital-shaped
        (PRODUCTIZABLE_HIGH) - problem-interest ALONE, with no other
        corroborating signal, is deliberately NOT considered a "sichere
        Grundlage" and stays TYPE_OTHER.
    HELP_REQUEST/NONE, or anything flagged non-productizable, always
    stays TYPE_OTHER.

    This changes NO code in verification.py / task_signal.py - the
    resulting draft goes through the SAME, unmodified
    `verification.verify()` gates as any other TYPE_DIGITAL_PRODUCT or
    TYPE_OTHER draft, and every existing MONEY/IDENTITY/LEGAL/SAFETY gate
    downstream is completely untouched - this function only sets one
    field on an in-memory draft, it never grants or bypasses anything."""
    if evidence.productizability == demand_signal.PRODUCTIZABLE_LOW:
        return model.TYPE_OTHER
    if evidence.intent_level == demand_signal.INTENT_EXPLICIT:
        return model.TYPE_DIGITAL_PRODUCT
    if (evidence.intent_level == demand_signal.INTENT_PROBLEM
            and evidence.productizability == demand_signal.PRODUCTIZABLE_HIGH):
        return model.TYPE_DIGITAL_PRODUCT
    return model.TYPE_OTHER


def draft_provenance(evidence: DemandEvidence) -> dict:
    """FACT/ESTIMATED/UNKNOWN for every field this module adds on top of
    `demand_signal.provenance_summary()` - title/url/source are copied
    verbatim from the real record (FACT); `opportunity_type` is OUR
    classification judgement, not a fact the source stated (ESTIMATED)."""
    prov = demand_signal.provenance_summary(evidence)
    prov["title_and_url"] = demand_signal.FACT
    prov["opportunity_type"] = demand_signal.ESTIMATED
    return prov


def acq_record_to_draft(record: AcqRecord, *, repeat_signal_count: int = 0,
                        now_iso: str = "") -> OpportunityDraft:
    """The core adapter: AcqRecord -> DemandEvidence -> OpportunityDraft.
    Never invents a field - everything not directly copied or extracted
    by `demand_signal.py` stays at its honest default (0 / "" / UNKNOWN)."""
    text_blob = f"{record.title} {record.text}".strip()
    evidence = demand_signal.build_demand_evidence(
        text_blob, title=record.title, discovered_at=record.posted_at,
        now_iso=now_iso, repeat_signal_count=repeat_signal_count,
        source_type=record.source or "")

    canon = canonical_url(record.url) if record.url else ""
    ref = canon or record.url
    otype = _infer_opportunity_type(evidence)

    meta = SourceMeta(
        source=record.source or "unknown", source_type="demand_signal",
        source_url=ref, access_method=model.ACCESS_OFFICIAL_API,
        automation_allowed=True, requires_login=False, requires_human=False,
        policy_status=model.POLICY_OK)

    evidence_quotes = [record.title] if record.title else []
    if record.text:
        evidence_quotes.append(record.text[:500])

    score = demand_signal.score_demand_quality(evidence)

    # Demand Ranking Layer (spec: Decision-/Ranking-Design step) - a
    # SECOND, INDEPENDENT, advisory-only pair of scores computed from the
    # SAME `evidence` object already built above. Purely additive: does
    # not read, does not touch, and is never consulted by `score`,
    # `otype`, or anything below this line. See `demand_ranking.py`'s
    # module docstring (HARD SAFETY BOUNDARY) for the full list of things
    # this must never be wired into - this call site only ever stores the
    # result in `draft.raw` for later, purely informational, read-model
    # display (discovery.py._draft_to_opportunity copies it onward).
    buyer = demand_ranking.buyer_confidence(evidence)
    problem = demand_ranking.problem_confidence(evidence)

    # Product Intent (Demand-First Affiliate architecture, Step 1) - a
    # THIRD, INDEPENDENT, additive-only extraction from the SAME evidence
    # object. Never reads, never touches, and is never consulted by
    # `score`, `otype`, `buyer`, `problem`, or anything above this line -
    # see product_intent.py's module docstring. Runs off `record.title`
    # only (the one raw-text field this step's contract covers).
    intent = product_intent.extract_product_intent(evidence, title=record.title)

    # a stated budget only ever becomes est_pay_eur when it is EUR - no
    # invented FX conversion (same rule as ecosystem/human_fed.py).
    est_pay = (evidence.budget.amount
              if (evidence.budget.amount > 0 and not evidence.budget.is_estimate
                  and evidence.budget.currency == "EUR") else 0.0)

    draft = OpportunityDraft(
        title=record.title[:200],
        description=(record.text or record.title)[:800],
        opportunity_type=otype,
        evidence=evidence_quotes,
        source_meta=meta,
        source_id=ref,
        source_url=ref,
        discovered_at=record.posted_at,
        est_pay_eur=est_pay,
        est_time_minutes=0.0,
        demand_hint=score.total,
        payment_evidence=evidence.budget,
        raw={
            "target_customer": evidence.audience_quote,
            "author": record.author,
            "platform": record.platform,
            "query": record.query,
            "demand_evidence": evidence.to_dict(),
            "demand_quality": score.to_dict(),
            "demand_provenance": draft_provenance(evidence),
            # additive read-model fields only - see the comment above
            # this function's `score = ...` line.
            "buyer_confidence": buyer.to_dict(),
            "problem_confidence": problem.to_dict(),
            # additive only - see the `intent = ...` comment above.
            "product_intent": intent.to_dict(),
        },
    )
    # attach AFTER construction - the fingerprint is computed FROM the
    # draft (title + source), so it needs the draft to exist first.
    draft.raw["fingerprint"] = task_signal.task_fingerprint(draft)
    return draft


# ---------------------------------------------------------------------------
# batch discovery over any AcqSearchable - in-memory only, no persistence
# ---------------------------------------------------------------------------

def discover_demand_signals(source: AcqSearchable, *, queries=DEFAULT_QUERIES,
                            limit: int = 25, since_ts=None, now_iso: str = "",
                            errors: list | None = None) -> list[OpportunityDraft]:
    """Fan `queries` out to one `AcqSearchable` (a real
    acquisition_sources.py fetcher, or a fixture for tests), map every
    result through `acq_record_to_draft()`, then dedupe + count repeats
    within this batch using the existing `task_signal.task_fingerprint()`
    (source-scoped, same mechanism the TASK discovery-quality layer
    already uses - see the module docstring for why cross-source
    clustering is explicitly NOT attempted here).

    One failing query (rate limit, timeout, transient outage) never kills
    the batch - it is recorded in `errors` (if given) and the remaining
    queries still run, so a single flaky query never hides an otherwise
    healthy source's results (spec: Step 3, "robust weiterlaufen ...
    transparent melden").

    Fair ordering (spec: Demand Validation phase - "jede Query muss eine
    Chance bekommen"): results are ROUND-ROBIN interleaved across queries
    (query 1's 1st hit, query 2's 1st hit, ..., query 1's 2nd hit, ...)
    BEFORE dedupe/scoring, so a caller that truncates this function's
    output to a small global limit (`DemandDiscoverySource.discover()`
    does exactly that) can never have an early query in `queries`
    systematically crowd out a later, possibly stronger one - the
    original, purely sequential concatenation had exactly that bug (a
    real run found the single best signal in the whole Step-3 validation,
    from query "i would pay for a tool" at position 10 of 17, silently
    truncated away because queries 1-9 alone already filled the limit).
    This changes ONLY the order results are considered in - not which
    records exist, not the dedupe/fingerprint logic, and not one line of
    demand_signal.py's scoring.

    In-memory only: no OpportunityStore access, no DiscoveryEngine call
    here - `DemandDiscoverySource.discover()` below is what a real
    DiscoveryEngine run actually calls."""
    per_query_records: list[list[AcqRecord]] = []
    for query in queries:
        try:
            per_query_records.append(list(source.search(query, limit, since_ts=since_ts)))
        except Exception as exc:                # noqa: BLE001 - one bad query
            if errors is not None:               # must not kill the others
                errors.append(f"{query!r}: {exc!r}")
            per_query_records.append([])

    records: list[AcqRecord] = []
    for round_ in itertools.zip_longest(*per_query_records):
        records.extend(rec for rec in round_ if rec is not None)

    # pass 1: fingerprint every record via a throwaway draft, to count
    # same-source repeats before final scoring
    prelim = [acq_record_to_draft(r, now_iso=now_iso) for r in records]
    fp_counts: dict[str, int] = {}
    for d in prelim:
        fp = d.raw.get("fingerprint", "")
        if fp:
            fp_counts[fp] = fp_counts.get(fp, 0) + 1

    # pass 2: keep the first occurrence of each fingerprint, scored with
    # the real repeat count (excluding itself)
    seen: set[str] = set()
    out: list[OpportunityDraft] = []
    for record, draft in zip(records, prelim):
        fp = draft.raw.get("fingerprint", "")
        if fp:
            if fp in seen:
                continue
            seen.add(fp)
        repeat = max(0, fp_counts.get(fp, 1) - 1)
        out.append(acq_record_to_draft(record, repeat_signal_count=repeat,
                                       now_iso=now_iso))
    return out


# ---------------------------------------------------------------------------
# Step 3 - a real OpportunitySource, so the real DiscoveryEngine (via
# sources.build_source()) can run this unmodified. No new discovery
# mechanism: this implements the EXACT SAME `.meta` + `.discover(limit)`
# protocol HackerNewsDemandSource/RemoteOkSource already implement.
# ---------------------------------------------------------------------------

class DemandDiscoverySource:
    """Wraps one real acquisition_sources.py fetcher (or a fixture, in
    tests) with buyer-intent queries as a `sources.OpportunitySource`.
    `discover(limit)` never raises for a single bad query (see
    `discover_demand_signals`); a totally broken fetcher still surfaces
    through DiscoveryEngine's own existing per-source try/except -
    nothing new is needed there for that case."""

    def __init__(self, name: str, fetcher: AcqSearchable, *,
                 queries: tuple[str, ...] = DEFAULT_QUERIES,
                 since_ts=None, now_iso: str = "") -> None:
        self.name = name
        self._fetcher = fetcher
        self._queries = tuple(queries)
        self._since_ts = since_ts
        #: an explicit "now" (ISO) stamps every record's age consistently
        #: for one run - default "" falls back to wall-clock time per
        #: record (fine for a real run; a caller that needs a frozen
        #: clock, e.g. a test, passes this explicitly).
        self._now_iso = now_iso
        #: populated by the most recent discover() call - per-query
        #: failures that did not stop the batch (spec: transparent
        #: reporting). discovery.py surfaces these into the run's report.
        self.last_errors: list[str] = []
        self.meta = SourceMeta(
            source=name, source_type="demand_signal", source_url="",
            access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
            requires_login=False, requires_human=False,
            policy_status=model.POLICY_OK)

    def discover(self, limit: int) -> list[OpportunityDraft]:
        # a small, bounded per-query fetch regardless of `limit` - this
        # source fans out over several queries already; a large `limit`
        # must not multiply into an excessive number of real requests
        # (StackExchange documents a ~300 req/day/IP keyless quota -
        # acquisition_sources.py).
        per_query = max(1, min(5, int(limit)))
        self.last_errors = []
        drafts = discover_demand_signals(
            self._fetcher, queries=self._queries, limit=per_query,
            since_ts=self._since_ts, now_iso=self._now_iso,
            errors=self.last_errors)
        return drafts[: max(0, int(limit))]


#: name -> (source label persisted on the record, real fetcher class)
_DEMAND_FETCHERS: dict[str, str] = {
    "demand-hn": "hn-algolia",
    "demand-stackexchange": "stackexchange",
    "demand-lobsters": "lobsters",
    "demand-lemmy": "lemmy",
}

#: the two buy-recommendation demand sources (spec: Demand Discovery
#: expansion) - each needs non-default constructor args (a specific SE
#: site / a specific curated Lemmy community set) plus the
#: IntentFilteredSource pre-filter, so they are built explicitly in
#: build_demand_source() rather than through the plain `classes` dict the
#: original four use. Registered here (not in `_DEMAND_FETCHERS`, which
#: only ever mapped a name to an unconfigured class) purely so
#: `sources.py`'s error message / name check can list them too.
_NEW_DEMAND_SOURCE_LABELS: dict[str, str] = {
    "demand-stackexchange-recs": "stackexchange-recs",
    "demand-lemmy-buying": "lemmy-buying",
}


def build_demand_source(name: str, **kw) -> DemandDiscoverySource:
    """Factory mirroring `sources.build_source()`. One of 'demand-hn',
    'demand-stackexchange', 'demand-lobsters', 'demand-lemmy' - each
    wraps the corresponding REAL, keyless `acquisition_sources.py`
    fetcher - or one of the two buy-recommendation sources,
    'demand-stackexchange-recs' (softwarerecs.stackexchange.com - a real
    SE site whose ENTIRE on-topic charter is "recommend a tool/product
    for X") and 'demand-lemmy-buying' (curated consumer-recommendation
    Lemmy communities, filtered through IntentFilteredSource since,
    unlike the single-topic communities `demand-lemmy` uses, they are not
    already narrowly on-topic). `queries=`/`since_ts=`/`now_iso=` override
    the defaults; 'demand-lemmy-buying' also accepts `communities=`."""
    from ..acquisition_sources import (
        HNAlgoliaSource, LemmySource, LobstersSource, StackExchangeSource,
    )

    n = (name or "").strip().lower()
    classes = {
        "demand-hn": HNAlgoliaSource,
        "demand-stackexchange": StackExchangeSource,
        "demand-lobsters": LobstersSource,
        "demand-lemmy": LemmySource,
    }
    if n in classes:
        return DemandDiscoverySource(
            _DEMAND_FETCHERS[n], classes[n](),
            queries=kw.get("queries", DEFAULT_QUERIES), since_ts=kw.get("since_ts"),
            now_iso=kw.get("now_iso", ""))
    if n == "demand-stackexchange-recs":
        inner = StackExchangeSource(sites=("softwarerecs",))
        return DemandDiscoverySource(
            _NEW_DEMAND_SOURCE_LABELS[n], IntentFilteredSource(inner),
            queries=kw.get("queries", DEFAULT_QUERIES), since_ts=kw.get("since_ts"),
            now_iso=kw.get("now_iso", ""))
    if n == "demand-lemmy-buying":
        inner = LemmySource(communities=kw.get("communities", LEMMY_BUYING_COMMUNITIES))
        return DemandDiscoverySource(
            _NEW_DEMAND_SOURCE_LABELS[n], IntentFilteredSource(inner),
            queries=kw.get("queries", BUYING_ADVICE_QUERIES), since_ts=kw.get("since_ts"),
            now_iso=kw.get("now_iso", ""))
    raise ValueError(
        f"unknown demand source {name!r} - one of: "
        + ", ".join(sorted(list(classes) + list(_NEW_DEMAND_SOURCE_LABELS))))

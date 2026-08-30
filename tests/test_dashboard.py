import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.revenuedashboard import render_html
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
        self.assertIn("No agent decisions recorded.", html)
        self.assertIn("Nothing awaiting a human.", html)
        # empty map still renders standing-by nodes, invents no links
        self.assertIn("class='amap'", html)
        self.assertIn("Agents are standing by.", html)
        self.assertIn("operator (CEO)", html)
        self.assertNotIn("marker-end", html)
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
        self.assertIn("class='amap'", html)           # central orchestration map
        self.assertIn("--bg:#070b14", html)           # dark navy theme
        self.assertIn("prefers-color-scheme: light", html)
        self.assertIn("<svg viewBox", html)           # inline SVG avatars/icons
        # sidebar nav items
        for label in ("Dashboard", "Agents", "Tasks", "Opportunities",
                      "Finances", "Automations", "Logs"):
            self.assertIn(label, html)
        # top-bar metrics
        for k in ("Main goal", "Session", "Active agents", "Agent tasks",
                  "Awaiting you", "LLM spend", "Revenue"):
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
        self.assertNotIn("://", html)                        # no protocol anywhere
        self.assertNotRegex(html, r"src\s*=")
        self.assertNotIn("url(http", html)                   # no remote css assets
        self.assertNotIn("<script", html)

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

    def test_agent_map_positions_nodes_and_draws_only_real_links(self):
        base = _report(self.store, self.d)
        # operator only discovered + researched -> exactly those links
        log = [
            {"ts": "t0", "action": "session_start", "reason": ""},
            {"ts": "t1", "action": "discover", "reason": "cold start"},
            {"ts": "t2", "action": "research", "reason": "due diligence"},
        ]
        html = render_html(base, _FIXED_TS, agent_log=log,
                           goal={"sources": ["static"], "decision_policy": "rules"})
        self.assertIn("class='amap'", html)
        self.assertIn("class='node'", html)
        self.assertIn("class='wires'", html)
        # deterministic role positions (operator centre, discovery up top)
        self.assertIn("left:50.0%;top:51.14%", html)   # operator
        self.assertIn("left:50.0%;top:10.0%", html)    # discovery
        # real links present
        self.assertIn("marker-end='url(#wa)'", html)
        # discover -> operator/discovery/evaluator lines exist; investigate did
        # NOT happen, so no operator->planner line
        self.assertIn("x1='400' y1='358' x2='400' y2='70'", html)   # op->discovery
        self.assertIn("x1='400' y1='70' x2='650' y2='232'", html)   # discovery->evaluator
        self.assertIn("x1='400' y1='358' x2='150' y2='232'", html)  # op->research
        self.assertNotIn("x2='400' y2='486'", html)                 # no op->planner
        # most-recent real task shown as a static chip on its edge
        self.assertIn("class='taskchip'", html)
        self.assertIn(">research</div>", html)

    def test_roster_panel_shows_every_agent_by_status(self):
        from revenue_os import roster
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("Target roster", html)
        self.assertIn(f"{len(roster.AGENTS)} agents", html)
        for spec in roster.AGENTS:
            self.assertIn(spec.name, html)
        self.assertIn("class='ragent rlive'", html)
        if roster.planned():
            self.assertIn("class='ragent rplanned'", html)
        # human-gated live agents keep a distinct tag
        self.assertIn(">human-gated</span>", html)
        # the panel never fabricates a status pill / metrics
        panel = html.split("Target roster")[1]
        self.assertNotIn("cs-working", panel)
        self.assertNotIn("class='meta'", panel)

    def test_cluster_flow_shows_every_agent_by_cluster(self):
        from revenue_os import roster
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("class='cflow'", html)
        self.assertIn(f"All {len(roster.AGENTS)} agents", html)
        flow = html.split("class='cflow'")[1].split("Target roster")[0]
        for spec in roster.AGENTS:
            self.assertIn(spec.name, flow, spec.id)
        for label in ("Discovery", "Build", "Marketing", "Acquisition",
                      "Revenue", "Support"):
            self.assertIn(label, flow)
        # human-gated agents keep the signal; nothing without a real output
        # is shown as "ran"
        self.assertIn(">human-gated</span>", flow)
        self.assertNotIn("cnode ran", flow)      # no agent_outputs passed
        self.assertNotIn("class='meta'", flow)   # no fabricated metrics

    def test_cluster_flow_ran_only_from_persisted_output(self):
        outputs = {"find_suppliers": {"ts": "2026-08-27T10:00:00+00:00",
                                      "objective": "run Supplier Finder",
                                      "output": {"confidence": "low"}}}
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           agent_outputs=outputs)
        flow = html.split("class='cflow'")[1].split("Target roster")[0]
        self.assertIn("ran 2026-08-27", flow)          # supplier finder ran
        self.assertEqual(flow.count("cnode ran"), 1)   # exactly the one persisted

    def test_acquisition_panel_empty_is_an_honest_note(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("id='acquisition'", html)
        panel = html.split("id='acquisition'")[1].split("</section>")[0]
        self.assertIn("No acquisition run yet", panel)
        self.assertNotIn("http", panel)

    def test_acquisition_panel_shows_real_counts_no_urls(self):
        acq = {
            "leads": [
                {"prospect_quality": "high", "human_review_status": "new",
                 "url": "https://news.ycombinator.com/item?id=1", "title": "x"},
                {"prospect_quality": "medium", "human_review_status": "reviewed"},
                {"prospect_quality": "none", "human_review_status": "rejected"},
            ],
            "briefs": [
                {"lead_id": "a", "status": "draft"},
                {"lead_id": "b", "status": "posted"},
            ],
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='acquisition'")[1].split("</section>")[0]
        self.assertIn("prospects found", panel)
        self.assertIn("high / medium quality", panel)
        self.assertIn("awaiting a human post", panel)
        # real counts, no fabricated prospect identity, no external URLs
        self.assertNotIn("news.ycombinator.com", panel)
        self.assertNotIn("http", panel)
        self.assertIn("posts every reply", panel)

    def test_acquisition_panel_shows_the_review_queue_without_urls(self):
        acq = {
            "leads": [{"prospect_quality": "medium", "human_review_status": "new"}],
            "briefs": [{"lead_id": "78f0", "status": "draft",
                        "brief": {"draft_reply": {"reply_draft": "hi",
                                                  "promise_language_flagged": []}}}],
            "queue": [{"lead_id": "78f0dbfe24e9", "prospect_quality": "medium",
                       "stage": "prepared", "brief_status": "draft",
                       "promo_allowed": "caution", "age_days": 47,
                       "age_bucket": "stale",
                       "url": "https://news.ycombinator.com/item?id=48905763"}],
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='acquisition'")[1].split("</section>")[0]
        self.assertIn("review queue (de-duped, still open)", panel)
        self.assertIn("with a tailored LLM draft", panel)
        self.assertIn("78f0dbfe24e9", panel)          # lead id shown
        self.assertIn("promo:caution", panel)
        self.assertNotIn("news.ycombinator.com", panel)  # never the URL
        self.assertNotIn("http", panel)

    def test_continuous_panel_empty_and_populated(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("id='continuous'", html)
        empty = html.split("id='continuous'")[1].split("</section>")[0]
        self.assertIn("No continuous session yet", empty)
        self.assertNotIn("http", empty)

        acq = {"loop": {
            "status": "running", "steps_taken": 7, "last_action": "discover",
            "human_queue": ["approve or reject candidate: `x`"],
            "session": {"started_at": "2026-09-01T00:00:00+00:00",
                        "last_tick_at": "2026-09-01T00:05:00+00:00",
                        "ticks": 3, "ended_at": None, "end_reason": None}}}
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='continuous'")[1].split("</section>")[0]
        self.assertIn("ticks", panel)
        self.assertIn("approve or reject candidate", panel)
        self.assertIn("stops at every human gate", panel)
        self.assertIn("no spend", panel)
        self.assertNotIn("http", panel)

    def test_experiments_panel_empty_and_rollup(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("id='experiments'", html)
        empty = html.split("id='experiments'")[1].split("</section>")[0]
        self.assertIn("No experiments yet", empty)
        self.assertNotIn("http", empty)

        from revenue_os.experiments import STATUSES
        acq = {"experiments": {"rollup": {
            "total": 3, "open": 2, "closed": 1,
            "overall": {**{s: 0 for s in STATUSES}, "drafted": 1, "posted": 1,
                        "no_sale": 1},
            "by_source": {"lemmy": {**{s: 0 for s in STATUSES}, "posted": 1,
                                    "no_sale": 1},
                          "hn-algolia": {**{s: 0 for s in STATUSES}, "drafted": 1}},
            "rows": [
                {"source": "lemmy", "platform": "Lemmy", "offer_price": 29.9,
                 "currency": "EUR", "status": "no_sale", "age_days": 20,
                 "revenue_ref": ""},
                {"source": "hn-algolia", "platform": "HN", "offer_price": 29.9,
                 "currency": "EUR", "status": "drafted", "age_days": 1,
                 "revenue_ref": ""},
            ]}}}
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='experiments'")[1].split("</section>")[0]
        self.assertIn("lemmy", panel)
        self.assertIn("no_sale", panel)
        self.assertIn("29.9 EUR", panel)
        self.assertIn("2 open", panel)
        self.assertNotIn("http", panel)
        self.assertNotIn("lead_id", panel)   # no prospect identity

    def test_experiments_panel_shows_feedback_gate(self):
        from revenue_os.experiments import STATUSES
        acq = {"experiments": {
            "rollup": {"total": 2, "open": 0, "closed": 2,
                       "overall": {**{s: 0 for s in STATUSES}, "no_sale": 2},
                       "by_source": {"lemmy": {**{s: 0 for s in STATUSES},
                                               "no_sale": 2}},
                       "rows": [{"source": "lemmy", "platform": "Lemmy",
                                 "offer_price": 29.9, "currency": "EUR",
                                 "status": "no_sale", "age_days": 20,
                                 "revenue_ref": ""}]},
            "feedback": {"settled": 2, "needed": 8, "sale": 0, "no_sale": 2,
                         "ready": False, "advisory_only": True,
                         "note": "not enough settled outcomes yet",
                         "by_source": {"lemmy": {"settled": 2, "sale": 0,
                                                 "no_sale": 2, "sale_rate": 0.0}},
                         "by_quality": {"medium": {"settled": 2, "sale": 0,
                                                   "no_sale": 2, "sale_rate": 0.0}},
                         "by_type": {}}}}
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='experiments'")[1].split("</section>")[0]
        self.assertIn("2 of 8 settled", panel)
        self.assertIn("stay unchanged", panel)      # not-active wording
        self.assertIn("lead quality", panel)
        self.assertNotIn("http", panel)

    def test_first_sale_panel_without_readiness_is_an_honest_note(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("id='first-sale'", html)
        panel = html.split("id='first-sale'")[1].split("</section>")[0]
        self.assertIn("Readiness not computed", panel)
        self.assertNotIn("http", panel)

    def test_first_sale_panel_flags_the_checkout_url_mismatch_and_stale_lead(self):
        acq = {
            "leads": [{"prospect_quality": "medium", "age_days": 47,
                       "human_review_status": "new"}],
            "briefs": [], "queue": [],
            "readiness": {
                "candidate": "ask-hn-x", "candidate_status": "launched",
                "offer_price": 29.9, "offer_currency": "EUR",
                "candidate_public_url": "https://divdav12.github.io/AI-Revenue-OS/checkout.html",
                "outreach_default_url": "https://divdav12.github.io/customer-launch-plan/checkout.html",
                "revenue_eur": 0, "llm_api_calls": 0, "llm_cost_usd": 0.0,
                "checkout_built": True, "checkout_deployed": True,
            },
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='first-sale'")[1].split("</section>")[0]
        # no external URLs - only paths
        self.assertNotIn("http", panel)
        self.assertIn("/AI-Revenue-OS/checkout.html", panel)
        self.assertIn("/customer-launch-plan/checkout.html", panel)
        self.assertIn("hits the wrong page", panel)              # URL mismatch flagged
        self.assertIn("freshest is 47d old", panel)              # stale lead flagged
        self.assertIn("does NOT check the live PayPal", panel)   # honest blind spot
        self.assertIn("Offer live", panel)
        self.assertIn("item(s) need attention", panel)

    def test_first_sale_panel_all_ready_when_urls_match_and_lead_fresh(self):
        url = "https://divdav12.github.io/AI-Revenue-OS/checkout.html"
        acq = {
            "leads": [{"prospect_quality": "high", "age_days": 3,
                       "human_review_status": "new"}],
            "briefs": [], "queue": [{"stage": "prepared", "lead_id": "a"}],
            "readiness": {
                "candidate": "c", "candidate_status": "launched",
                "offer_price": 29.9, "offer_currency": "EUR",
                "candidate_public_url": url, "outreach_default_url": url,
                "revenue_eur": 0, "llm_api_calls": 2, "llm_cost_usd": 0.01,
                "checkout_built": True, "checkout_deployed": True,
            },
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='first-sale'")[1].split("</section>")[0]
        self.assertIn("All disk-checkable items look ready", panel)
        self.assertNotIn("hits the wrong page", panel)
        self.assertNotIn("http", panel)

    def test_agent_outputs_panel_reads_persisted_data_newest_first(self):
        outputs = {
            "find_suppliers": {"ts": "2026-08-25T09:00:00+00:00",
                               "objective": "run Supplier Finder",
                               "output": {"confidence": "none", "research_needed": True}},
            "quality_check": {"ts": "2026-08-27T09:00:00+00:00",
                              "objective": "run Quality Control",
                              "output": {"qc_status": "block", "human_gate_required": False}},
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           agent_outputs=outputs)
        self.assertIn("id='agent-outputs'", html)
        panel = html.split("id='agent-outputs'")[1].split("</section>")[0]
        self.assertIn("Supplier Finder", panel)
        self.assertIn("Quality Control", panel)
        self.assertIn("qc_status=block", panel)
        self.assertIn("research_needed=True", panel)
        # newest first: quality_check (27th) before find_suppliers (25th)
        self.assertLess(panel.index("Quality Control"), panel.index("Supplier Finder"))
        self.assertIn("2 agent(s) with a persisted output", panel)

    def test_agent_outputs_panel_empty_is_honest(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        panel = html.split("id='agent-outputs'")[1].split("</section>")[0]
        self.assertIn("No agent outputs yet", panel)
        self.assertNotIn("<table>", panel)

    def test_pipeline_panel_empty_is_honest(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        panel = html.split("id='pipeline'")[1].split("</section>")[0]
        self.assertIn("No pipeline run yet", panel)
        self.assertIn("Quality Control", panel)   # the pipeline is described

    def test_pipeline_panel_renders_real_run_state(self):
        pipe = {
            "candidate": "dt", "status": "blocked",
            "updated_at": "2026-08-29T12:00:00+00:00",
            "steps": {
                "select": {"status": "ok", "summary": {"kept": "1 item(s)"}},
                "research": {"status": "skipped", "reason": "LLM-only"},
                "find_suppliers": {"status": "ok"},
                "quality_check": {"status": "ok", "summary": {"qc_status": "block"}},
            },
            "human_gate": {"reason": "Quality Control returned qc_status=block",
                           "blocking_issues": ["pricing mismatch"]},
            "error": "quality_check: block",
        }
        html = render_html(_report(self.store, self.d), _FIXED_TS, pipeline=pipe)
        panel = html.split("id='pipeline'")[1].split("</section>")[0]
        self.assertIn("candidate <b>dt</b>", panel)
        self.assertIn("Opportunity Finder", panel)
        self.assertIn("Store Builder", panel)
        self.assertIn("human-gated", panel)          # store_builder tagged
        self.assertIn("skipped", panel)
        self.assertIn("pricing mismatch", panel)
        self.assertIn("quality_check: block", panel)
        self.assertNotIn("://", html)

    def test_map_edges_come_from_real_task_lineage(self):
        base = _report(self.store, self.d)
        root = "t-root"
        task_log = [
            {"ts": "1", "task_id": root, "parent_id": None, "depth": 0,
             "capability": "discover", "agent": "market_scanner",
             "status": "ok", "summary": {"count": 3}},
            {"ts": "2", "task_id": "e1", "parent_id": root, "depth": 1,
             "capability": "evaluate", "agent": "evaluator",
             "status": "ok", "summary": {"total": 3.0}},
            {"ts": "3", "task_id": "e2", "parent_id": root, "depth": 1,
             "capability": "evaluate", "agent": "evaluator",
             "status": "ok", "summary": {"total": 2.0}},
            {"ts": "4", "task_id": "s1", "parent_id": root, "depth": 1,
             "capability": "select", "agent": "opportunity_finder",
             "status": "ok", "summary": {"kept": 2, "shortlist": 2}},
        ]
        html = render_html(base, _FIXED_TS,
                           agent_log=[{"ts": "0", "action": "discover", "reason": "x"}],
                           task_log=task_log)
        # operator -> market scanner (root), scanner -> evaluator, scanner -> finder
        self.assertIn("x1='400' y1='358' x2='400' y2='70'", html)   # op -> scanner
        self.assertIn("x1='400' y1='70' x2='650' y2='232'", html)   # scanner -> evaluator
        self.assertIn("x1='400' y1='70' x2='650' y2='400'", html)   # scanner -> finder
        self.assertIn("&times;2", html)                             # fan-out count
        # merged activity feed carries the real task rows
        self.assertIn("opportunity_finder", html)
        self.assertIn("Opportunity Finder", html)   # map node
        self.assertNotIn("://", html)

    def test_competitor_analyzer_node_and_lineage_and_note(self):
        self.store.put(Candidate(
            name="alpha", status="shortlisted", total=3.0, verdict="hold",
            competition={"verdict": "crowded", "rationale": "mature category",
                         "basis": "model knowledge, no web"},
        ))
        task_log = [
            {"ts": "1", "task_id": "c1", "parent_id": None, "depth": 0,
             "capability": "analyze_competition", "agent": "competitor_analyzer",
             "status": "ok", "summary": {"candidate_name": "alpha"}},
        ]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "0", "action": "analyze_competition", "reason": "x"}],
            task_log=task_log, goal={"competition": "llm"},
        )
        self.assertIn("Competitor Analyzer", html)          # map node
        self.assertIn("x1='400' y1='358' x2='650' y2='560'", html)  # operator -> competitor
        self.assertIn("competition: crowded", html)         # candidate details
        self.assertNotIn("://", html)

    def test_copywriter_node_and_lineage_and_headline(self):
        self.store.put(Candidate(
            name="alpha", status="validated", total=3.0, verdict="hold",
            offer={"what_is_sold": "x", "price": 49.0},
            launch_draft={"headline": "Ship it in a weekend",
                          "primary_cta": "Buy now", "basis": "model draft, not published"},
        ))
        task_log = [
            {"ts": "1", "task_id": "k1", "parent_id": None, "depth": 0,
             "capability": "write_copy", "agent": "copywriter",
             "status": "ok", "summary": {"candidate_name": "alpha"}},
        ]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "0", "action": "write_copy", "reason": "x"}],
            task_log=task_log, goal={"copywriter": "llm"},
        )
        self.assertIn("Copywriter AI", html)                      # map node
        self.assertIn("x1='400' y1='358' x2='150' y2='560'", html)  # operator -> copywriter
        self.assertIn("Ship it in a weekend", html)               # candidate details
        self.assertNotIn("://", html)

    def test_revenue_analysis_panel_and_node_and_lineage(self):
        empty = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("No revenue analysis yet.", empty)
        task_log = [
            {"ts": "1", "task_id": "r1", "parent_id": None, "depth": 0,
             "capability": "analyze_revenue", "agent": "revenue_analyst",
             "status": "ok", "summary": {}},
        ]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "0", "action": "analyze_revenue", "reason": "x"}],
            task_log=task_log, goal={"revenue_analyst": True},
            revenue_analysis={
                "ts": "T",
                "portfolio": {"revenue": 120.0, "spent": 10.0, "net": 110.0,
                              "roi_ratio": 11.0, "launched": 1, "earning": 0},
                "per_candidate": [{"name": "win", "status": "launched",
                                   "revenue": 120.0, "spent": 10.0, "net": 110.0,
                                   "roi_ratio": 11.0}],
                "best": {"name": "win", "net": 110.0}, "worst": None,
                "spend_efficiency": 11.0, "outcome_signal": "not enough outcomes yet",
                "readout": "Portfolio: $120.0 revenue, $10.0 spent, $110.0 net.",
            },
        )
        self.assertIn("Revenue Analyst", html)                          # map node
        self.assertIn("x1='400' y1='358' x2='650' y2='660'", html)      # operator -> analyst
        self.assertIn("Portfolio: $120.0 revenue", html)                # panel readout
        self.assertIn(">win<", html)
        self.assertNotIn("://", html)

    def test_web_grounded_notes_render_sources_scheme_stripped(self):
        self.store.put(Candidate(
            name="alpha", status="shortlisted", total=3.0, verdict="hold",
            research={"verdict": "caution", "rationale": "crowded",
                      "basis": "web search, 2 sources",
                      "sources": [{"url": "https://zapier.com/pricing",
                                   "title": "Zapier Pricing"}]},
        ))
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           goal={"research": "web"})
        self.assertIn("web search, 2 sources", html)
        self.assertIn("zapier.com/pricing", html)
        self.assertIn("Zapier Pricing", html)
        self.assertNotIn("://", html)                 # scheme stripped
        # the Product Researcher node shows web mode as enabled
        self.assertIn("mode: web", html)

    def test_content_creator_node_lineage_and_candidate_line(self):
        self.store.put(Candidate(
            name="alpha", status="validated", total=3.0, verdict="hold",
            offer={"what_is_sold": "x", "price": 29.0},
            deliverable={"dir": "deliverables/alpha", "files": ["landing.html"]},
        ))
        task_log = [
            {"ts": "1", "task_id": "p1", "parent_id": None, "depth": 0,
             "capability": "package_deliverable", "agent": "content_creator",
             "status": "ok", "summary": {"candidate_name": "alpha"}},
        ]
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "0", "action": "package_deliverable", "reason": "x"}],
            task_log=task_log, goal={"content_creator": True},
        )
        self.assertIn("Content Creator", html)                     # map node
        self.assertIn("x1='400' y1='358' x2='250' y2='660'", html)  # operator -> content
        self.assertIn("deliverables/alpha/landing.html", html)     # candidate details
        self.assertIn("(not published)", html)
        self.assertNotIn("://", html)

    def test_trends_panel_real_or_empty(self):
        empty = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("No trend analysis yet.", empty)
        with_trend = render_html(
            _report(self.store, self.d), _FIXED_TS,
            goal={"trend_hunter": True},
            trend={"ts": "T", "count": 4, "runs": 1,
                   "keywords": [["automation", 3], ["notion", 2]],
                   "sources": {"hn": 4}},
        )
        self.assertIn("automation", with_trend)
        self.assertIn("Trend Hunter", with_trend)

    def test_agent_map_empty_links_shows_standby_not_fake_edges(self):
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            goal={"sources": ["static"], "decision_policy": "rules"},
        )
        self.assertIn("No active task links", html)
        self.assertIn("Agents are standing by.", html)
        self.assertNotIn("<line", html)
        self.assertNotIn("class='taskchip'", html)
        # map nodes still rendered
        self.assertIn("class='node'", html)
        self.assertIn("operator (CEO)", html)
        self.assertIn("Product Researcher", html)

    def test_agent_map_decision_link_needs_llm_policy_and_real_call(self):
        base = _report(self.store, self.d)
        log = [{"ts": "t", "action": "stop", "reason": "enough"}]
        # llm policy configured but no decide spend -> no decision->operator link
        no_call = render_html(base, _FIXED_TS, agent_log=log,
                              goal={"decision_policy": "llm"})
        self.assertNotIn("x1='120' y1='368'", no_call)
        # with a real decide spend entry -> link appears
        with_call = render_html(
            base, _FIXED_TS, agent_log=log,
            spend_entries=[{"ts": "t", "activity": "decide", "api_calls": 1,
                            "cost_usd": 0.01}],
            goal={"decision_policy": "llm"},
        )
        self.assertIn("x1='120' y1='368' x2='400' y2='358'", with_call)

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
        self.assertIn("Product Researcher", html)
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

    def test_interactive_default_off_is_byte_identical(self):
        self.store.put(Candidate(name="alpha", status="shortlisted"))
        r = _report(self.store, self.d)
        self.assertEqual(render_html(r, _FIXED_TS),
                         render_html(r, _FIXED_TS, interactive=False))
        plain = render_html(r, _FIXED_TS)
        self.assertNotIn("action='/action'", plain)
        self.assertNotIn("class='gate-form'", plain)
        self.assertNotIn("<button", plain)

    def test_interactive_renders_gate_forms_per_status(self):
        self.store.put(Candidate(name="s1", status="shortlisted"))
        self.store.put(Candidate(name="i1", status="investigating"))
        self.store.put(Candidate(name="v1", status="validated",
                                 offer={"price": 9.0}))
        self.store.put(Candidate(name="l1", status="launched"))
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           interactive=True, csrf="TOK", flash="ok: s1 -> approved")
        self.assertIn("action='/action'", html)
        self.assertIn("name='csrf' value='TOK'", html)
        self.assertIn("class='flash'", html)
        # shortlisted -> approve/reject
        self.assertIn("name='action' value='approve'", html)
        self.assertIn("name='action' value='reject'", html)
        # investigating -> outcome + metric input
        self.assertIn("name='action' value='outcome'", html)
        self.assertIn("name='metric'", html)
        self.assertIn("name='result' value='validated'", html)
        # validated -> launch
        self.assertIn("name='action' value='launch'", html)
        # launched -> payment + amount input
        self.assertIn("name='action' value='payment'", html)
        self.assertIn("name='amount'", html)
        # still no JavaScript / external refs
        self.assertNotIn("<script", html)
        self.assertNotIn("://", html)

    def test_interactive_flash_error_gets_err_class(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           interactive=True, flash="error: bad")
        self.assertIn("class='flash err'", html)

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


class CommandCoreTests(unittest.TestCase):
    """The mission-control band: the objective, the real lifecycle rail and
    the human-maintained blocker register."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.store = CandidateStore(Path(self.d) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def _core(self, html):
        return html.split("id='command'")[1].split("</section>")[0]

    def test_objective_and_lifecycle_rail_use_real_counts(self):
        self.store.put(Candidate(name="a", status="discovered"))
        self.store.put(Candidate(name="b", status="discovered"))
        self.store.put(Candidate(name="c", status="launched"))
        self.store.put(Candidate(name="d", status="rejected"))
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS))
        self.assertIn("FIRST REAL CUSTOMER", core)
        self.assertIn("Continuous discovery", core)
        for label in ("Discovery", "Shortlist", "Approve", "Investigate",
                      "Validate", "Launch", "Earning", "Rejected"):
            self.assertIn(label, core)
        # 2 discovered / 1 launched / 1 rejected, and the empty stages stay 0
        self.assertIn("<div class='sv'>2</div>", core)
        self.assertIn("<div class='sv'>1</div>", core)
        self.assertIn("<div class='sv'>0</div>", core)
        # human-gated transitions are marked as such
        self.assertIn("stage gate", core)
        self.assertIn("never approves, launches, or books a", core)

    def test_revenue_is_only_real_recorded_payments(self):
        self.store.put(Candidate(name="a", status="launched"))
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS))
        self.assertIn("revenue booked", core)
        self.assertIn("0 EUR", core)          # nothing booked -> zero, not blank
        self.assertIn("none launched", core)  # no priced offer on disk

    def test_offer_line_comes_from_the_candidate_store(self):
        self.store.put(Candidate(
            name="ask-hn-x", status="launched",
            offer={"what_is_sold": "Customer Launch Plan", "price": 29.9,
                   "currency": "EUR"}))
        acq = {"readiness": {"candidate": "ask-hn-x", "candidate_status": "launched",
                             "offer_price": 29.9, "offer_currency": "EUR",
                             "revenue_eur": 0, "llm_api_calls": 0,
                             "llm_cost_usd": 0.0}}
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS,
                                      acquisition=acq))
        self.assertIn("Customer Launch Plan", core)
        self.assertIn("29.9 EUR", core)

    def test_blocker_register_absent_is_honest_not_all_clear(self):
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS))
        self.assertIn("No blocker register", core)
        self.assertIn("not an all-clear", core)

    def test_open_blockers_are_shown_and_resolved_ones_are_not(self):
        blockers = [
            {"id": "paypal", "title": "PayPal checkout blocked",
             "detail": "PAYEE_ACCOUNT_RESTRICTED on the live payment path",
             "severity": "critical", "status": "open", "area": "payment"},
            {"id": "old", "title": "Something already fixed", "detail": "",
             "severity": "warning", "status": "resolved", "area": "checkout"},
        ]
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS,
                                      blockers=blockers))
        self.assertIn("1 open blocker(s)", core)
        self.assertIn("PayPal checkout blocked", core)
        self.assertIn("PAYEE_ACCOUNT_RESTRICTED", core)
        self.assertIn("sev-critical", core)
        self.assertNotIn("Something already fixed", core)
        self.assertIn("not auto-detected and are not hidden", core)

    def test_all_blockers_resolved_says_so_without_claiming_all_clear(self):
        blockers = [{"id": "x", "title": "t", "severity": "info",
                     "status": "resolved"}]
        core = self._core(render_html(_report(self.store, self.d), _FIXED_TS,
                                      blockers=blockers))
        self.assertIn("No open blockers", core)
        self.assertIn("1 recorded, all resolved", core)

    def test_blocker_text_is_escaped_and_scheme_stripped(self):
        blockers = [{"id": "x", "title": "<script>alert(1)</script>",
                     "detail": "see https://example.com/page",
                     "severity": "warning", "status": "open"}]
        html = render_html(_report(self.store, self.d), _FIXED_TS,
                           blockers=blockers)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("://", html)          # the page-wide invariant holds


class FleetGridTests(unittest.TestCase):
    """The 24-agent cluster grid. Every cell is driven by a file on disk;
    nothing is animated into looking busy."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.store = CandidateStore(Path(self.d) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def _flow(self, html):
        return html.split("class='cflow'")[1].split("Target roster")[0]

    def _cells(self, flow):
        """Every fleet cell as its own chunk of markup."""
        starts = [m.start() for m in re.finditer(r"<div class='cnode(?=[ '])", flow)]
        return [flow[s:(starts[i + 1] if i + 1 < len(starts) else len(flow))]
                for i, s in enumerate(starts)]

    def _cell(self, flow, name):
        """The markup of the one fleet cell carrying `name`."""
        for chunk in self._cells(flow):
            if f">{name}</span>" in chunk:
                return chunk
        raise AssertionError(f"no fleet cell for {name!r}")

    def test_every_roster_agent_has_a_cell_with_name_cluster_and_status(self):
        from revenue_os import roster
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS))
        for spec in roster.AGENTS:
            self.assertIn(spec.name, flow, spec.id)
        for cluster in roster.CLUSTERS:
            self.assertIn(cluster, flow)
        self.assertEqual(len(self._cells(flow)), len(roster.AGENTS))

    def test_agent_without_any_record_is_idle_with_no_activity(self):
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS))
        cell = self._cell(flow, "Sales Tracker")
        self.assertIn("st-idle", cell)
        self.assertIn("IDLE", cell)
        self.assertIn("No activity recorded", cell)
        self.assertIn("runs <b>0</b>", cell)
        # never animated into looking busy, never given a progress bar
        self.assertNotIn("fs-running", cell)
        self.assertNotIn("class='bar'", cell)

    def test_running_needs_a_live_session_and_a_real_dispatched_task(self):
        task_log = [{"ts": "1", "task_id": "t1", "parent_id": None, "depth": 0,
                     "capability": "discover", "agent": "market_scanner",
                     "objective": "discover opportunities", "status": "ok",
                     "summary": {"count": 3}}]
        live = render_html(_report(self.store, self.d), _FIXED_TS,
                           task_log=task_log,
                           session={"ticks": 1, "cycles": 1, "ended_at": None})
        cell = self._cell(self._flow(live), "Market Scanner")
        self.assertIn("fs-running", cell)
        self.assertIn("RUNNING", cell)
        self.assertIn("runs <b>1</b>", cell)
        # same task log, session over -> the run is history, not activity
        ended = render_html(_report(self.store, self.d), _FIXED_TS,
                            task_log=task_log,
                            session={"ticks": 1, "cycles": 1, "ended_at": "x",
                                     "end_reason": "max-ticks"})
        cell = self._cell(self._flow(ended), "Market Scanner")
        self.assertNotIn("fs-running", cell)
        self.assertIn("st-idle", cell)
        self.assertIn("runs <b>1</b>", cell)
        self.assertIn("discover", cell)      # the real last activity

    def test_goal_mode_off_disables_the_agent(self):
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS,
                                      goal={"research": "off", "competition": "llm"}))
        self.assertIn("DISABLED", self._cell(flow, "Product Researcher"))
        self.assertNotIn("DISABLED", self._cell(flow, "Competitor Analyzer"))

    def test_human_gated_agents_are_marked_on_every_cell(self):
        from revenue_os import roster
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS))
        gated = [s for s in roster.AGENTS if s.gate == "human"]
        self.assertTrue(gated)
        self.assertEqual(flow.count("cnode gated"), len(gated))
        for spec in gated:
            self.assertIn("HUMAN-GATED", self._cell(flow, spec.name))

    def test_outreach_drafter_waits_for_a_human_when_a_draft_exists(self):
        acq = {"leads": [{"prospect_quality": "medium",
                          "human_review_status": "new"}],
               "briefs": [{"lead_id": "a", "status": "draft"},
                          {"lead_id": "b", "status": "draft"}],
               "queue": [{"lead_id": "a", "stage": "prepared"}]}
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS,
                                      acquisition=acq))
        cell = self._cell(flow, "Outreach Drafter")
        self.assertIn("WAITING", cell)
        self.assertIn("awaiting your review and your own post", cell)
        self.assertIn("briefs <b>2</b>", cell)

    def test_progress_bar_only_when_the_agent_persisted_a_progress_value(self):
        outputs = {"manage_profit": {"ts": "2026-08-27T09:00:00+00:00",
                                     "objective": "run Profit Master",
                                     "output": {"progress_pct": 40}},
                   "track_sales": {"ts": "2026-08-27T09:00:00+00:00",
                                   "objective": "run Sales Tracker",
                                   "output": {"paid_count": 0}}}
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS,
                                      agent_outputs=outputs))
        self.assertIn("width:40%", self._cell(flow, "Profit Master"))
        self.assertNotIn("cnode-prog", self._cell(flow, "Sales Tracker"))
        # exactly one bar in the whole fleet - the one an agent reported
        self.assertEqual(flow.count("class='cnode-prog'"), 1)

    def test_cost_ceiling_shows_as_blocked_not_as_running(self):
        spend = [{"ts": "t", "activity": "research", "api_calls": 1,
                  "cost_usd": 0.5, "ceiling_hit": True}]
        flow = self._flow(render_html(_report(self.store, self.d), _FIXED_TS,
                                      spend_entries=spend,
                                      goal={"research": "llm"}))
        cell = self._cell(flow, "Product Researcher")
        self.assertIn("fs-blocked", cell)
        self.assertIn("BLOCKED", cell)

    def test_acquisition_chain_is_the_real_three_step_flow(self):
        acq = {"leads": [{"prospect_quality": "high", "human_review_status": "new"},
                         {"prospect_quality": "none", "human_review_status": "new"}],
               "briefs": [{"lead_id": "a", "status": "draft"}],
               "queue": [{"lead_id": "a", "stage": "prepared"}]}
        html = render_html(_report(self.store, self.d), _FIXED_TS, acquisition=acq)
        panel = html.split("id='acquisition'")[1].split("</section>")[0]
        chain = panel.split("class='chain'")[1].split("</div>\n")[0]
        self.assertIn("Prospect Scout", chain)
        self.assertIn("Opportunity Scorer", chain)
        self.assertIn("Outreach Drafter", chain)
        self.assertLess(chain.index("Prospect Scout"), chain.index("Opportunity Scorer"))
        self.assertLess(chain.index("Opportunity Scorer"), chain.index("Outreach Drafter"))
        self.assertIn("chain-step gate", chain)          # the drafter is gated
        self.assertIn("It never posts, DMs, or emails", panel)
        self.assertIn("<b>2</b> public post(s)", chain)  # real store counts
        self.assertIn("<b>1</b> high/medium quality", chain)
        self.assertNotIn("http", panel)

    def test_orchestration_readout_counts_are_real(self):
        html = render_html(
            _report(self.store, self.d), _FIXED_TS,
            agent_log=[{"ts": "t", "action": "discover", "reason": "x"}],
            task_log=[{"ts": "1", "task_id": "a", "parent_id": None, "depth": 0,
                       "capability": "discover", "agent": "market_scanner",
                       "status": "ok", "summary": {}}],
        )
        self.assertIn("operator decisions recorded", html)
        self.assertIn("tasks dispatched (task_log)", html)
        self.assertIn("agents with a recorded run", html)
        self.assertIn("1 / 24", html)
        self.assertIn("No link is drawn to make", html)


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

    def test_dashboard_loads_agent_outputs_json_from_disk(self):
        from revenue_os.agent_runner import run_agent
        run_agent(Path(self.data), "manage_profit", {"booked_revenue": 200})
        code, _ = _run(["dashboard", "--data-dir", self.data])
        self.assertEqual(code, 0)
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Profit Master", html)
        self.assertIn("id='agent-outputs'", html)
        self.assertIn("cnode ran", html)          # shown as ran in the flow
        self.assertNotIn("://", html)

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

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

    def test_roster_panel_shows_all_21_with_planned_marked(self):
        from revenue_os import roster
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("Target roster", html)
        for spec in roster.AGENTS:
            self.assertIn(spec.name, html)
        self.assertIn("class='ragent rlive'", html)      # live agents
        self.assertIn("class='ragent rplanned'", html)   # planned agents
        # a planned agent carries no status pill / metrics
        panel = html.split("Target roster")[1]
        self.assertNotIn("cs-working", panel)
        self.assertNotIn("class='meta'", panel)
        self.assertIn("Planned agents are not running", html)

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

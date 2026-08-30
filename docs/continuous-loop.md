# Continuous revenue loop (Phase 3)

`revenue-loop --watch` ticks the existing OBSERVE -> DECIDE -> ACT
supervisor on an interval, bounded and resumable. It adds **no new
autonomous action**: every tick runs the same deterministic pipeline
(`revenue-loop`) and stops at every human gate. It spends nothing, calls
no API, and never touches PayPal write actions or contacts a prospect.

```
python -m revenue_os revenue-loop --watch --data-dir data \
  --interval 900 \
  [--max-ticks N] [--max-runtime SECONDS] [--max-spend USD] \
  [--fresh] [--dashboard] \
  [--discovery-cooldown-hours 6] [--followup-days 14] [--no-discovery]
```

## What one tick does

1. `revenue-loop` run: stage an approved plan's PDF, run+deploy the
   pipeline for a qualified candidate, read-only PayPal booking, and one
   **free** discovery cycle (`autopilot`), stopping at the first human
   gate.
2. Experiment feedback (deterministic, read-only):
   - `open_from_briefs` - one experiment per prospect outreach attempt
   - `correlate_sale` - join intake + revenue ledger (never calls PayPal)
   - `sweep` - close `posted` experiments older than `--followup-days`
     with no intake as `no_sale`

## Cooldowns

- **Discovery** only re-runs every `--discovery-cooldown-hours` (default
  6). Brief prep, the review queue, the payment check and funnel status
  still run every tick. `0` disables the cooldown (previous behaviour).
- A lead that was **rejected**, **skipped**, or whose experiment closed
  (`sale` / `no_sale`) never re-surfaces in the acquisition queue.
- A **posted** lead is not re-nagged during its follow-up window.

## Experiments

```
python -m revenue_os experiments --data-dir data          # ledger + per-source rollup
python -m revenue_os experiment-close <lead-id> no_sale   # a human closes one
python -m revenue_os outreach-status <lead-id> posted     # also advances the experiment
```

Lifecycle: `drafted -> posted -> intake -> sale` · `drafted -> skipped` ·
`posted -> no_sale`. State in `data/experiments.json` (atomic,
restart-safe). One experiment per lead, ever.

## Safety / restart

- Ctrl-C exits cleanly; `end_reason` is recorded in
  `data/revenue_loop.json` under `session`.
- An unfinished `--watch` session **resumes** on the next start (counters
  continue); `--fresh` starts a new one.
- `--max-spend` bounds cumulative LLM spend since the session started (a
  safety net - the deterministic path spends nothing).
- Runs unchanged with `ANTHROPIC_API_KEY` unset: discovery uses the free
  keyless sources, scoring is deterministic, outreach is the template
  brief, experiments are deterministic. The LLM paths (`--score llm`,
  `--draft llm`, `draft-launch-plan`) stay opt-in and simply unavailable.

## Scheduling

Compose with cron / Task Scheduler / the `schedule` skill - run
`revenue-loop --watch --max-runtime 3600` on a timer so a crash or hang
is bounded and the next run resumes.

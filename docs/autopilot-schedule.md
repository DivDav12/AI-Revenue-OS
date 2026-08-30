# Running the autopilot on a schedule

`autopilot` is one idempotent cycle: free discovery -> outreach briefs +
review queue -> read-only PayPal booking -> funnel status. It never posts,
messages, emails, or spends past the EUR 3.00 pre-sale cap. Running it on a
timer just keeps the acquisition queue fresh; a human still does every
post, approval, and send.

## What one cycle does

```
python -m revenue_os autopilot cycle --data-dir data --delay 1
```

1. `discover-free` across the keyless sources (hn-algolia, stackexchange,
   lobsters, lemmy). $0.
2. Drafts an outreach brief for each new high/medium-quality lead.
3. Rebuilds the **acquisition review queue** - every high/medium prospect
   still waiting on a person, de-duped (a lead you rejected, or a brief
   marked `posted`/`skipped`, never comes back).
4. Read-only PayPal booking if credentials are in `.env`.

Then check the queue and act:

```
python -m revenue_os acquisition-queue --data-dir data
python -m revenue_os outreach-brief <lead-id> --data-dir data   # full draft
# ... you post the reply yourself, then:
python -m revenue_os outreach-status <lead-id> posted --data-dir data
```

`outreach-status <id> posted` (or `skipped`, or `review-opportunity <id>
--reject`) removes the prospect from the queue for good - it never
re-surfaces on later cycles.

## Cadence

- **Every 3-6 hours** is plenty. Genuine "how do I get my first customers"
  posts are rare; a fresh ask stays actionable for days.
- Do **not** run it every few minutes: StackExchange's keyless API has a
  ~300 requests/day/IP quota (the source is failure-isolated, but you just
  waste the quota), and there is nothing new to find that often.
- One `--delay 1` (one second between queries) is enough politeness for
  the free sources.

## Scheduling it

### Claude Code `schedule` skill (cloud routine)

```
/schedule create "autopilot cycle" --cron "0 */4 * * *" \
  --prompt "run: python -m revenue_os autopilot cycle --data-dir data --delay 1, then summarise the acquisition queue"
```

### Plain cron (self-hosted)

```
0 */4 * * *  cd /path/to/AI-Revenue-OS && PYTHONPATH=src python -m revenue_os autopilot cycle --data-dir data --delay 1 >> data/autopilot.log 2>&1
```

### Windows Task Scheduler

Action: `powershell -NoProfile -Command "cd C:\path\to\AI-Revenue-OS; $env:PYTHONPATH='src'; python -m revenue_os autopilot cycle --data-dir data --delay 1"`
Trigger: every 4 hours.

## Pausing

```
python -m revenue_os autopilot pause  --reason "travelling"
python -m revenue_os autopilot resume
```

A paused autopilot runs no cycle (scheduled invocations become no-ops)
until you `resume`.

---
name: ism-shop-executive
description: >-
  Produces daily shop intelligence reports covering bay occupancy, technician utilization,
  pending customer approvals, parts holds, and completed revenue. Use when a manager or
  owner asks for shop performance, bottlenecks, or end-of-day summaries.
---

# Shop Executive Intelligence

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. Use `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly with the exact flags below.

## 1. Gather Live Shop Telemetry & Cockpit View

1. Open the local monitoring cockpit URL with `python -m tools.cli --json cockpit` (or inspect computed gauges, coupled Bay × Technician bottlenecks, and state-aware Action Queue in JSON via `python -m tools.cli --json cockpit --data-only`).
2. Or run `python -m tools.cli --json batch --cmd "status --date <YYYY-MM-DD>" --cmd "tech-overview --date <YYYY-MM-DD>"` (or omit `--date` for today) to collect active bays, on-duty technicians, scheduled appointments, and utilization in one call.

## 2. Query Work-in-Progress, Action Queue, and Bottlenecks

1. Run `python -m tools.cli --json list-docs --doctype "Repair Order" --filter "status=not in:Closed / Released,Cancelled" --fields "name,customer,vehicle,status,approval_status,grand_total,outstanding_amount" --limit 50` (add `--filter "customer=<NAME>"` when querying a specific fleet or customer; use `--fields` / `--filter` directly instead of piping truncated output into Python).
2. Run `python -m tools.cli --json action-items --status Open` to surface synced irregularities, shortages, or escalations. When handed one or more Shop Board `ACT-...` IDs (or drilling into active RO board cards), run `python -m tools.cli --json action-cards --action-item <ACT-1> [<ACT-2> ...]` (or `--repair-order <RO>`) for the single-request context bundle (RO summary, transcript, media links, and clearing conditions), or `python -m tools.cli --json board-threads --status OPEN` to scan active RO threads. Mark handled items done directly via `python -m tools.cli --json resolve-action --action-item <ID> [--notes <TEXT>]` (direct housekeeping, no extra confirmation click; idempotent if already auto-cleared), or snooze/dismiss via `python -m tools.cli --json sync-action`. Only call `python -m tools.cli --json board-post --repair-order <RO> --text <TEXT>` when sending a direct floor message to the technician. Use `python -m tools.cli --json vehicle-info --vehicle <VEH>` or `python -m tools.cli --json get-inspection --repair-order <RO>` when drilling into specific vehicle history or walkaround completion.
3. Group active Repair Orders into clear operational buckets:
   - **Pending Authorization**: Quotes awaiting customer signature.
   - **Waiting on Parts**: Vehicles stalled in bays or lot due to parts deficits.
   - **Authorized - In Progress**: Vehicles actively on lifts.
   - **Work Completed / Unpaid**: Finished jobs ready for billing and release.

## 3. Evening & Catch-Up Shop Health Scoring

When asked for an evening P&L / shop health run (or when `cockpit --data-only` shows `pnl_health` is `Stale`):

1. Run `python -m tools.cli --json batch --cmd "cockpit --data-only" --cmd "pnl-digest"` to pull exact server-computed P&L totals, Effective Labor Rate, realized parts margin, GL net profit, unbilled WIP, and next-open-day bookings.
2. Score `pnl_health` (`0–100`, grade `Healthy`/`Watch`/`Critical`) from server margins, ELR, parts margin %, and net profit, plus `tomorrow_readiness` (`0–100`) from next-day bookings vs. stock, open POs, and technician availability. Never recompute taxes or alter server totals.
3. Stage or preview (`--stage` / `--dry-run`) `publish-score` for each health score (including Frappe P&L report `report_url`) and log concrete operational irregularities using `python -m tools.cli --stage create-action --dedup-key "<rule>:<target>" --source Agent ...` so all manager workstations sync cleanly.

## 4. Present Executive Briefing

1. Format the report as concise Markdown tables with total dollar values, bay occupancy ratio, and top bottlenecks.
2. Highlight actionable next steps (for example: follow up on pending SMS approvals or expedite holds).
3. Never alter accounting totals or compute custom tax estimates; report exact server fields (`grand_total`, `outstanding_amount`).

## Verification

- Cross-check bay and appointment counts against `status` output before presenting totals.

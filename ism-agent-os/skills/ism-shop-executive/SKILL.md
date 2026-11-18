---
name: ism-shop-executive
description: >-
  Produces daily shop intelligence reports covering bay occupancy, technician utilization,
  pending customer approvals, parts holds, and completed revenue. Use when a manager or
  owner asks for shop performance, bottlenecks, or end-of-day summaries.
---

# Shop Executive Intelligence

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. Use `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly with the exact flags below.

## 1. Gather Live Shop Telemetry

1. Run `python -m tools.cli --json batch --cmd "status --date <YYYY-MM-DD>" --cmd "tech-overview --date <YYYY-MM-DD>"` (or omit `--date` for today) to collect active bays, on-duty technicians, scheduled appointments, and utilization in one call.

## 2. Query Work-in-Progress and Bottlenecks

1. Run `python -m tools.cli --json list-docs --doctype "Repair Order" --filter "status=not in:Closed / Released,Cancelled" --fields "name,customer,vehicle,status,approval_status,grand_total,outstanding_amount" --limit 50` (add `--filter "customer=<NAME>"` when querying a specific fleet or customer; use `--fields` / `--filter` directly instead of piping truncated output into Python).
2. Run `python -m tools.cli --json action-items --status Open` to surface open shop floor discrepancies, inventory shortages, or escalations (and mark handled ones done with `python -m tools.cli --json resolve-action --action-item <ID> [--notes <TEXT>]`). Use `python -m tools.cli --json vehicle-info --vehicle <VEH>` or `python -m tools.cli --json get-inspection --repair-order <RO>` when drilling into specific vehicle history or walkaround completion.
3. Group active Repair Orders into clear operational buckets:
   - **Pending Authorization**: Quotes awaiting customer signature.
   - **Waiting on Parts**: Vehicles stalled in bays or lot due to parts deficits.
   - **Authorized - In Progress**: Vehicles actively on lifts.
   - **Work Completed / Unpaid**: Finished jobs ready for billing and release.

## 3. Present Executive Briefing

1. Format the report as concise Markdown tables with total dollar values, bay occupancy ratio, and top bottlenecks.
2. Highlight actionable next steps (for example: follow up on pending SMS approvals or expedite holds).
3. Never alter accounting totals or compute custom tax estimates; report exact server fields (`grand_total`, `outstanding_amount`).

## Verification

- Cross-check bay and appointment counts against `status` output before presenting totals.

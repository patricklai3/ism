---
name: ism-shop-executive
description: >-
  Produces daily shop intelligence reports covering bay occupancy, technician utilization,
  pending customer approvals, parts holds, and completed revenue. Use when a manager or
  owner asks for shop performance, bottlenecks, or end-of-day summaries.
---

# Shop Executive Intelligence

Use this workflow to compile operational briefings for shop managers and owners via the ISM Agent OS CLI (`python -m tools.cli --json ...` or `ism-agent-os ...`).

## 1. Gather Live Shop Telemetry

1. Run `ism-agent-os status` (optionally `--date YYYY-MM-DD`, defaults to today) to collect active bays, on-duty technicians, and scheduled appointments.
2. Run `ism-agent-os tech-overview` with `--date` to obtain scheduled hours per technician and identify unassigned jobs.

## 2. Query Work-in-Progress and Bottlenecks

1. Run `ism-agent-os list-docs --doctype "Repair Order" --fields-json '["name","customer","vehicle","status","approval_status","grand_total","outstanding_amount"]' --limit 50`.
2. Run `ism-agent-os action-items --status Open` to surface open shop floor discrepancies, inventory shortages, or escalations (and mark handled ones done with `ism-agent-os resolve-action`). Use `ism-agent-os vehicle-info --vehicle <VEH>` or `ism-agent-os get-inspection --repair-order <RO>` when drilling into specific vehicle history or walkaround completion.
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

- Cross-check bay and appointment counts against `ism-agent-os status` before presenting totals.

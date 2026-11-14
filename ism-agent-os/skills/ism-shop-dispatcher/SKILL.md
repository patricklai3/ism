---
name: ism-shop-dispatcher
description: >-
  Manages dual-resource bay and technician scheduling, floor capacity checks,
  unscheduled service detection, and ambient wallboard updates in Frappe ISM. Use when
  checking shop availability, booking appointments, or resolving scheduling bottlenecks.
---

# Shop Dispatcher Operations

Use this workflow to coordinate physical service bays, equipment tags, and technician capacity via the ISM Agent OS CLI (`python -m tools.cli --json ...` or `ism-agent-os ...`).

## 1. Inspect Floor Status and Unscheduled Work

1. Run `ism-agent-os status` (optionally `--date YYYY-MM-DD`, defaults to today) to view active bays and on-duty technicians.
2. If scheduling an existing Repair Order, run `ism-agent-os unscheduled` with `--repair-order` to confirm which operations still need a bay window. For returning vehicle history or plate/VIN lookups, run `ism-agent-os search-vehicles` or `ism-agent-os vehicle-info --vehicle <VEH>`.
3. Run `ism-agent-os tech-overview` with `--date` when you need per-technician utilization and unassigned entry details.

## 2. Find Collision-Free Slots & Estimate Duration

1. Run `ism-agent-os estimate` with `--repair-order` (or `--operations-json`) when you need probabilistic P80/P50 shop duration bounds before quoting.
2. Run `ism-agent-os slots` with `--repair-order` (or `--duration`), `--date`, `--days`, and optional `--tags` (e.g. `--tags Lift`), `--min-time`, `--include-past`, or `--include-resources`.
3. Review the returned `slots` list and top-level `resources` directory (`available_bays`, `available_bay_ids` ordered least-scarce first, `available_technicians`, `skipped_holidays`, and `bottleneck`).
4. Never guess bay availability or ignore lunch breaks. Only propose slots returned by `ism-agent-os slots`.

## 3. Book, Reschedule, or Transition Appointments

1. **Book**: Run `ism-agent-os book` with `--date`, `--time`, and either `--repair-order` or `--entry-type Diagnostic` with `--duration` and `--provisional-customer` / `--provisional-vehicle`, plus optional `--bay` or `--tech`.
2. **Reschedule**: Run `ism-agent-os reschedule` with `--schedule-entry`, updated `--date`/`--time`/`--bay`/`--tech` instead of cancelling and re-creating.
3. **Update Status**: Run `ism-agent-os appointment-status` with `--schedule-entry` and `--status` (`Confirmed`, `In Progress`, `Completed`, `No Show`, or `Cancelled`). Marking `No Show` or `Cancelled` releases bay/technician capacity and reopens services in `unscheduled`.
4. **Convert Walk-In / Diagnostic Entry**: Run `ism-agent-os create-ro-from-entry --schedule-entry <SE-...>` to create a linked `Checked In` Repair Order.
5. Check `warnings` in the response if a technician was double-booked or on approved leave, and confirm the Ambient Shop Calendar link updates cleanly.

## Verification

- Confirm booking/reschedule/status responses return `success: true` and a valid Schedule Entry ID.
- Verify the selected bay and technician match the open capacity reported by `ism-agent-os slots`.

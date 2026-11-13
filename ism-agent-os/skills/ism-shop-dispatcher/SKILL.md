---
name: ism-shop-dispatcher
description: >-
  Manages dual-resource bay and technician scheduling, floor capacity checks,
  unscheduled service detection, and ambient wallboard updates in Frappe ISM. Use when
  checking shop availability, booking appointments, or resolving scheduling bottlenecks.
---

# Shop Dispatcher Operations

Use this workflow to coordinate physical service bays, equipment tags, and technician capacity.

## 1. Inspect Floor Status and Unscheduled Work

1. Call `ism_get_shop_floor_status` for the target date (defaults to today) to view active bays and on-duty technicians.
2. If scheduling an existing Repair Order, call `ism_get_unscheduled_services` with `repair_order` to confirm which operations still need a bay window.
3. Call `ism_get_tech_overview` with `date` when you need per-technician utilization and unassigned entry details.

## 2. Find Collision-Free Slots

1. Call `ism_find_available_slots` with `repair_order` (or `duration_minutes`), `start_date`, `days_ahead`, and optional `equipment_tags`.
2. Review the returned slots table for `available_bays`, `available_technicians`, and any `bottleneck` flags.
3. Never guess bay availability or ignore lunch breaks. Only propose slots returned by `ism_find_available_slots`.

## 3. Book the Appointment

1. Call `ism_book_appointment` with `repair_order`, `scheduled_date`, `start_time`, and optional `service_bay` or `technician`.
2. Confirm the booking emits the "ism_calendar_update" realtime event to refresh ambient wall monitors.
3. Share the confirmed Schedule Entry ID (`SE-...`), bay, technician, and Ambient Shop Calendar link.

## Verification

- Confirm the booking response returns `success: true` and a valid Schedule Entry ID.
- Verify the selected bay and technician match the open capacity reported by `ism_find_available_slots`.

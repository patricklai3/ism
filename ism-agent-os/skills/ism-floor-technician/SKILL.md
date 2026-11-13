---
name: ism-floor-technician
description: >-
  Executes shop floor Repair Order transitions including starting bay work, placing jobs
  on hold for parts, and recording completion with odometer out. Use when a technician
  starts, pauses, or finishes work on a vehicle.
---

# Floor Technician Operations

Use this workflow to update active Repair Order status on the shop floor.

## 1. Start Work in Bay

1. Before entering the bay, inspect the Repair Order with `frappe_get_document` (`doctype="Repair Order"`) if status is unknown.
2. Call `ism_update_job_status` with `repair_order` and `action="start"`.
3. **Authorization Lock**: If the server rejects the call because `approval_status` is not `Approved` or `Partially Approved`, stop immediately. Notify the Service Advisor and run `ism_send_quote_for_approval` if instructed. Never bypass the authorization lock.

## 2. Place Job on Hold for Parts

1. When missing parts block progress, call `ism_update_job_status` with `repair_order`, `action="hold"`, and `notes` stating which part is needed.
2. Next, call `ism_check_parts_stock` with `ro_name` and notify the Parts Specialist if procurement is required.

## 3. Complete Work

1. Confirm all authorized operations and inspection notes are finished.
2. Ask the technician for the final `odometer_out` reading and completion summary.
3. Call `ism_update_job_status` with `repair_order`, `action="complete"`, `odometer_out`, and `notes`.
4. The server sets status to `Work Completed` and updates vehicle location to `Completed / Parked in Lot`.

## Verification

- Verify the returned `summary_card` displays the expected new status (`Authorized - In Progress`, `Waiting on Parts`, or `Work Completed`).

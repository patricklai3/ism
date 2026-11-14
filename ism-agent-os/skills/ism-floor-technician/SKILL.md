---
name: ism-floor-technician
description: >-
  Executes shop floor Repair Order transitions including starting bay work, placing jobs
  on hold for parts, and recording completion with odometer out. Use when a technician
  starts, pauses, or finishes work on a vehicle.
---

# Floor Technician Operations

Use this workflow to update active Repair Order status on the shop floor via the ISM Agent OS CLI (`python -m tools.cli --json ...` or `ism-agent-os ...`).

## 1. Check-In Inspection & Start Work in Bay

1. Before entering the bay, inspect the Repair Order with `ism-agent-os get-doc --doctype "Repair Order" --name <RO>` if status is unknown, and check walkaround items with `ism-agent-os get-inspection --repair-order <RO>` (or apply a checklist with `ism-agent-os apply-inspection --repair-order <RO>` after listing `ism-agent-os inspection-templates`).
2. Record check-in findings (`Pass`, `Attention`, `Fail`, `N/A`), notes, odometer, or concern via `ism-agent-os record-inspection --repair-order <RO> --items-json <JSON>`.
3. Run `ism-agent-os job-status --repair-order <RO> --action start`.
4. **Authorization Lock**: If the server rejects the call because `approval_status` is not `Approved` or `Partially Approved`, stop immediately. Notify the Service Advisor and run `ism-agent-os send-approval` if instructed. Never bypass the authorization lock.

## 2. Record Findings, Supplements, or Parts Holds

1. When missing parts block progress, run `ism-agent-os job-status --repair-order <RO> --action hold --notes "<part needed>"`.
2. Next, run `ism-agent-os check-stock --repair-order <RO>` and notify the Parts Specialist if procurement is required.
3. If teardown inspection discovers additional repairs needed, add a mid-job supplement via `ism-agent-os add-supplement` (`--repair-order`, `--title`, `--hours`, `--parts-json`, `--notes`)—this creates an `Estimating` row so the Service Advisor can send `ism-agent-os send-approval` before repair execution.
4. If a floor discrepancy or safety blocker requires advisor/manager follow-up, log it via `ism-agent-os create-action`.

## 3. Complete Services & Park / Finish Work

1. To check off approved operations while continuing bay work, run `ism-agent-os toggle-service --repair-order <RO> --service-row <ROW1> [<ROW2> ...]` in a single batched command (enforces the authorization lock per row).
2. To return the vehicle to the lot temporarily between operations, run `ism-agent-os job-status --repair-order <RO> --action park`.
3. Confirm all authorized operations and inspection notes are finished, ask for the final `odometer_out` reading and completion summary, and run `ism-agent-os job-status --repair-order <RO> --action complete --odometer-out <KM> --notes "<summary>"`.
4. The server sets status to `Work Completed` and updates vehicle location to `Completed / Parked in Lot`.
5. For customer billing and handover after operator confirmation, run `ism-agent-os checkout --repair-order <RO> --confirm` to invoice, collect payment, and release the vehicle cleanly.

## Verification

- Verify `data.status` displays the expected new status (`Authorized - In Progress`, `Waiting on Parts`, or `Work Completed`).

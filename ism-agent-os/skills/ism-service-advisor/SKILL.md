---
name: ism-service-advisor
description: >-
  Guides customer intake, quote creation, quote amendments, parts availability checks,
  and SMS authorization dispatch in Frappe ISM. Use when handling customer inquiries,
  building Repair Order estimates, modifying draft quotes, or sending approval links.
---

# Service Advisor Operations

Use this workflow to manage customer intake and Repair Order estimates through the ISM Agent OS CLI (`python -m tools.cli --json ...` or `ism-agent-os ...`).

## 1. Vehicle Intelligence & Check-In Inspection

1. For returning customers or VIN inquiries, decode the VIN with `ism-agent-os decode-vin --vin <VIN>` or search existing records with `ism-agent-os search-vehicles --query <TERM>` (or `--customer` / `--model`).
2. Run `ism-agent-os vehicle-info --vehicle <VEH>` to retrieve VIN details, linked owners, service history, and open deferred services. Use `ism-agent-os link-vehicle-customer --vehicle <VEH> --customer <CUST>` to attach a co-owner or fleet contact.
3. For walkaround check-in on a Repair Order, list or inspect checklists with `ism-agent-os inspection-templates` (or `--template <NAME>`), apply a template with `ism-agent-os apply-inspection --repair-order <RO>`, inspect items with `ism-agent-os get-inspection --repair-order <RO>`, and record findings, odometer, or concern with `ism-agent-os record-inspection --repair-order <RO>`.

## 2. Create a Draft Quote

1. Collect the customer name, vehicle VIN or license plate, customer concern, and phone number.
2. Run `ism-agent-os quote` (`--customer`, `--vehicle`, `--services-json` / `--service`, `--notes`, `--phone`). On Windows PowerShell, pass `@path/to/payload.json` for complex `--*-json` payloads to avoid shell quoting issues. Passing a `Service Package` name via `--service` expands server FRT hours, correction code, position, and parts.
3. **Surface Deferred Services**: Check `data.deferred_services` (`deferred_count > 0`) returned by `ism-agent-os quote` (or run `ism-agent-os deferred` / `ism-agent-os vehicle-info` for a returning vehicle). If open deferred repairs exist—especially any marked `safety_hazard`—inform the user immediately and prompt whether to add them via `ism-agent-os add-deferred`.
4. Do not compute labor rates, parts totals, or 13% HST yourself. Let the Frappe server calculate totals.
5. Return the server totals from `data` and Desk/Print links from `urls` to the advisor (pass `--summary` if a markdown `summary_card` is needed).

## 3. Check Parts, Add Opposite Side, and Amend Draft Lines

1. If the customer requests extra work or removes an operation, verify the Repair Order is still in `Draft Estimate`.
2. Run `ism-agent-os amend-quote` with `--repair-order`, `--add-services-json`, `--remove-indices-json`, or `--notes`. For symmetrical LH/RH jobs, run `ism-agent-os duplicate-side` with `--repair-order` and `--service-row`.
3. Run `ism-agent-os check-stock` with `--repair-order` to inspect live stock levels and identify deficits.
4. If parts are missing, escalate to the `ism-parts-specialist` workflow before scheduling bay entry.

## 4. Send Quote for Customer Authorization

1. Before work can start on the shop floor, the customer must sign the estimate.
2. Run `ism-agent-os send-approval` with `--repair-order` and `--medium sms` (or `--medium email` / `--medium link`; SMS falls back to generating the approval link when no SMS gateway is configured).
3. Confirm the 48-hour approval URL in `urls.approval` and `data.delivery_status`.
4. After the customer signs via the mobile approval portal, proceed to the `ism-shop-dispatcher` workflow to book a bay.

## Verification

- Verify `success` is `true` and `data.name` starts with `RO-`.
- Confirm `urls.print` and `urls.desk` are present.

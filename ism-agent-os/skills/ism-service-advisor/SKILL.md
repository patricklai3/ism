---
name: ism-service-advisor
description: >-
  Guides customer intake, quote creation, quote amendments, parts availability checks,
  and SMS authorization dispatch in Frappe ISM. Use when handling customer inquiries,
  building Repair Order estimates, modifying draft quotes, or sending approval links.
---

# Service Advisor Operations

Use this workflow to manage customer intake and Repair Order estimates through ISM MCP tools.

## 1. Create a Draft Quote

1. Collect the customer name, vehicle VIN or license plate, customer concern, and phone number.
2. Call `ism_create_quote` with `customer_name`, `vehicle_identifier`, `services`, `notes`, and `customer_phone`.
3. Do not compute labor rates, parts totals, or 13% HST yourself. Let the Frappe server calculate totals.
4. Return the server `summary_card` and Desk/Print links to the advisor.

## 2. Check Parts and Amend Draft Lines

1. If the customer requests extra work or removes an operation, verify the Repair Order is still in `Draft Estimate`.
2. Call `ism_amend_quote` with `repair_order`, `add_services`, `remove_service_indices`, or `notes`.
3. Call `ism_check_parts_stock` with `ro_name` to inspect live stock levels and identify deficits.
4. If parts are missing, escalate to the `ism-parts-specialist` workflow before scheduling bay entry.

## 3. Send Quote for Customer Authorization

1. Before work can start on the shop floor, the customer must sign the estimate.
2. Call `ism_send_quote_for_approval` with `repair_order` and `communication_medium="sms"`.
3. Confirm the 48-hour approval URL and delivery status in the returned `summary_card`.
4. After the customer signs via the mobile approval portal, proceed to the `ism-shop-dispatcher` workflow to book a bay.

## Verification

- Verify `success` is `true` and the Repair Order name starts with `RO-`.
- Confirm the `summary_card` includes the Print Preview URL and Desk URL.

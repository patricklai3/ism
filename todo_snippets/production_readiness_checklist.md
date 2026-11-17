# Production Readiness Backlog — Swarm Execution Plan

All 12 production-readiness items are partitioned into **2 sequential Swarm Runs** (3–4 parallel workers + 1 verifier per swarm).

**Why 2 swarms instead of 1:**
1. **Zero file collisions:** Within each swarm, every worker owns a strictly disjoint set of `.py`, `.js`, and `.json` files (including separate test files). No two workers edit the same file.
2. **Schema gate:** Swarm 1 lands all DocType `.json` schema fields, roles, pricing math, alerts, and Tesla VIN/manual backend updates. Run `bench migrate` once after Swarm 1, then run Swarm 2 for billing/ledger hooks, Desk UI modals/buttons, print format cleanup, and `ism-agent-os` CLI parity.

---

## Swarm 1 — Schema, Auth Locks, Pricing, Alerts & Tesla Core
**Swarm size:** 4 parallel workers (`workspace: "share"`) + 1 `verifier`

### Shared Field Contract (Locked Before Workers Start)
- **`Repair Order Service` (`repair_order_service.json`):**
  - `authorized_amount` (`Currency`, `read_only: 1`) — snapshot of `total_amount` when row becomes `Approved`.
  - `discount_amount` (`Currency`, default `0`).
- **`Repair Order` (`repair_order.json`):**
  - `discount_amount` (`Currency`, default `0` — order-level pre-tax discount).
  - `shop_supplies_amount` (`Currency`, default `0`).
  - `environmental_fee_amount` (`Currency`, default `0` — tire/battery/coolant disposal).
  - `deposit_amount` (`Currency`, default `0`, `read_only: 1`).
- **`ISM Settings` (`ism_settings.json`):**
  - `shop_supplies_percentage` (`Percent`, default `0`), `shop_supplies_cap` (`Currency`, default `0`), `enable_ready_for_pickup_sms` (`Check`, default `0`).
- **`s.parts` JSON item convention:** `part_source == "Customer Supplied"` forces `rate = 0.0`, `amount = 0.0`.

---

### Worker 1: `schema-auth-roles`
- **Owns:**
  - `ism-frappe/ism/ism/doctype/repair_order/repair_order.json`
  - `ism-frappe/ism/ism/doctype/repair_order_service/repair_order_service.json`
  - `ism-frappe/ism/ism/doctype/ism_settings/ism_settings.json`
  - `ism-frappe/ism/ism/doctype/repair_vehicle/repair_vehicle.json`
  - `ism-frappe/ism/ism/doctype/service_package/service_package.json`
  - `ism-frappe/ism/setup_tasks/seed.py`
  - `ism-frappe/ism/ism/doctype/repair_order/authorization.py`
  - `ism-frappe/ism/tests/test_auth_and_roles.py` *(new)*
- **Do Not Touch:** `pricing.py`, `billing.py`, `fulfillment.py`, `approval.py`, `repair_order.js`.
- **Tasks:**
  1. **Schema Fields:** Add the Shared Field Contract fields above to `repair_order_service.json`, `repair_order.json`, and `ism_settings.json`.
  2. **Signed Estimate Lock & Cost Tolerance (Item 7):** In `authorization.py`, snapshot `s.authorized_amount = flt(s.total_amount, 2)` when `authorize_repair_order_jobs` marks a row `Approved`. In `validate_authorization_guards`, if an `Approved` row's `total_amount` exceeds `authorized_amount` beyond `ISM Settings` (`cost_tolerance_percentage` / `cost_tolerance_flat_amount`), demote that row to `Pending Approval` and recompute `ro.approval_status` (`Partially Approved` / `Pending Approval`).
  3. **Advisor vs. Technician Role Split (Item 10):** Seed `ISM Service Advisor` and `ISM Technician` in `seed.py` (`ensure_ism_roles`). Add `ISM Manager`, `ISM Service Advisor`, and `ISM Technician` permissions across the owned DocType JSONs, using `permlevel: 1` on financial/authorization fields (`labor_rate`, `labor_amount`, `discount_amount`, `tax_rate`, `taxes_and_charges`, `approval_status`) so technicians can only update inspections, bay location, time logs, and completion flags.

### Worker 2: `pricing-packages`
- **Owns:**
  - `ism-frappe/ism/ism/doctype/repair_order/pricing.py`
  - `ism-frappe/ism/tests/test_pricing_and_discounts.py` *(new)*
- **Do Not Touch:** `*.json`, `billing.py`, `authorization.py`, `repair_order.js`.
- **Tasks:**
  1. **Server-Side Package Auto-Expansion (Item 1 — Backend):** In `calculate_service_totals`, if a row has `s.service_package` set and `s.parts` is empty (`None` or `""` on a newly added Desk row), auto-expand package parts, `correction_code`, `labor_hours`, and `labor_rate` using `build_service_package_row_fields` / `_expand_package_parts` without clobbering explicit user overrides.
  2. **Customer-Supplied Parts, Discounts & Shop Fees (Item 8 — Pricing):**
     - When a part in `s.parts` has `part_source == "Customer Supplied"`, enforce `rate = 0.0` and `amount = 0.0`.
     - Apply row `discount_amount` (via `getattr(s, "discount_amount", 0)`) to `s.total_amount` (clamped `>= 0`).
     - In `calculate_taxes_and_totals`, apply order-level `discount_amount`, auto-compute `shop_supplies_amount` when `shop_supplies_percentage > 0` (capped at `shop_supplies_cap`), include `environmental_fee_amount` in `net_total`, and compute tax and `grand_total`.

### Worker 3: `alerts-portal`
- **Owns:**
  - `ism-frappe/ism/api/approval.py`
  - `ism-frappe/ism/ism/doctype/repair_order/fulfillment.py`
  - `ism-frappe/ism/tests/test_approval_and_inspection.py`
- **Do Not Touch:** `authorization.py`, `billing.py`, `hooks.py`, `repair_order.js`.
- **Tasks:**
  1. **Guest Portal Rate Limiting (Item 11a):** Add Redis/cache-backed per-IP and per-token rate limiting on `get_public_quote_approval` and `submit_customer_approval` in `approval.py` to block brute-force/replay abuse.
  2. **Shop Floor Realtime & Action Item Alerts (Item 11b):**
     - On `submit_customer_approval`, emit `frappe.publish_realtime` (`ism_ro_approval_updated`) and create/update a `Shop Action Item` notifying the advisor when a customer approves or declines.
     - In `fulfillment.py` (`_process_stock_update_event`), emit `frappe.publish_realtime` and create a `Shop Action Item` when incoming stock flips an RO from `Waiting on Parts` to `Authorized - In Progress`.
     - Trigger optional "Vehicle Ready for Pickup" SMS (`dispatch_sms`) when RO transitions to `Work Completed` and `enable_ready_for_pickup_sms` is enabled.

### Worker 4: `tesla-vin-manual`
- **Owns:**
  - `ism-frappe/ism/ism/doctype/repair_vehicle/vin_decoder.py`
  - `ism-frappe/ism/api/tesla.py`
  - `ism-frappe/ism/ism/doctype/repair_vehicle/test_repair_vehicle.py`
  - `ism-frappe/ism/tests/test_tesla_integration.py`
- **Do Not Touch:** `repair_order.js`, `pricing.py`.
- **Tasks:**
  1. **2024–2026 Tesla VIN Hardening (Item 12a):** Update `get_hardware_generation` in `vin_decoder.py` so `year >= 2024` returns `{"t": "HW4"}` for Model S and Model X (matching Model 3/Y), and verify/add 2025–2026 Model Y Juniper drive unit codes in `DRIVE["Y"]` and `_AWD_DRIVE_CODES["Y"]`.
  2. **Offline/Paste Procedure Fallback (Item 12b — Backend):** Extend `import_tesla_procedure` in `tesla.py` to accept `raw_html` or `raw_markdown` directly (parsing via `parse_tesla_procedure_html` / `parse_tesla_procedure_markdown`) so Desk/CLI users can import procedures when `service.tesla.com` blocks server-side `urllib` fetches.

---

## Swarm 2 — Billing/Ledger Engine, Desk UI & Agent CLI Parity
*Run after Swarm 1 merges and `bench migrate` completes.*
**Swarm size:** 3 parallel workers (`workspace: "share"`) + 1 `verifier`

### Shared Method Contract (Locked Before Workers Start)
- `RepairOrder.record_deposit(amount, payment_method="Debit / Interac", reference_no=None)` -> whitelisted on `RepairOrder`; creates submitted advance `Payment Entry`, increments `ro.deposit_amount` and `ro.paid_amount`, updates `ro.outstanding_amount` / `ro.payment_status`.
- `RepairOrder.process_payment_and_release(payment_method="Debit / Interac", paid_amount=None, odometer_out=None, payments=None, **kwargs)` -> optional `payments` accepts `list[{"payment_method": str, "amount": float}]` for split tenders and auto-reconciles any unallocated advance `Payment Entry` deposits for that customer/RO.
- `ism.api.agent.amend_quote(..., update_services=None)` -> accepts list of `{service_row_id | idx, labor_hours, labor_rate, discount_amount, parts, advisor_notes}` for in-place draft edits.

---

### Worker 1: `billing-ledger`
- **Owns:**
  - `ism-frappe/ism/ism/doctype/repair_order/billing.py`
  - `ism-frappe/ism/ism/doctype/repair_order/repair_order.py`
  - `ism-frappe/ism/hooks.py`
  - `ism-frappe/ism/tests/test_billing_and_deposits.py` *(new)*
- **Do Not Touch:** `pricing.py`, `authorization.py`, `agent_quotes.py`, `repair_order.js`.
- **Tasks:**
  1. **`Sales Invoice` & `Payment Entry` Lifecycle Hooks (Item 4):** Wire `doc_events` in `hooks.py` for `Sales Invoice` (`on_cancel`) and `Payment Entry` (`on_submit`, `on_cancel`). Unlink `ro.sales_invoice` and reopen billing state when an invoice is cancelled; recompute `ro.paid_amount`, `ro.outstanding_amount`, and `ro.payment_status` whenever a `Payment Entry` against `ro.sales_invoice` is submitted or cancelled.
  2. **Upfront Deposits & Split Payments (Item 5 — Backend):** Implement `record_ro_deposit` in `billing.py` (exposed as `@frappe.whitelist() def record_deposit` on `RepairOrder`), allocate prior deposits onto the `Sales Invoice` at checkout, and support multi-tender `payments` splits in `process_ro_payment_and_release`.
  3. **Pre-Checkout Stock Guard & Billing Edge Cases (Items 6 & 8 — Billing):**
     - Run a warehouse stock availability pre-check on `Approved` stock parts before creating the `Sales Invoice`, throwing an actionable deficit summary instead of raw ERPNext `NegativeStockError`.
     - For `part_source == "Customer Supplied"`, omit warehouse stock deduction (`is_stock_item=0` service/note row or skip stock ledger impact) and omit part warranty disclosure tag.
     - Carry `discount_amount`, `shop_supplies_amount`, and `environmental_fee_amount` onto the generated `Sales Invoice` so `si.grand_total` matches `ro.grand_total`.

### Worker 2: `desk-ui-print`
- **Owns:**
  - `ism-frappe/ism/ism/doctype/repair_order/repair_order.js`
  - `ism-frappe/ism/ism/print_format/repair_order_invoice/repair_order_invoice.json`
- **Do Not Touch:** `*.py`, `repair_order.json`, `repair_order_service.json`.
- **Tasks:**
  1. **Nested Parts Modal & Package Auto-Fill in Desk (Item 1 — UI):** Add `frappe.ui.form.on("Repair Order Service", ...)` handler in `repair_order.js` to fetch `get_service_package_details` on `service_package` selection, plus a "Manage Parts" dialog on each service row showing part code, qty, rate, `part_source` (`OEM` / `Aftermarket` / `Customer Supplied`), live FIFO stock badge (`get_repair_order_parts_stock`), and alternate switcher (`get_component_alternates`).
  2. **Desk Workflow Action Buttons (Item 2):** Add status-gated Desk buttons & dialogs for:
     - **Checkout & Release** (`process_payment_and_release` supporting single or split tenders + `odometer_out`) and **Record Deposit** (`record_deposit`)
     - **In-Person / Verbal Approval** (`authorize_jobs`)
     - **Add Supplement** (`add_supplement`), **Duplicate Opposite Side** (`duplicate_service_for_opposite_side`), and **Import Tesla Procedure** (URL or HTML/Markdown paste fallback for Item 12b)
     - **Apply Inspection Template** (`apply_inspection_template`), **Pick Up / Park Vehicle** (`pick_up_vehicle` / `park_vehicle`), and **Cancel Repair Order** (`cancel_repair_order`).
  3. **Supplement Re-Approval Button Fix (Item 3):** Display `Send for Customer Authorization` whenever `frm.doc.services` has any row in `["Draft", "Estimating", "Pending Approval"]`, even when `approval_status` is `"Partially Approved"`.
  4. **Invoice Print Format Cleanup (Item 9b):** In `repair_order_invoice.json`, replace hardcoded `"Ontario HST"` with dynamic tax template/description label, render discount/shop-supplies/disposal fee rows when non-zero, and render `<img src="{{ doc.signature_image }}">` in the signature block when present.

### Worker 3: `agent-cli-parity`
- **Owns:**
  - `ism-frappe/ism/api/agent_quotes.py`
  - `ism-frappe/ism/api/agent.py`
  - `ism-agent-os/tools/client.py`
  - `ism-agent-os/tools/cli_commands.py`
  - `ism-agent-os/tools/cli_parsers.py`
  - `ism-frappe/ism/tests/test_agent_facade.py`
  - `ism-agent-os/tests/test_client_and_cli.py`
- **Do Not Touch:** `billing.py`, `pricing.py`, `repair_order.js`.
- **Tasks:**
  1. **In-Place Quote Amendments (Item 9a):** Extend `amend_quote` in `agent_quotes.py` / `agent.py` and `ism-agent-os` (`amend-quote`) with `update_services` support to modify `labor_hours`, `labor_rate`, `discount_amount`, `parts`, and `advisor_notes` on existing unapproved rows by `service_row_id` or `idx`, plus order-level discount/fee fields.
  2. **Deposits & Split Checkout in Agent OS (Item 5 — API/CLI):** Add `record_ro_deposit` (`deposit` CLI subcommand) and `payments` split-tender support to `checkout_repair_order` (`checkout` CLI subcommand) across `agent_quotes.py`, `agent.py`, `IsmAgentClient`, and CLI parsers/tests.

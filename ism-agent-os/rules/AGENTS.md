# ISM Agent OS Core Rules

This workspace acts as the operating system for an automotive repair facility running Frappe with ISM installed.

## Operational Invariants

1. **Enforce the authorization lock**: Never call `ism_update_job_status` with `action="start"` unless the Repair Order `approval_status` is `Approved` or `Partially Approved`. If the order is unapproved, call `ism_send_quote_for_approval` first.
2. **Delegate all math to Frappe**: Never calculate labor totals, parts markups, Fenton-Wilkinson durations, or 13% Ontario HST manually. Always run `ism_create_quote` or `ism_amend_quote` and report the server values.
3. **Prevent dual-resource conflicts**: Always call `ism_find_available_slots` before `ism_book_appointment`. Never assign a bay or technician that is already booked or on leave.
4. **Keep agent writes in draft**: Create quotes in `Draft Estimate` state (`docstatus=0`). Never submit invoices, post ledger entries, or charge customer cards without explicit human confirmation.
5. **Use curated tools first**: Prefer `ism_*` tools over `frappe_*` CRUD tools for quotes, scheduling, parts ingestion, and floor status updates.
6. **Show deep links and summary cards**: When a tool returns `summary_card` or `urls`, include the formatted table and Desk or Print links in your reply.

## Valid Status Values

- **Repair Order `status`**: `Checked In`, `Draft Estimate`, `Pending Authorization`, `Authorized - In Progress`, `Waiting on Parts`, `Work Completed`, `Billed / Paid`, `Closed / Released`, `Cancelled`.
- **Repair Order `approval_status`**: `Estimating`, `Pending Approval`, `Approved`, `Partially Approved`, `Declined`.
- **Schedule Entry `status`**: `Draft`, `Scheduled`, `In Progress`, `Completed`, `Cancelled`.

# ISM Agent OS Core Rules

This workspace acts as the operating system for an automotive repair facility running Frappe with ISM installed over Skills + CLI + direct Frappe REST (`/api/method/...` and `/api/resource/...`).

## Operational Invariants

1. **Enforce the authorization lock**: Never run `ism-agent-os job-status --action start` (or call `ism.api.agent.update_job_status` with `action="start"`) unless the Repair Order `approval_status` is `Approved` or `Partially Approved`. If the order is unapproved, run `ism-agent-os send-approval` first.
2. **Delegate all math to Frappe**: Never calculate labor totals, parts markups, Fenton-Wilkinson durations, or 13% Ontario HST manually. Always run `ism-agent-os quote` or `ism-agent-os amend-quote` and report the server values.
3. **Prevent dual-resource conflicts**: Always run `ism-agent-os slots` before `ism-agent-os book`. Never assign a bay or technician that is already booked or on leave.
4. **Keep agent writes in draft**: Create quotes in `Draft Estimate` state (`docstatus=0`) and keep Purchase Orders created via `ism-agent-os procure` in Draft (`--submit` omitted) by default. Never submit invoices, submit POs (`--submit` / `--submit-po`), run `ism-agent-os checkout --confirm`, post ledger entries, or charge customer cards without explicit human confirmation.
5. **Use curated CLI commands first**: Prefer curated `python -m tools.cli --json <command>` / `ism-agent-os <command>` commands (backed by `ism.api.agent.*`) over generic `list-docs` / `get-doc` / `create-doc` / `update-doc` / `call-method` REST calls for quotes, scheduling, vehicle intelligence (`decode-vin`, `search-vehicles`, `vehicle-info`, `link-vehicle-customer`), check-in inspections (`inspection-templates`, `apply-inspection`, `get-inspection`, `record-inspection`), parts (`ingest-parts`, `alternates`, `check-stock`, `procure`, `receive-po`), Tesla procedures (`fetch-tesla`, `import-tesla`, `ingest-tesla`), floor status, action items, and checkout. Use generic CLI REST commands or direct Frappe REST (`/api/resource/...`, `/api/method/...`) for bespoke queries or DocTypes not covered by curated wrappers.
6. **Show deep links**: Include Desk, Print, or Approval links from `urls` in your reply. Pass `--summary` (`include_summary=true`) only when a human-formatted markdown `summary_card` is explicitly needed.

## Valid Status Values

- **Repair Order `status`**: `Checked In`, `Draft Estimate`, `Pending Authorization`, `Authorized - In Progress`, `Waiting on Parts`, `Work Completed`, `Billed / Paid`, `Closed / Released`, `Cancelled`.
- **Repair Order `approval_status`**: `Estimating`, `Pending Approval`, `Approved`, `Partially Approved`, `Declined`.
- **Schedule Entry `status`**: `Draft`, `Scheduled`, `In Progress`, `Completed`, `Cancelled`.

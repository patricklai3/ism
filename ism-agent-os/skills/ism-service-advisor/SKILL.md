---
name: ism-service-advisor
description: >-
  Guides customer intake, quote creation, quote amendments, parts availability checks,
  and SMS authorization dispatch in Frappe ISM. Use when handling customer inquiries,
  building Repair Order estimates, modifying draft quotes, or sending approval links.
---

# Service Advisor Operations

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. The table below lists exact CLI flags—call `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly on turn 1 without redundant pre-checks (`status`, `slots`, `tech-overview`, `search-vehicles`, `estimate`). Target 1-2 CLI turns for simple requests and 3 max for multi-step workflows.

## Direct Intent-to-Command Routing

| User Intent | Direct Command |
| :--- | :--- |
| Decode 17-char Tesla VIN | `python -m tools.cli --json decode-vin --vin <VIN>` |
| Search vehicles by VIN, plate, customer, or model | `python -m tools.cli --json search-vehicles [--query <TERM>] [--customer <NAME>] [--model <MODEL>]` |
| Full vehicle profile, owners, history, deferred work | `python -m tools.cli --json vehicle-info --vehicle <VEH>` |
| Link co-owner or fleet contact to vehicle | `python -m tools.cli --json link-vehicle-customer --vehicle <VEH> --customer <CUST> [--primary] [--relationship <REL>]` |
| Create Draft Estimate Repair Order | `python -m tools.cli --json quote --customer <NAME> --vehicle <VEH> [--service "Title::0.5"] [--parts-json @parts.json] [--notes <TEXT>] [--phone <NUM>] [--email <ADDR>] [--odometer-in <KM>]` |
| Create Draft Quote + return filtered slots in one call | `python -m tools.cli --json intake-slots --customer <NAME> --vehicle <VEH> [--service "Title::0.5"] [--date <YYYY-MM-DD>] [--days N] [--duration <MIN>] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>]` |
| Amend draft quote lines or notes | `python -m tools.cli --json amend-quote --repair-order <RO> [--add-service "Title::0.3"] [--remove-indices-json @indices.json] [--notes <TEXT>]` (combine with `send-approval` via `batch` if sending right after) |
| Probabilistic P80/P50 job duration estimate for an RO | `python -m tools.cli --json estimate --repair-order <RO>` |
| Duplicate symmetrical LH/RH service row | `python -m tools.cli --json duplicate-side --repair-order <RO> --service-row <ROW>` |
| List or add open deferred services | `python -m tools.cli --json deferred [--repair-order <RO> \| --vehicle <VEH>]` / `python -m tools.cli --json add-deferred --repair-order <RO> --item <ITEM>` |
| Check live parts stock for an RO | `python -m tools.cli --json check-stock --repair-order <RO>` |
| Send quote for customer approval | `python -m tools.cli --json send-approval --repair-order <RO> [--medium sms\|email\|link] [--phone <NUM>] [--recipient <ADDR>]` (combine with `amend-quote` via `batch` if amending first) |
| Check-in inspection templates / apply / read / record | `python -m tools.cli --json inspection-templates [--template <NAME>]`, `apply-inspection`, `get-inspection`, `record-inspection` |
| Multi-step advisor flow in one process | `python -m tools.cli --json batch --cmd "amend-quote --repair-order <RO> --add-service '<NAME>::<HRS>'" --cmd "send-approval --repair-order <RO> --medium link"` (alias: `ism-agent-os batch`) |

## Routing Rules

1. **Zero `--help` or redundant pre-checks**: Use the exact flags in the table above on turn 1. When customer, vehicle, and service/concern are known, run `quote` directly—do not pre-run `--help`, `search-vehicles`, `status`, or `estimate`.
2. **Quote-free `--service` & `--add-service` (NEVER pass inline JSON on PowerShell)**:
   - On `quote` and `intake-slots`, pass repeatable `--service "Title"` (freeform title or exact Service Package) or `--service "Title::0.5"` to specify explicit labor hours (e.g., `--service "Brake Fluid Flush::1.0"`).
   - On `amend-quote`, pass repeatable `--add-service "Title::0.3"` (e.g., `--add-service "Wiper Blade Replacement::0.3"`).
   - Use `@path/to/payload.json` only if you need `--parts-json` (`[{"item_code":"...","qty":1}]`) or `--remove-indices-json` (`[0]`); never pass inline JSON strings on PowerShell.
3. **One-call `intake-slots` & `batch --cmd`**: `intake-slots` creates the draft quote and returns filtered slots directly (no follow-up `slots` call needed). ALWAYS combine chained steps (`amend-quote` + `send-approval`, or `decode-vin` + `vehicle-info`) into one `batch` call (`python -m tools.cli --json batch --cmd "amend-quote ..." --cmd "send-approval ... --medium link"` or `python -m tools.cli --json batch --cmd "decode-vin --vin <VIN>" --cmd "vehicle-info --vehicle <VIN>"`) — do **NOT** run `amend-quote` + `send-approval` or `decode-vin` + `vehicle-info` as separate CLI calls. Also use `python -m tools.cli --json batch --cmd "..." --cmd "..."` for other chained calls, wrapping multi-word inner values in single quotes (`'...'`).
4. **Surface deferred services**: Check `data.deferred_services` (`deferred_count > 0`) returned by `quote` or `vehicle-info`. Highlight any `safety_hazard` items immediately and offer `add-deferred`.
5. **Server math & links**: Never compute labor rates, parts totals, or 13% HST manually. Return server totals from `data` and Desk/Print/Approval links from `urls` (pass `--summary` only if a markdown `summary_card` is requested).

## Required Fields & Pre-Dispatch Callback

- **`quote` / `intake-slots` required fields**: `--customer`, `--vehicle`, `--odometer-in` (`>= 0`; NEVER guess or default), and at least one `--service`/`--services-json`/`--notes`.
- **Mandatory Callback**: Before executing any write (`quote`, `intake-slots`, `amend-quote`, `send-approval`, `add-deferred`), run read-only checks or `--dry-run`, output `### ISM Pre-Dispatch Summary` (marking missing fields `[MISSING — REQUIRED]`), and wait for explicit user confirmation. When planning `quote -> amend-quote + send-approval`, explicitly list `quote`, `amend-quote`, and `send-approval` in the Pre-Dispatch Summary and plan step 2 as one `batch` call (`--cmd "amend-quote ..." --cmd "send-approval ..."`), so Turn 2 executes `batch` in a single CLI call (and passes the write-gate hook).

## Verification

- Verify `success: true`, `data.name` (`RO-...`), and `urls.print` / `urls.desk` (or `urls.approval` for `send-approval`).

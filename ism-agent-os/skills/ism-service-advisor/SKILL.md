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
| Decode 17-char Tesla VIN (optional B-pillar date for mid-year gen split) | `python -m tools.cli --json decode-vin --vin <VIN> [--manufactured-month <MM/YY>]` |
| List canonical Tesla generations & manual mappings | `python -m tools.cli --json tesla-generations [--model <MODEL>]` |
| Search vehicles by VIN, plate, customer, model, or generation | `python -m tools.cli --json search-vehicles [--query <TERM>] [--customer <NAME>] [--model <MODEL>] [--generation <GEN>]` |
| Full vehicle profile, owners, history, deferred work | `python -m tools.cli --json vehicle-info --vehicle <VEH>` |
| Link co-owner or fleet contact to vehicle | `python -m tools.cli --json link-vehicle-customer --vehicle <VEH> --customer <CUST> [--primary] [--relationship <REL>]` |
| Create Draft Estimate Repair Order | `python -m tools.cli --json quote --customer <NAME> --vehicle <VEH> [--color <COLOR>] [--license-plate <PLATE>] [--service "Title::0.5"] [--parts-json @parts.json] [--notes <TEXT>] [--phone <NUM>] [--email <ADDR>] [--odometer-in <KM>]` |
| Create Draft Quote + return filtered slots in one call | `python -m tools.cli --json intake-slots --customer <NAME> --vehicle <VEH> [--color <COLOR>] [--license-plate <PLATE>] [--service "Title::0.5"] [--date <YYYY-MM-DD>] [--days N] [--duration <MIN>] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>]` |
| Amend draft quote lines or notes | `python -m tools.cli --json amend-quote --repair-order <RO> [--add-service "Title::0.3"] [--remove-indices-json @indices.json] [--notes <TEXT>]` (combine with `send-approval` via `batch` if sending right after) |
| Probabilistic P80/P50 job duration estimate for an RO | `python -m tools.cli --json estimate --repair-order <RO>` |
| Duplicate symmetrical LH/RH service row | `python -m tools.cli --json duplicate-side --repair-order <RO> --service-row <ROW>` |
| List or add open deferred services | `python -m tools.cli --json deferred [--repair-order <RO> \| --vehicle <VEH>]` / `python -m tools.cli --json add-deferred --repair-order <RO> --item "<JOB_TITLE>"` (accepts deferred `job_title` or row `name`; prefer exact `job_title` from `deferred`/`vehicle-info`) |
| Check live parts stock for an RO | `python -m tools.cli --json check-stock --repair-order <RO>` |
| Calculate Tesla-offset part sell price (CAD) | `python -m tools.cli --json price-part [--tesla-price <CAD>] [--cost <CAD>] [--part-number <NO>] [--part-name <NAME>]` |
| Send quote for customer approval | `python -m tools.cli --json send-approval --repair-order <RO> [--medium sms\|email\|link] [--phone <NUM>] [--recipient <ADDR>]` (combine with `amend-quote` via `batch` if amending first) |
| Record customer deposit on an RO | `python -m tools.cli --json deposit --repair-order <RO> --amount <CAD> --payment-method <METHOD> [--reference-no <REF>]` (or chain after `quote` in `batch`: `--repair-order @step0.data.name`) |
| Check-in inspection templates / apply / read / record | `python -m tools.cli --json inspection-templates [--template <NAME>]`, `apply-inspection`, `get-inspection`, `record-inspection` |
| Multi-step advisor flow in one process | `python -m tools.cli --json batch --cmd "amend-quote --repair-order <RO> --add-service '<NAME>::<HRS>'" --cmd "send-approval --repair-order <RO> --medium link"` (alias: `ism-agent-os batch`) |

## Routing Rules

1. **Zero `--help` or redundant pre-checks**: Use the exact flags in the table above on turn 1. When customer, vehicle, and service/concern are known, run `quote` directly—do not pre-run `--help`, `search-vehicles`, `status`, or `estimate`.
2. **Quote-free `--service` & `--add-service` (NEVER pass inline JSON on PowerShell)**:
   - On `quote` and `intake-slots`, pass repeatable `--service "Title"` (freeform title or exact Service Package) or `--service "Title::0.5"` to specify explicit labor hours (e.g., `--service "Brake Fluid Flush::1.0"`).
   - On `amend-quote`, pass repeatable `--add-service "Title::0.3"` (e.g., `--add-service "Wiper Blade Replacement::0.3"`).
   - Use `@path/to/payload.json` only if you need `--parts-json` (`[{"item_code":"...","qty":1}]`) or `--remove-indices-json` (`[0]`); never pass inline JSON strings on PowerShell.
3. **One-call `intake-slots` & `batch --cmd`**: `intake-slots` creates the draft quote and returns filtered slots directly (no follow-up `slots` call needed). ALWAYS combine 2+ reads or chained steps—including pricing 2+ parts (`batch --cmd "price-part ..." --cmd "price-part ..."`), combining 2+ read lookups (`batch --cmd "alternates --service-package '<PKG>' --slot-number 2" --cmd "inspection-templates"` or `batch --cmd "decode-vin --vin <VIN>" --cmd "vehicle-info --vehicle <VIN>"`), or `amend-quote` + `send-approval`—into ONE `batch` call (`python -m tools.cli --json batch --cmd "..." --cmd "..."`) instead of separate CLI calls. Wrap multi-word inner values in single quotes (`'...'`).
4. **Surface deferred services**: Check `data.deferred_services` (`deferred_count > 0`) returned by `quote` or `vehicle-info`. Highlight any `safety_hazard` items immediately and offer `add-deferred`.
5. **Server math, parts pricing & links**: Never compute labor rates, durations, or 13% HST manually; calculate part selling rates via `price-part` (reusing non-zero Frappe `Item.standard_rate` on repeat quotes unless a fresh Tesla lookup at `https://parts.tesla.com/en-CA/find-part?searchTerm=<query>` is requested, and keeping Tesla comparison data out of customer-facing quote text). Return server totals from `data` and Desk/Print/Approval links from `urls` (pass `--summary` only if a markdown `summary_card` is requested).

## Required Fields, `--stage` & Pre-Dispatch Callback

- **`quote` / `intake-slots` required fields**: `--customer`, `--vehicle`, `--odometer-in` (`>= 0`; NEVER guess or default), vehicle **color** (`--color` / `--vehicle-color`) and **license plate** (`--license-plate` / `--plate`) if not already on the `Repair Vehicle`, and at least one `--service`/`--services-json`/`--notes`.
- **Human-facing vehicle references**: Always refer to vehicles by **year, color, model, and license plate** (not VIN-only). Under tight space, drop VIN first, then year and plate (keep color + model).
- **Preferred Turn-1 Staged Form (`--stage`)**: Stage single writes (`python -m tools.cli --json --stage quote ...`) or chained pipelines (`python -m tools.cli --json --stage batch --cmd "quote ..." --cmd "amend-quote --repair-order @step0.data.name ..." --cmd "send-approval --repair-order @prev.data.name --medium link"` or `quote` + `deposit --repair-order @step0.data.name --amount <CAD> --payment-method <METHOD>`), even when `--odometer-in` or optional fields need user input. Use PowerShell-safe `@stepN.<field>` / `@prev.<field>` (`stepN.<field>` and `$stepN.<field>` also work, but NEVER put unescaped `$` like `$step0` or `$100` inside PowerShell `"..."` strings). Present the returned `127.0.0.1` URL and end the turn. On follow-up turns, read created IDs via `python -m tools.cli --json stage-receipt` or pass `@last` / `@last.<field>`.
- **Legacy Callback (`--dry-run`)**: When not using `--stage` (and `ISM_STAGE_ONLY` is unset), run `--dry-run`, output `### ISM Pre-Dispatch Summary` (marking missing fields `[MISSING — REQUIRED]`), and wait for explicit user confirmation before executing live writes.

## Verification

- Verify `success: true`, `data.name` (`RO-...`), and `urls.print` / `urls.desk` (or `urls.approval` for `send-approval`).

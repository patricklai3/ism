# ISM Agent OS Core Rules

This workspace operates an automotive repair facility on Frappe with ISM via Skills + CLI (`python -m tools.cli --json <command>`; alias: `ism-agent-os`) and direct Frappe REST (`/api/method/...`, `/api/resource/...`).

## Skill Files (Read Directly — Do NOT Run `Get-ChildItem skills\`)

- `skills/ism-service-advisor/SKILL.md` — intake, `quote`, `amend-quote`, `estimate`, `send-approval`, `decode-vin`, `vehicle-info`
- `skills/ism-shop-dispatcher/SKILL.md` — `status`, `tech-overview`, `slots`, `intake-slots`, `book`, `reschedule`, `unscheduled`
- `skills/ism-floor-technician/SKILL.md` — `job-status`, `toggle-service`, inspections, `add-supplement`, `checkout`
- `skills/ism-parts-specialist/SKILL.md` — `check-stock`, `alternates`, `price-part`, `procure`, `pay-purchase`, `purchase-invoice`, `receive-po`, Tesla & catalog ingestion
- `skills/ism-shop-executive/SKILL.md` — floor telemetry, `list-docs`, `action-items`

## Operational Invariants

1. **Local Staged Confirmation (`--stage`) or Pre-Dispatch Summary (ALL writes)**: Before ANY write CLI command (`quote`, `intake-slots`, `create-ro-from-entry`, `amend-quote`, `send-approval`, `book`, `reschedule`, `appointment-status`, `job-status`, `deposit`, `checkout`, `toggle-service`, `add-supplement`, `duplicate-side`, `add-deferred`, `create-action`, `resolve-action`, `apply-inspection`, `record-inspection`, `procure`, `pay-purchase`, `purchase-invoice`, `receive-po`, `ingest-parts`, `import-tesla`, `ingest-tesla`, `link-vehicle-customer`, `create-doc`, `update-doc`, `call-method`, or any `batch` containing writes):
   - NEVER fabricate, guess, or substitute missing required fields (e.g. never invent `odometer_in=10000` or default `payment_method`).
   - **Preferred (`--stage`)**: Run `python -m tools.cli --json --stage <subcmd ...>` (or `--stage batch --cmd "..." --cmd "..."` with PowerShell-safe `@stepN.<field>` / `@prev.<field>` chaining; `stepN.<field>` and `$stepN.<field>` also work, but NEVER put unescaped `$` like `$step0` or `$100` inside PowerShell `"..."` strings) on Turn 1—even when required fields are omitted. *(In Antigravity on Windows, if `.ism_staged/server.json` is not alive, start `python -m tools.stage_server` with `IsDaemon=true` before `--stage` so the runner's Job Object does not kill the daemon on command exit.)* Present the returned clickable `127.0.0.1` URL (and any `missing_required` fields) to the user and end your turn (never auto-open a browser). The user completes any missing fields and clicks **Submit** locally. On follow-up turns, read created IDs via `python -m tools.cli --json stage-receipt` (`[--id <STAGE_ID>]`) or pass `@last` / `@last.<field>`.
   - **Legacy (`--dry-run`)**: Unless `ISM_STAGE_ONLY=1` is set, you may run `python -m tools.cli --dry-run <subcmd ...>`, output `### ISM Pre-Dispatch Summary` (marking missing fields `[MISSING — REQUIRED]`), and wait for explicit user confirmation before executing the live write command.
2. **Direct intent-to-command routing (read-only & after confirmation)**: **NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. Use exact CLI flags from Quick Command Index and skill files. Do NOT run redundant pre-checks (`status`, `slots`, `tech-overview`, `estimate`, or `get-doc`) when sufficient inputs are given.
3. **Batch & composite calls**: `python -m tools.cli --json intake-slots` creates the draft quote and returns filtered slots in one call. For 2+ calls (including pricing 2+ parts via `batch --cmd "price-part ..." --cmd "price-part ..."` or combining 2+ read lookups like `alternates` + `inspection-templates` or `decode-vin` + `vehicle-info`), ALWAYS run ONE `python -m tools.cli --json batch --cmd "..." --cmd "..."` call (alias: `ism-agent-os batch`; or `--file @cmds.json`) instead of separate CLI invocations. Wrap multi-word inner values in `'...'`. Chain outputs between batch steps using PowerShell-safe `@stepN.<field>` or `@prev.<field>` (e.g. `--stage batch --cmd "quote ..." --cmd "amend-quote --repair-order @step0.data.name ..." --cmd "send-approval --repair-order @prev.data.name --medium link"`; `stepN.<field>` and `$stepN.<field>` also work, but never put unescaped `$` like `$step0` or `$100` inside PowerShell `"..."` strings). When creating, amending, and sending approval without `--stage`, run `quote` first, then combine `amend-quote` and `send-approval` in ONE `batch` call.
4. **Quote-free service & date flags (NEVER pass inline JSON on PowerShell)**:
   - Use repeatable `--service "Title"` or `--service "Title::0.5"` on `quote` / `intake-slots`, and `--add-service "Title::0.3"` on `amend-quote`.
   - Use `@path/to/payload.json` ONLY for `--parts-json` or `--remove-indices-json`.
   - Always resolve natural-language dates (`today`, `tomorrow`, `friday`, `next monday`, `+2d`) and 12h/named times (`9am`, `2:30pm`, `noon`) into strict ISO `YYYY-MM-DD` and 24-hour `HH:MM:SS` before calling the CLI. If current date is unknown, run `status` (omitting `--date`) first to read the server `date`. For vague windows (`morning`, `afternoon`), query `slots` or `intake-slots` (`--min-time 08:00:00` or `--min-time 13:00:00`) instead of guessing a fixed booking time.
5. **Scheduling & conflict handling**: Only run `slots` before `book` when availability is requested or date/time is omitted. On bay/tech conflict or ambiguous-bay 417, report conflict details and alternatives before re-dispatching.
6. **Enforce the authorization lock**: Never run `job-status --action start` unless `approval_status` is `Approved` or `Partially Approved`. Never run `job-status --action complete` without `--odometer-out`.
7. **Delegate labor & tax math to Frappe; price parts via `ism-agent-os`**: Never calculate labor totals, Fenton-Wilkinson durations, or 13% Ontario HST manually. Calculate customer part selling prices exclusively in `ism-agent-os` via `python -m tools.cli --json price-part` or `ingest-parts` (per `rules/PARTS_PRICING.md` and `rules/parts_pricing.json`), and keep Tesla OEM comparison details out of customer-facing quote/invoice text.
8. **Submit submittable documents after confirmation**: Quotes (`Repair Order`) start in `Draft Estimate` until customer approval, whereas all Frappe submittable documents (`Purchase Order`, `Request for Quotation`, `Purchase Receipt`, `Purchase Invoice`, `Payment Entry`, `Sales Invoice`) are submitted immediately upon live execution after `### ISM Pre-Dispatch Summary` confirmation. Never leave submittable documents in Draft or ask the user whether to keep them in Draft vs Submit unless they explicitly ask for `--draft`. Never run `checkout --confirm` without `--payment-method`, `--odometer-out`, and explicit human confirmation.
9. **Use curated CLI wrappers first & show deep links**: Prefer curated CLI subcommands over generic REST calls. Include Desk, Calendar, Print, or Approval links from `urls` in replies. Pass `--summary` only when explicitly requested.

## Quick Command Index (`python -m tools.cli --json <cmd>`)

- `status [--date <YYYY-MM-DD>]`
- `tech-overview [--date <YYYY-MM-DD>]`
- `slots [--repair-order <RO>] [--duration <MIN>] [--date <YYYY-MM-DD>] [--days N] [--tags <Lift|Alignment Rack|HV Diagnostic>] [--bay <BAY>] [--technician <TECH>] [--min-time HH:MM[:SS]]`
- `book --date <YYYY-MM-DD> --time <HH:MM[:SS]> [--repair-order <RO>] [--bay <BAY>] [--tech <TECH>] [--duration <MIN>] [--entry-type <Diagnostic|Repair>] [--provisional-customer <NAME>] [--provisional-vehicle <INFO>] [--notes <TEXT>]`
- `unscheduled --repair-order <RO>`
- `estimate --repair-order <RO>`
- `quote --customer <NAME> --vehicle <VEH> --odometer-in <KM> (--service "Title::0.5" | --notes <TEXT>) [--parts-json @parts.json]`
- `intake-slots --customer <NAME> --vehicle <VEH> --odometer-in <KM> (--service "Title::0.5" | --notes <TEXT>) [--date <YYYY-MM-DD>] [--days N] [--duration <MIN>] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>]`
- `create-ro-from-entry --schedule-entry <SE> --odometer-in <KM>`
- `amend-quote --repair-order <RO> [--add-service "Title::0.3"] [--remove-indices-json @indices.json] [--notes <TEXT>]`
- `send-approval --repair-order <RO> [--medium sms|email|link] [--phone <NUM>] [--recipient <ADDR>]`
- `job-status --repair-order <RO> --action <start|hold|park|complete> [--odometer-out <KM>] [--notes <TEXT>]`
- `procure [--repair-order <RO>] --parts-json <@file|JSON> [--company <CO>] [--payment-method <METHOD>] [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>] [--draft]`
- `ingest-parts <file> --service-package <PKG> [--supplier <SUP>] [--source <OEM|Aftermarket>] [--condition <New|Used|Reconditioned>] [--tesla-price <CAD>] [--preserve-rate] [--sync-rates]`
- `price-part [--tesla-price <CAD>] [--cost <CAD>] [--source <OEM|Aftermarket>] [--condition <New|Used|Reconditioned>] [--part-number <NO>] [--part-name <NAME>] [--override-rate <CAD>]`
- `pay-purchase (--purchase-order <PO> | --purchase-invoice <PI>) --payment-method <METHOD> [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>]`
- `purchase-invoice (--purchase-order <PO> | --supplier <SUP> --parts-json <@file|JSON>) [--update-stock] [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]`
- `receive-po (--purchase-order <PO> | --purchase-invoice <PI>)`
- `deposit --repair-order <RO> --amount <CAD> --payment-method <METHOD> [--reference-no <REF>]`
- `checkout --repair-order <RO> --payment-method <METHOD> --odometer-out <KM> [--paid-amount <AMT>] --confirm`
- `decode-vin --vin <VIN>`
- `vehicle-info --vehicle <VEH>`
- `--stage <subcmd ...>` (stage write command(s) for local browser form UI and return clickable `127.0.0.1` URL immediately)
- `stage-receipt [--id <STAGE_ID>]` (read latest or specific submitted staged receipt; use `@last` / `@last.<field>` in follow-up commands)
- `--dry-run <subcmd ...>` (preview `ISM Pre-Dispatch Summary` and check required fields without writing)
- `batch --cmd "<subcmd ...>" --cmd "<subcmd ...>"` (wrap inner spaces in `'...'`; supports `@stepN.<field>` / `@prev.<field>`, plus `stepN.<field>` / `$stepN.<field>`)

## Valid Status Values

- **Repair Order `status`**: `Checked In`, `Draft Estimate`, `Pending Authorization`, `Authorized - In Progress`, `Waiting on Parts`, `Work Completed`, `Billed / Paid`, `Closed / Released`, `Cancelled`.
- **Repair Order `approval_status`**: `Estimating`, `Pending Approval`, `Approved`, `Partially Approved`, `Declined`.
- **Schedule Entry `status`**: `Draft`, `Scheduled`, `Confirmed`, `In Progress`, `Completed`, `No Show`, `Cancelled`.

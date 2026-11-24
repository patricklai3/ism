# ISM Agent OS Core Rules

This workspace operates an automotive repair facility on Frappe with ISM via Skills + CLI (`python -m tools.cli --json <command>`; alias: `ism-agent-os`) and direct Frappe REST (`/api/method/...`, `/api/resource/...`).

## Skill Files (Read Directly — Do NOT Run `Get-ChildItem skills\`)

- `skills/ism-service-advisor/SKILL.md` — intake, `quote`, `amend-quote`, `estimate`, `send-approval`, `decode-vin`, `vehicle-info`
- `skills/ism-shop-dispatcher/SKILL.md` — `status`, `tech-overview`, `slots`, `intake-slots`, `book`, `reschedule`, `unscheduled`
- `skills/ism-floor-technician/SKILL.md` — `job-status`, `toggle-service`, inspections, `add-supplement`, `checkout`, `action-cards`, `board-post`
- `skills/ism-parts-specialist/SKILL.md` — `check-stock`, `bins`, `create-bin`, `assign-bin`, `move-stock`, `adjust-stock`, `alternates`, `price-part`, `procure`, `pay-purchase`, `purchase-invoice`, `receive-po`, Tesla & catalog ingestion
- `skills/ism-shop-executive/SKILL.md` — `cockpit`, `pnl-digest`, `publish-score`, `sync-action`, `list-docs`, `action-items`, `action-cards`, `board-threads`

## Operational Invariants

1. **Pre-Dispatch Summary (`--dry-run`) for ALL domain writes**: Before ANY domain write CLI command (`quote`, `intake-slots`, `create-ro-from-entry`, `amend-quote`, `send-approval`, `book`, `reschedule`, `appointment-status`, `job-status`, `deposit`, `checkout`, `toggle-service`, `add-supplement`, `duplicate-side`, `add-deferred`, `create-action`, `sync-action`, `publish-score`, `apply-inspection`, `record-inspection`, `procure`, `pay-purchase`, `purchase-invoice`, `receive-po`, `create-bin`, `assign-bin`, `move-stock`, `adjust-stock`, `ingest-parts`, `import-tesla`, `ingest-tesla`, `link-vehicle-customer`, `board-post`, `create-doc`, `update-doc`, `call-method`, or any `batch` containing domain writes):
   - NEVER fabricate, guess, or substitute missing required fields (e.g. never invent `odometer_in=10000` or default `payment_method`).
   - **Direct housekeeping (`resolve-action`)**: `resolve-action --action-item <ACT-ID> [--notes <TEXT>]` is directly executable without a separate human confirmation click (still validates `--action-item`) and is safe/idempotent if Frappe clearing conditions already cleared the card.
   - **Required Pre-Dispatch Callback (`--dry-run`)**: Run `python -m tools.cli --dry-run <subcmd ...>` (or `--dry-run batch --cmd "..." --cmd "..."`), output `### ISM Pre-Dispatch Summary` (marking any missing required fields `[MISSING — REQUIRED]`), and wait for explicit user confirmation before executing the live write command.
2. **Direct intent-to-command routing (read-only & after confirmation)**: **NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. Use exact CLI flags from Quick Command Index and skill files. Do NOT run redundant pre-checks (`status`, `slots`, `tech-overview`, `estimate`, or `get-doc`) when sufficient inputs are given.
3. **Batch & composite calls**: `python -m tools.cli --json intake-slots` creates the draft quote and returns filtered slots in one call. For 2+ calls (including pricing 2+ parts via `batch --cmd "price-part ..." --cmd "price-part ..."` or combining 2+ read lookups like `alternates` + `inspection-templates` or `decode-vin` + `vehicle-info`), ALWAYS run ONE `python -m tools.cli --json batch --cmd "..." --cmd "..."` call (alias: `ism-agent-os batch`; or `--file @cmds.json`) instead of separate CLI invocations. Wrap multi-word inner values in `'...'`. Chain outputs between batch steps using PowerShell-safe `@stepN.<field>` or `@prev.<field>` (e.g. `batch --cmd "quote ..." --cmd "amend-quote --repair-order @step0.data.name ..." --cmd "send-approval --repair-order @prev.data.name --medium link"`; `stepN.<field>` and `$stepN.<field>` also work, but never put unescaped `$` like `$step0` or `$100` inside PowerShell `"..."` strings). When creating, amending, and sending approval, you can chain them in ONE `batch` call or run `quote` first then combine `amend-quote` and `send-approval` in ONE `batch` call.
4. **Quote-free service & date flags (NEVER pass inline JSON on PowerShell)**:
   - Use repeatable `--service "Title"` or `--service "Title::0.5"` on `quote` / `intake-slots`, and `--add-service "Title::0.3"` on `amend-quote`.
   - Use `@path/to/payload.json` ONLY for `--parts-json` or `--remove-indices-json`.
   - Always resolve natural-language dates (`today`, `tomorrow`, `friday`, `next monday`, `+2d`) and 12h/named times (`9am`, `2:30pm`, `noon`) into strict ISO `YYYY-MM-DD` and 24-hour `HH:MM:SS` before calling the CLI. If current date is unknown, run `status` (omitting `--date`) first to read the server `date`. For vague windows (`morning`, `afternoon`), query `slots` or `intake-slots` (`--min-time 08:00:00` or `--min-time 13:00:00`) instead of guessing a fixed booking time.
5. **Scheduling & conflict handling**: Only run `slots` before `book` when availability is requested or date/time is omitted. On bay/tech conflict or ambiguous-bay 417, report conflict details and alternatives before re-dispatching.
6. **Enforce the authorization lock**: Never run `job-status --action start` unless `approval_status` is `Approved` or `Partially Approved`. Never run `job-status --action complete` without `--odometer-out`.
7. **Delegate labor & tax math to Frappe; price parts via `ism-agent-os`**: Never calculate labor totals, Fenton-Wilkinson durations, or 13% Ontario HST manually. Calculate customer part selling prices exclusively in `ism-agent-os` via `python -m tools.cli --json price-part` or `ingest-parts` (per `rules/PARTS_PRICING.md` and `rules/parts_pricing.json`), and keep Tesla OEM comparison details out of customer-facing quote/invoice text.
8. **Submit submittable documents after confirmation**: Quotes (`Repair Order`) start in `Draft Estimate` until customer approval, whereas all Frappe submittable documents (`Purchase Order`, `Request for Quotation`, `Purchase Receipt`, `Purchase Invoice`, `Payment Entry`, `Sales Invoice`) are submitted immediately upon live execution after `### ISM Pre-Dispatch Summary` confirmation. Never leave submittable documents in Draft or ask the user whether to keep them in Draft vs Submit unless they explicitly ask for `--draft`. Never run `checkout --confirm` without `--payment-method`, `--odometer-out`, and explicit human confirmation.
9. **Use curated CLI wrappers first & show deep links**: Prefer curated CLI subcommands over generic REST calls (use `move-stock` / `adjust-stock` instead of raw `create-doc --doctype "Stock Entry"`). Include Desk, Calendar, Print, or Approval links from `urls` in replies. Pass `--summary` only when explicitly requested.
10. **Addressing Shop Board Action Cards (Exception-Only Board)**:
   - `/board` (`board-threads`, `board-thread`, `action-cards`) is **exception-only**: routine system events (portal approvals, PIN updates, Square payments, self-serve bookings/reschedules, and parts-ready notices) live in `/cockpit` and `action-items`, not on `/board` timelines.
   1. Fetch 1 or N cards plus RO summary, capture transcript, media links, and clearing conditions in one call: `python -m tools.cli --json action-cards --action-item <ACT-1> [<ACT-2> ...]` (or `--repair-order <RO>`).
   2. Preview via `--dry-run batch` and, after confirmation, execute the required ERP domain writes via `batch`.
   3. Append `resolve-action --action-item <ACT-ID> --notes "<summary>"` in the `batch` or run `resolve-action` directly after resolution completes (no extra confirmation click needed; safe/idempotent if Frappe clearing conditions already auto-cleared the card).
   4. Only call `board-post --repair-order <RO> --text "..."` when the technician specifically needs a direct floor message (skips site AI by default; pass `--run-ai` only if AI processing is explicitly desired).

## Quick Command Index (`python -m tools.cli --json <cmd>`)

- `status [--date <YYYY-MM-DD>]`
- `cockpit [--date <YYYY-MM-DD>]`
- `action-cards [--action-item <ACT-1> [<ACT-2> ...]] [--repair-order <RO>] [--status Open] [--limit 20] [--events-limit 20]`
- `board-threads [--status OPEN|ALL|ACTIVE|NEEDS_ACTION|BLOCKED_PARTS|WAITING_CUSTOMER|CLOSED] [--assigned-only] [--query <TEXT>] [--limit 50]`
- `board-thread (--thread-key RO:<RO> | --repair-order <RO>) [--limit 100] [--before-event <BE>]`
- `board-post (--thread-key RO:<RO> | --repair-order <RO>) --text <TEXT> [--intent <TAG>] [--run-ai]`
- `resolve-action --action-item <ACT-ID> [--notes <TEXT>]` (direct housekeeping — no separate human confirmation click; idempotent if auto-cleared)
- `pnl-digest [--from-date <YYYY-MM-DD>] [--to-date <YYYY-MM-DD>] [--company <CO>]`
- `publish-score --metric <KEY> --score <0-100> --verdict <TEXT> [--grade <Healthy|Watch|Critical>] [--period-date <YYYY-MM-DD>] [--report-url <URL>]`
- `sync-action --dedup-key <KEY> --action <snooze|dismiss|reopen> [--snooze-minutes N] [--notes <TEXT>]`
- `tech-overview [--date <YYYY-MM-DD>]`
- `slots [--repair-order <RO>] [--duration <MIN>] [--date <YYYY-MM-DD>] [--days N] [--tags <Lift|Alignment Rack|HV Diagnostic>] [--bay <BAY>] [--technician <TECH>] [--min-time HH:MM[:SS]]`
- `book --date <YYYY-MM-DD> --time <HH:MM[:SS]> [--repair-order <RO>] [--bay <BAY>] [--tech <TECH>] [--duration <MIN>] [--entry-type <Diagnostic|Repair>] [--provisional-customer <NAME>] [--provisional-vehicle <INFO>] [--notes <TEXT>]`
- `unscheduled --repair-order <RO>`
- `estimate --repair-order <RO>`
- `quote --customer <NAME> --vehicle <VEH> --odometer-in <KM> [--color <COLOR>] [--license-plate <PLATE>] (--service "Title::0.5" | --notes <TEXT>) [--parts-json @parts.json]`
- `intake-slots --customer <NAME> --vehicle <VEH> --odometer-in <KM> [--color <COLOR>] [--license-plate <PLATE>] (--service "Title::0.5" | --notes <TEXT>) [--date <YYYY-MM-DD>] [--days N] [--duration <MIN>] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>]`
- `create-ro-from-entry --schedule-entry <SE> --odometer-in <KM> [--color <COLOR>] [--license-plate <PLATE>]`
- `amend-quote --repair-order <RO> [--add-service "Title::0.3"] [--remove-indices-json @indices.json] [--notes <TEXT>]`
- `send-approval --repair-order <RO> [--medium sms|email|link] [--phone <NUM>] [--recipient <ADDR>]`
- `job-status --repair-order <RO> --action <start|hold|park|complete> [--odometer-out <KM>] [--notes <TEXT>]`
- `check-stock [--repair-order <RO>] [--items-json <@file|JSON>] [--company <CO>] [--bin-location <BIN>]`
- `bins [--warehouse <WH>] [--zone <ZONE>] [--query <TEXT>] [--include-stock]`
- `create-bin --bin-code <CODE> [--warehouse <WH>] [--zone <Z>] [--aisle <A>] [--rack <R>] [--shelf <S>] [--bin-type <Standard|Overflow|Receiving|Bulk|Hazmat>] [--description <TEXT>]`
- `assign-bin --item-code <ITEM> [--bin-location <BIN>]`
- `move-stock --from-bin <BIN> --to-bin <BIN> (--item-code <ITEM> --qty <N> | --items-json <@file|JSON>) [--warehouse <WH>] [--notes <TEXT>]`
- `adjust-stock --purpose <receipt|issue> --items-json <@file|JSON> [--bin-location <BIN>] [--warehouse <WH>] [--reason <TEXT>]`
- `procure [--repair-order <RO>] --parts-json <@file|JSON> [--company <CO>] [--payment-method <METHOD>] [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>] [--draft]`
- `ingest-parts <file> --service-package <PKG> [--supplier <SUP>] [--source <OEM|Aftermarket>] [--condition <New|Used|Reconditioned>] [--tesla-price <CAD>] [--preserve-rate] [--sync-rates]`
- `price-part [--tesla-price <CAD>] [--cost <CAD>] [--source <OEM|Aftermarket>] [--condition <New|Used|Reconditioned>] [--part-number <NO>] [--part-name <NAME>] [--override-rate <CAD>]`
- `pay-purchase (--purchase-order <PO> | --purchase-invoice <PI>) --payment-method <METHOD> [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>]`
- `purchase-invoice (--purchase-order <PO> | --supplier <SUP> --parts-json <@file|JSON>) [--update-stock] [--bin-location <BIN>] [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]`
- `receive-po (--purchase-order <PO> | --purchase-invoice <PI>) [--bin-location <BIN>] [--bin-allocations-json <@file|JSON>]`
- `deposit --repair-order <RO> --amount <CAD> --payment-method <METHOD> [--reference-no <REF>]`
- `checkout --repair-order <RO> --payment-method <METHOD> --odometer-out <KM> [--paid-amount <AMT>] --confirm`
- `decode-vin --vin <VIN> [--manufactured-month <MM/YY>]`
- `search-vehicles [--query <TERM>] [--customer <NAME>] [--model <MODEL>] [--generation <GEN>]`
- `tesla-generations [--model <MODEL>]`
- `vehicle-info --vehicle <VEH>`
- `record-inspection --repair-order <RO> [--odometer-in <KM>] [--color <COLOR>] [--license-plate <PLATE>] [--concern <TEXT>] [--items-json <@file|JSON>]`
- **Vehicle Display & Intake/Check-In Rules**:
  - **Intake & check-in require vehicle color and license plate**: Pass `--color` (`--vehicle-color`) and `--license-plate` (`--plate`) on `quote`, `intake-slots`, `create-ro-from-entry`, and `record-inspection` whenever missing on the `Repair Vehicle` (customer self-serve `/book` appointments may arrive without a license plate, so supply `--license-plate` on check-in / RO conversion).
  - **Human-facing vehicle references**: Refer to vehicles by **year, color, model, and license plate** (e.g., `2022 Pearl White Model 3 (EV-402)`) — never VIN-only. Under limited space, omit VIN first, then year and license plate (keeping color + model longest).
- `--dry-run <subcmd ...>` (preview `ISM Pre-Dispatch Summary` and check required fields without writing)
- `batch --cmd "<subcmd ...>" --cmd "<subcmd ...>"` (wrap inner spaces in `'...'`; supports `@stepN.<field>` / `@prev.<field>`, plus `stepN.<field>` / `$stepN.<field>`)

## Valid Status Values

- **Repair Order `status`**: `Checked In`, `Draft Estimate`, `Pending Authorization`, `Authorized - In Progress`, `Waiting on Parts`, `Work Completed`, `Billed / Paid`, `Closed / Released`, `Cancelled`.
- **Repair Order `approval_status`**: `Estimating`, `Pending Approval`, `Approved`, `Partially Approved`, `Declined`.
- **Schedule Entry `status`**: `Draft`, `Scheduled`, `Confirmed`, `In Progress`, `Completed`, `No Show`, `Cancelled`.

---
name: ism-parts-specialist
description: >-
  Handles supplier parts catalog bulk imports, Tesla Service Manual procedure parsing,
  alternate part lookups, stock deficit checks, standalone or RO-linked Purchase Orders,
  supplier advance payments, Purchase Invoices, and Purchase Receipts in Frappe ISM.
  Use when ingesting catalogs, adding Tesla procedures, ordering inventory, or recording supplier payments and receipts.
---

# Parts Specialist Operations

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. Use `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly with the exact flags below.

## 1. Bulk Import Supplier Catalogs

1. Prepare CSV, TSV, pipe-delimited, or JSON rows with part number, description, source, condition, rate, cost, quantity, revision, OEM number, and optional `tesla_oem_price` (CAD).
2. When `tesla_oem_price` is present, `ingest-parts` automatically calculates `unit_rate` via `rules/parts_pricing.json` (`-30%` baseline, small-ticket match `<= $20`, and cost-markup floor), overriding raw catalog `rate` unless `--preserve-rate` is passed. Pass `--sync-rates` to update `Item.standard_rate` on existing Frappe items.
3. Run `python -m tools.cli --json ingest-parts <file> --service-package <PKG>` (with required `--service-package` to nest the part under at least one `Service Package`, plus `--supplier`, `--source`, `--condition`).
4. Review `total_created`, `total_updated`, `total_errors`, and `pricing_summary` (internal only) in output.

## 1b. Tesla-Offset Part Pricing (`price-part`)

1. **Reuse stored Frappe rate first**: If the Frappe `Item` already has a non-zero `standard_rate` on a repeat quote, reuse it unless a fresh Tesla price check is requested.
2. **Search Tesla Canada**: Open `https://parts.tesla.com/en-CA/find-part?searchTerm=<query>` using the OEM part number first, or the part name if unlisted or superseded.
3. **Exercise discretion**: Confirm the region is Canada (`en-CA`, CAD), verify vehicle model/generation compatibility across broad name results, and resolve superseded part numbers.
4. **Compute & audit internally**: Run `python -m tools.cli --json price-part --tesla-price <CAD> [--cost <CAD>] [--source OEM|Aftermarket] [--condition New|Used|Reconditioned] [--part-number <NO>]`. Surface the pricing rule and warnings (`above_tesla`, missing cost, or cost-plus fallback) in `### ISM Pre-Dispatch Summary` only—never in customer-facing quote or invoice text.
5. **Batch 2+ pricing or read lookups in one call**: Whenever pricing 2+ parts or combining 2+ read lookups in one turn, ALWAYS use ONE `batch` call instead of separate CLI invocations (e.g., `python -m tools.cli --json batch --cmd "price-part --tesla-price 200 --cost 40 --source Aftermarket --part-name 'Aero Duct'" --cmd "price-part --tesla-price 10 --cost 4 --source OEM --part-name 'Trim Clip'"`, or `python -m tools.cli --json batch --cmd "alternates --service-package '<PKG>' --slot-number 2" --cmd "inspection-templates"`).

## 2. Ingest Tesla Service Manual Procedures

1. When given a `https://service.tesla.com/...` link, run `python -m tools.cli --json fetch-tesla <url>` (add `--repair-order <RO>` or `--vehicle <VEH>` to check vehicle model/generation compatibility) or `python -m tools.cli --json ingest-tesla <url>` (supports `--model`, `--generation`, `--repair-order`, `--on-conflict`, `--force`, `--side`) to fetch and import in one step. Run `python -m tools.cli --json tesla-generations [--model <MODEL>]` to inspect canonical Tesla generation names and manual mappings.
2. If you already have procedure details from scraped text or parsed JSON, run `python -m tools.cli --json import-tesla` with `--title`, `--code`, `--hours`, `--model`, optional `--generation`, `--parts-json` (or `--url` / `--data-json`), and optional `--repair-order`, `--on-conflict` (`skip`, `overwrite`, `add`, `error`), `--force`, and `--side` (`LH`, `RH`, `Both`). `Service Package` records are scoped by `(model, generation)`, so the same package title can coexist across generations and `quote`/`amend-quote` auto-select the matching generation.
3. Return the created `Service Package` or `Repair Order` link from `urls.desk`.

## 3. Resolve Deficits and Procure Parts (RO-Linked or Standalone Inventory)

1. For Repair Order jobs, run `python -m tools.cli --json check-stock --repair-order <RO> [--bin-location <BIN>]` to list required parts, available warehouse/bin stock, and `deficit_count` in `data`. For general inventory checks, omit `--repair-order` and pass `--items-json`.
2. To find compatible OEM, aftermarket, or used replacements with stock quantities, run `python -m tools.cli --json alternates` (`--service-package`, `--component-slot`, `--slot-number`, `--oem`, or `--item-code`). If also checking templates or another read in the same turn, combine them in one `batch` call (`python -m tools.cli --json batch --cmd "alternates --service-package '<PKG>' --slot-number 2" --cmd "inspection-templates"`).
3. A `Purchase Order` does **NOT** require a `Repair Order`. Standalone stock/inventory purchases omit `--repair-order`:
   - If ordering a part that does not exist in Frappe `Item` yet, ingest it via `ingest-parts <file> --service-package <PKG>` first (so it gets canonical `[Source]-[PartNumber]-[Condition]` coding and is nested under a `Service Package`), then procure it. **NEVER** create vehicle parts via raw `create-doc --doctype "Item"`.
   - Before dispatching `ingest-parts`, look up candidate `Service Package` records via `list-docs --doctype "Service Package"` if the target service is not already known, or flag `--service-package` as `[MISSING — REQUIRED]` in the Pre-Dispatch Summary.
   - Run `python -m tools.cli --json procure [--repair-order <RO>] --parts-json <@file|JSON>` (`item_code`, `qty`, `supplier`, optional `rate`, `order_mode`).
   - All submittable documents (`Purchase Order`, `Request for Quotation`, `Purchase Receipt`, `Purchase Invoice`, `Payment Entry`) are **submitted immediately** after `### ISM Pre-Dispatch Summary` confirmation. Never ask the user "Draft or Submit?" or leave submittable docs in Draft unless explicitly instructed (`--draft`).

## 4. Supplier Payments, Purchase Invoices, and Stock Receipts

Suppliers often require payment at order time before goods arrive, and the supplier invoice may arrive either electronically first or physically inside the box with the parts:

1. **Paid at Time of Order (Advance Payment on PO)**:
   - When ordering and paying in one confirmed step: pass `--payment-method <METHOD>` (and optional `--paid-amount <AMT>`, `--reference-no <REF>`, `--reference-date <YYYY-MM-DD>`) directly to `python -m tools.cli --json procure`. This submits the PO and immediately submits the linked `Payment Entry` (`Pay` to `Supplier`). When **paid in full**, **OMIT `--paid-amount`** so Frappe pays the full tax-inclusive `grand_total` automatically (never compute `qty * rate` manually); pass `--paid-amount` ONLY for an explicit partial deposit.
   - When recording payment later against an existing PO or unpaid PI: run `python -m tools.cli --json pay-purchase (--purchase-order <PO> | --purchase-invoice <PI>) --payment-method <METHOD> [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>]`.
2. **Scenario A — Invoice Arrives Immediately, Goods Arrive Later**:
   - First submit the Purchase Invoice with `update_stock=0`: run `python -m tools.cli --json purchase-invoice --purchase-order <PO> [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]` (omit `--update-stock`). Any advance `Payment Entry` made against `<PO>` is attached automatically, reducing `outstanding_amount`.
   - Later when the parts physically arrive, submit the Purchase Receipt: run `python -m tools.cli --json receive-po --purchase-order <PO> [--bin-location <BIN>] [--bin-allocations-json <@file|JSON>]` (or `--purchase-invoice <PI>`). Stock increases on submit and triggers Repair Order readiness re-evaluation.
3. **Scenario B — Invoice Arrives Physically With the Parts**:
   - Check `update_stock` directly on the Purchase Invoice: run `python -m tools.cli --json purchase-invoice --purchase-order <PO> --update-stock [--bin-location <BIN>] [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]` (or standalone with `--supplier <SUP> --parts-json <@file|JSON> --update-stock [--bin-location <BIN>]`). Any advance `Payment Entry` made against `<PO>` is attached automatically, and stock updates immediately on submission (no separate `receive-po` needed).
4. **Standard Receipt Without Invoice Yet**:
   - When parts arrive before any invoice, run `python -m tools.cli --json receive-po --purchase-order <PO> [--bin-location <BIN>] [--bin-allocations-json <@file|JSON>]`.
5. **Supplier PO Lookup Guard (`purchase-invoice` / `receive-po`)**:
   - When a user asks to receive or invoice a PO for a specific supplier, look up that supplier's open PO via `python -m tools.cli --json list-docs --doctype "Purchase Order" --filter supplier=<SUP> --filter docstatus=1 --fields "name,supplier,status,per_received,per_billed,grand_total,advance_paid" --order-by "creation desc" --limit 5`. Run this single filtered `list-docs` call directly — do **NOT** omit `--filter docstatus=1` or `--fields`, do **NOT** run an unfiltered `list-docs` first, and do **NOT** run `--help`.
   - Then invoice/receive directly using the exact `purchase-invoice` flags (`python -m tools.cli --json purchase-invoice --purchase-order <PO> --update-stock --bill-no <NO> --bill-date <YYYY-MM-DD>`; omit `--update-stock` if goods arrive later, or use `python -m tools.cli --json receive-po --purchase-order <PO>` for receipt only) — do **NOT** run `--help`.
   - If `list-docs` returns no open PO for that supplier, **STOP** and report that no open Purchase Order was found for that supplier — **NEVER** substitute or fall back to another supplier's Purchase Order.

## 5. Storage Bins, Putaway, Transfers & Cycle-Count Adjustments

Use curated bin commands (never raw `create-doc --doctype "Stock Entry"`) so `validate_negative_stock` and bin balances remain exact:

1. **Inspect bins & live bin stock**: `python -m tools.cli --json bins [--warehouse <WH>] [--zone <ZONE>] [--query <TEXT>] [--include-stock]`
2. **Create a Storage Bin**: `python -m tools.cli --json create-bin --bin-code <CODE> [--warehouse <WH>] [--zone <Z>] [--aisle <A>] [--rack <R>] [--shelf <S>] [--bin-type <Standard|Overflow|Receiving|Bulk|Hazmat>] [--description <TEXT>]`
3. **Assign or clear an Item's Home Bin**: `python -m tools.cli --json assign-bin --item-code <ITEM> [--bin-location <BIN>]` (omit `--bin-location` to clear)
4. **Bin-to-bin stock transfer (putaway / overflow)**: `python -m tools.cli --json move-stock --from-bin <BIN> --to-bin <BIN> (--item-code <ITEM> --qty <N> | --items-json <@file|JSON>) [--warehouse <WH>] [--notes <TEXT>]`
5. **Bin stock receipt / issue adjustment**: `python -m tools.cli --json adjust-stock --purpose <receipt|issue> --items-json <@file|JSON> [--bin-location <BIN>] [--warehouse <WH>] [--reason <TEXT>]`

## Required Fields & Pre-Dispatch Callback

- **`procure` / `pay-purchase` / `purchase-invoice` / `receive-po` / `create-bin` / `assign-bin` / `move-stock` / `adjust-stock` / `ingest-parts` / `import-tesla` / `ingest-tesla`**:
  - Run `--dry-run` first (or `--dry-run batch --cmd "procure ..." --cmd "receive-po --purchase-order @step0.data.purchase_orders.0"`), output `### ISM Pre-Dispatch Summary` (marking any missing fields such as `--payment-method`, `--service-package`, or `--bill-no` as `[MISSING — REQUIRED]`), and wait for explicit operator confirmation before dispatching live writes.
- Do **NOT** require `--repair-order` on `procure`.
- `--service-package` is **REQUIRED** on `ingest-parts` so every newly ingested part is nested under at least one `Service Package`.
- When payment is recorded (`pay-purchase`, or `procure` with `--paid-amount`), `--payment-method` is required; if the user wrote e.g. `"paid $150 +hst"` without stating the payment method, flag `--payment-method` as missing rather than guessing.

## Verification

- Confirm `data.total_errors` is zero (or report failed rows explicitly) after catalog import.
- Confirm `docstatus == 1` (`submitted: true`) and check `total_advance` / `outstanding_amount` / `per_received` in `data` after procurement, payment, invoicing, and receiving calls.

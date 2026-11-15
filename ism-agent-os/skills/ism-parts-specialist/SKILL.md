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

1. Prepare CSV, TSV, pipe-delimited, or JSON rows with part number, description, source, condition, rate, cost, quantity, revision, and OEM number.
2. Run `python -m tools.cli --json ingest-parts <file>` (with `--supplier`, `--source`, `--condition`, and optional `--service-package`) to sanitize rows into structured JSON items and import them.
3. Review `total_created`, `total_updated`, and `total_errors` in `data`.

## 2. Ingest Tesla Service Manual Procedures

1. When given a `https://service.tesla.com/...` link, run `python -m tools.cli --json fetch-tesla <url>` (add `--repair-order <RO>` or `--vehicle <VEH>` to check vehicle model/generation compatibility) or `python -m tools.cli --json ingest-tesla <url>` (supports `--repair-order`, `--on-conflict`, `--force`, `--side`) to fetch and import in one step.
2. If you already have procedure details from scraped text or parsed JSON, run `python -m tools.cli --json import-tesla` with `--title`, `--code`, `--hours`, `--model`, `--parts-json` (or `--url` / `--data-json`), and optional `--repair-order`, `--on-conflict` (`skip`, `overwrite`, `add`, `error`), `--force`, and `--side` (`LH`, `RH`, `Both`).
3. Return the created `Service Package` or `Repair Order` link from `urls.desk`.

## 3. Resolve Deficits and Procure Parts (RO-Linked or Standalone Inventory)

1. For Repair Order jobs, run `python -m tools.cli --json check-stock --repair-order <RO>` to list required parts, available warehouse stock, and `deficit_count` in `data`. For general inventory checks, omit `--repair-order` and pass `--items-json`.
2. To find compatible OEM, aftermarket, or used replacements with stock quantities, run `python -m tools.cli --json alternates` (`--service-package`, `--component-slot`, `--slot-number`, `--oem`, or `--item-code`).
3. A `Purchase Order` does **NOT** require a `Repair Order`. Standalone stock/inventory purchases omit `--repair-order`:
   - If ordering a part that does not exist in Frappe `Item` yet, ingest/create the `Item` first (e.g., via `ingest-parts` or `create-doc` `Item`), then procure it.
   - Run `python -m tools.cli --json procure [--repair-order <RO>] --parts-json <@file|JSON>` (`item_code`, `qty`, `supplier`, optional `rate`, `order_mode`).
   - All submittable documents (`Purchase Order`, `Request for Quotation`, `Purchase Receipt`, `Purchase Invoice`, `Payment Entry`) are **submitted immediately** after `### ISM Pre-Dispatch Summary` confirmation. Never ask the user "Draft or Submit?" or leave submittable docs in Draft unless explicitly instructed (`--draft`).

## 4. Supplier Payments, Purchase Invoices, and Stock Receipts

Suppliers often require payment at order time before goods arrive, and the supplier invoice may arrive either electronically first or physically inside the box with the parts:

1. **Paid at Time of Order (Advance Payment on PO)**:
   - When ordering and paying in one confirmed step: pass `--payment-method <METHOD>` (and optional `--paid-amount <AMT>`, `--reference-no <REF>`, `--reference-date <YYYY-MM-DD>`) directly to `python -m tools.cli --json procure`. This submits the PO and immediately submits the linked `Payment Entry` (`Pay` to `Supplier`).
   - When recording payment later against an existing PO or unpaid PI: run `python -m tools.cli --json pay-purchase (--purchase-order <PO> | --purchase-invoice <PI>) --payment-method <METHOD> [--paid-amount <AMT>] [--reference-no <REF>] [--reference-date <YYYY-MM-DD>]`.
2. **Scenario A — Invoice Arrives Immediately, Goods Arrive Later**:
   - First submit the Purchase Invoice with `update_stock=0`: run `python -m tools.cli --json purchase-invoice --purchase-order <PO> [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]` (omit `--update-stock`). Any advance `Payment Entry` made against `<PO>` is attached automatically, reducing `outstanding_amount`.
   - Later when the parts physically arrive, submit the Purchase Receipt: run `python -m tools.cli --json receive-po --purchase-order <PO>` (or `--purchase-invoice <PI>`). Stock increases on submit and triggers Repair Order readiness re-evaluation.
3. **Scenario B — Invoice Arrives Physically With the Parts**:
   - Check `update_stock` directly on the Purchase Invoice: run `python -m tools.cli --json purchase-invoice --purchase-order <PO> --update-stock [--bill-no <NO>] [--bill-date <YYYY-MM-DD>]` (or standalone with `--supplier <SUP> --parts-json <@file|JSON> --update-stock`). Any advance `Payment Entry` made against `<PO>` is attached automatically, and stock updates immediately on submission (no separate `receive-po` needed).
4. **Standard Receipt Without Invoice Yet**:
   - When parts arrive before any invoice, run `python -m tools.cli --json receive-po --purchase-order <PO>`.

## Required Fields & Pre-Dispatch Callback

- **`procure` / `pay-purchase` / `purchase-invoice` / `receive-po` / `ingest-parts` / `import-tesla` / `ingest-tesla`**: Output `### ISM Pre-Dispatch Summary` via `--dry-run` before dispatching live writes.
- Do **NOT** require `--repair-order` on `procure`.
- When payment is recorded (`pay-purchase`, or `procure` with `--paid-amount`), `--payment-method` is required; if the user wrote e.g. `"paid $150 +hst"` without stating the payment method, flag `--payment-method` as missing in the Pre-Dispatch Summary rather than guessing.

## Verification

- Confirm `data.total_errors` is zero (or report failed rows explicitly) after catalog import.
- Confirm `docstatus == 1` (`submitted: true`) and check `total_advance` / `outstanding_amount` / `per_received` in `data` after procurement, payment, invoicing, and receiving calls.

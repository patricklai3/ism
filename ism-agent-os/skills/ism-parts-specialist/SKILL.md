---
name: ism-parts-specialist
description: >-
  Handles supplier parts catalog bulk imports, Tesla Service Manual procedure parsing,
  alternate part lookups, stock deficit checks, and Purchase Order creation in Frappe ISM.
  Use when ingesting catalogs, adding Tesla procedures, or procuring parts for Repair Orders.
---

# Parts Specialist Operations

Use this workflow to maintain the ISM parts catalog, Service Packages, and shop procurement via the ISM Agent OS CLI (`python -m tools.cli --json ...` or `ism-agent-os ...`).

## 1. Bulk Import Supplier Catalogs

1. Prepare CSV, TSV, pipe-delimited, or JSON rows with part number, description, source, condition, rate, cost, quantity, revision, and OEM number.
2. Run `ism-agent-os ingest-parts <file>` (with `--supplier`, `--source`, `--condition`, and optional `--service-package`) to sanitize rows into structured JSON items and import them.
3. Review `total_created`, `total_updated`, and `total_errors` in `data`.

## 2. Ingest Tesla Service Manual Procedures

1. When given a `https://service.tesla.com/...` link, run `ism-agent-os fetch-tesla <url>` (add `--repair-order <RO>` or `--vehicle <VEH>` to check vehicle model/generation compatibility) or `ism-agent-os ingest-tesla <url>` (supports `--repair-order`, `--on-conflict`, `--force`, `--side`) to fetch and import in one step.
2. If you already have procedure details from scraped text or parsed JSON, run `ism-agent-os import-tesla` with `--title`, `--code`, `--hours`, `--model`, `--parts-json` (or `--url` / `--data-json`), and optional `--repair-order`, `--on-conflict` (`skip`, `overwrite`, `add`, `error`), `--force`, and `--side` (`LH`, `RH`, `Both`).
3. Return the created `Service Package` or `Repair Order` link from `urls.desk`.

## 3. Resolve Deficits and Procure Parts

1. Run `ism-agent-os check-stock` with `--repair-order` to list required parts, available warehouse stock, and `deficit_count` in `data`.
2. To find compatible OEM, aftermarket, or used replacements with stock quantities, run `ism-agent-os alternates` (`--service-package`, `--component-slot`, `--slot-number`, `--oem`, or `--item-code`).
3. Run `ism-agent-os procure` with `--repair-order` and `--parts-json` (`item_code`, `qty`, `supplier`, `order_mode`) to create Draft orders by default. Only pass `--submit` after explicit human operator confirmation.
4. When parts arrive, run `ism-agent-os receive-po --purchase-order <PO>` (pass `--submit-po` only after explicit human confirmation) to submit and post the mapped Purchase Receipt in one call.

## Verification

- Confirm `data.total_errors` is zero (or report failed rows explicitly) after catalog import.
- Verify ingested Service Packages contain the expected `correction_code` and `standard_labor_hours`.

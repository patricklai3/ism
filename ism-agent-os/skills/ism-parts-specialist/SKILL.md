---
name: ism-parts-specialist
description: >-
  Handles supplier parts catalog bulk imports, Tesla Service Manual procedure parsing,
  alternate part lookups, stock deficit checks, and Purchase Order creation in Frappe ISM.
  Use when ingesting catalogs, adding Tesla procedures, or procuring parts for Repair Orders.
---

# Parts Specialist Operations

Use this workflow to maintain the ISM parts catalog, Service Packages, and shop procurement.

## 1. Bulk Import Supplier Catalogs

1. Ensure catalog lines follow `Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#`.
2. If raw text uses custom CSV headers, run `python ingestion/supplier_catalog.py` or `ism-agent-os ingest-parts <file>` to normalize it.
3. Call `ism_import_supplier_catalog` with `raw_text`, `supplier`, `default_source`, and `default_condition`.
4. Review `created`, `updated`, and `errors` counts in the returned `summary_card`.

## 2. Ingest Tesla Service Manual Procedures

1. When given a `https://service.tesla.com/...` link, call `ism_fetch_tesla_manual` with `url` to extract title, correction code, and flat-rate time (FRT).
2. If you already have procedure details from scraped text or Markdown, pass them directly.
3. Call `ism_import_tesla_procedure` with `procedure_title`, `correction_code`, `labor_hours`, `model`, and optional `parts`.
4. Return the created `Service Package` link from the `summary_card`.

## 3. Resolve Deficits and Procure Parts

1. Call `ism_check_parts_stock` with `ro_name` to list required parts, available warehouse stock, and `is_deficit` flags.
2. To find compatible OEM, aftermarket, or used replacements, call `frappe_call_method` with `method="ism.api.parts.get_component_alternates"`.
3. After confirming supplier names and prices with a human operator, call `ism_submit_procurement_orders` with `repair_order` and `parts_data` (`item_code`, `qty`, `supplier`, `order_mode`).

## Verification

- Confirm `error_count` is zero (or report failed rows explicitly) after catalog import.
- Verify ingested Service Packages contain the expected `correction_code` and `standard_labor_hours`.

# ISM Parts Pricing Policy (`ism-agent-os`)

This document defines the preliminary baseline for parts pricing in `ism-agent-os`. All numeric thresholds live in [parts_pricing.json](parts_pricing.json) and are executed deterministically by `tools/pricing.py` (`python -m tools.cli --json price-part` and `ingest-parts`).

## 1. Core Principles

- **Anchor to Tesla Canada OEM retail**: Price parts at a discount below Tesla's Canada OEM price (`CAD`) rather than marking up from purchase cost.
- **Protect low-dollar and high-cost parts**: Never sell a part at a loss or razor-thin margin when Tesla's retail price is low or our purchase cost is high.
- **Keep comparison internal**: Tesla OEM reference prices, cost floors, margins, and warnings are shown only in CLI JSON and `### ISM Pre-Dispatch Summary` blocks—never on customer-facing quote or invoice descriptions.

## 2. Preliminary Baseline Rules

| Parameter (`parts_pricing.json`) | Default | Meaning |
| :--- | :--- | :--- |
| `currency` | `"CAD"` | All pricing and floor calculations run in CAD. |
| `tesla_search_url_template` | `https://parts.tesla.com/en-CA/find-part?searchTerm={query}` | Tesla Canada catalog search URL template. |
| `baseline_discount_pct` | `30.0` | Default `30%` discount off Tesla OEM price (`0.70 * tesla_oem_price`) for `New` `OEM` and `Aftermarket` parts. |
| `condition_discount_overrides` | `New: null, Used: null, Reconditioned: null` | Optional per-condition discount overrides (`null` uses `baseline_discount_pct`). |
| `small_ticket_max_tesla_price` | `20.0` | Tesla OEM prices at or below `$20.00` skip the `30%` discount. |
| `small_ticket_discount_pct` | `0.0` | Discount applied at or below `small_ticket_max_tesla_price` (`0%` = match `100%` of Tesla price). |
| `min_sell_price` | `5.0` | Minimum selling price floor (`$5.00 CAD`). |
| `min_cost_markup_pct` | `25.0` | Minimum percentage markup over purchase cost (`cost * 1.25`). |
| `min_cost_margin_dollars` | `5.0` | Minimum dollar margin over purchase cost (`cost + $5.00`). |
| `fallback_cost_markup_pct` | `40.0` | Cost-plus fallback markup when Tesla OEM price is unavailable (`cost + max(cost * 40%, $5.00)`). |
| `rounding_increment` | `0.01` | Final sell price rounding increment (`$0.01 CAD` / exact cent). |

## 3. Calculation Order

1. **Manual override**: If an explicit override rate is provided (`--override-rate` or `--preserve-rate`), use it directly (`manual_override`).
2. **Missing inputs**:
   - Tesla price known, cost unknown (`0` or omitted): Compute Tesla-based price and emit `"Cost unknown — verify supplier cost before ordering."`
   - Tesla price unavailable, cost known (`> 0`): Compute `max(cost + max(cost * 40%, $5.00), $5.00)` (`cost_plus_fallback`) and emit a fallback warning.
   - Both missing: Fail validation (`[MISSING — REQUIRED]`).
3. **Tesla-based price**:
   - If `tesla_oem_price <= $20.00`: apply `small_ticket_discount_pct` (`100%` match, rule `small_ticket_match`).
   - Else if `condition_discount_overrides[part_condition]` is set: apply that discount (`condition_override`).
   - Else: apply `baseline_discount_pct` (`30%` off, rule `baseline_offset`).
   - Enforce `min_sell_price` (`$5.00`, rule `min_sell_floor`).
4. **Cost-markup floor (when `cost > 0`)**:
   - `cost_floor = cost + max(cost * 25%, $5.00)`.
   - `final = max(tesla_based_price, cost_floor)`. If `cost_floor` wins, rule is `cost_floor`.
5. **Rounding & above-Tesla flag**:
   - Round to `rounding_increment` (`$0.01`, half-up).
   - If `sell_price > tesla_oem_price`, set `above_tesla = true` and attach an advisor warning.

## 4. Worked Examples (Preliminary Defaults)

| Scenario | Tesla OEM (CAD) | Cost (CAD) | Rule Applied | Sell Price (CAD) |
| :--- | :--- | :--- | :--- | :--- |
| Standard part, low aftermarket cost | `$200.00` | `$40.00` | `baseline_offset` (`-30%`) | `$140.00` |
| Standard part, high OEM cost | `$100.00` | `$90.00` | `cost_floor` (`$90 * 1.25`) | `$112.50` (`above_tesla`) |
| Small-ticket part, healthy cost | `$10.00` | `$4.00` | `small_ticket_match` (`100%`) | `$10.00` |
| Small-ticket part, near-retail cost | `$10.00` | `$9.00` | `cost_floor` (`$9 + $5`) | `$14.00` (`above_tesla`) |
| Tiny clip, low cost | `$3.00` | `$1.00` | `cost_floor` (`$1 + $5`) | `$6.00` (`above_tesla`) |
| Discontinued / non-Tesla item | `N/A` | `$25.00` | `cost_plus_fallback` (`+40%`) | `$35.00` |

## 5. Tesla Lookup & Repeat-Quote Workflow

1. **Reuse stored Frappe rate first**: On repeat quotes, if the Frappe `Item` already has a non-zero `standard_rate`, reuse it unless a fresh Tesla price check is requested.
2. **Search Tesla Canada**: When pricing a new or unpriced part, open `https://parts.tesla.com/en-CA/find-part?searchTerm=<query>` using the OEM part number first, or the part name if unlisted or superseded.
3. **Exercise agent discretion**:
   - Confirm the page region is Canada (`en-CA`, `CAD`).
   - Verify vehicle model and generation compatibility (name searches often match multiple models).
   - Resolve superseded part numbers when the original number no longer appears in Tesla's catalog.
4. **Calculate & dispatch**: Run `python -m tools.cli --json price-part ...` (or `ingest-parts` with a `tesla_oem_price` column) and surface the internal breakdown in `### ISM Pre-Dispatch Summary` before dispatching writes.

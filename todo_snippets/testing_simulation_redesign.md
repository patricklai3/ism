# Testing & Simulation Swarm Framework — Two-Stage Execution Plan

Redesign ISM's seeding, test fixtures, and simulation swarm infrastructure into a unified 3-layer stack (**Canonical Master Catalog** $\rightarrow$ **Lifecycle Transaction Engine (`SimBuilder`)** $\rightarrow$ **World Profiles & `monte_carlo(seed)`**), backed by a clean-reinstalled golden database.

## Execution Strategy (`model-triage`)

| Stage | Scope | Execution Mode | Model / Pinning |
| :--- | :--- | :--- | :--- |
| **Stage 1** | Core Foundation (Phases 1–3): Clean site reinstall, Layer 1 master catalog, Golden slot build, Hygiene rule removal + dev docs, `SimBuilder` + `SimManifest` | **Linear sequential** (one owner; tight schema/contract coupling; serializes `test-in-slot.ps1` cleanly) | `Orchestrator (Carbon)` |
| **Stage 2** | Consumers & Edge Discovery (Phase 4): Fixed profiles, `monte_carlo(seed)`, `ism-frappe` test migration, `ism-agent-os` eval/swarm migration | **One `/swarm`** (`workspace: "share"`, 4 parallel workers across `slot-1`..`slot-4` + 1 verifier) | `monitor`: `gemini-4-hydrogen-low`<br>Authors: `gemini-4-hydrogen-medium` / `-high`<br>`verifier`: `gemini-4-carbon-high` (or `argon-high`) |

---

## Locked Architectural Contracts (Freeze Before Coding)

1. **Package Layout:**
   - Reusable seeding/simulation engine lives under `ism-frappe/ism/testing/` (`catalog.py`, `builder.py`, `manifest.py`, `profiles.py`, `monte_carlo.py`, `README.md`).
   - Test runner base & suites stay under `ism-frappe/ism/tests/` (`base.py`, `constants.py`, `test_*.py`).
2. **Zero Prefix Pollution in Isolated DBs:** Canonical master records use realistic shop names (`Lightspeed EV`, `Bay 1 - Main Lift`, `Marcus Vance`), never `_TEST_ISM_` or `Test ` prefixes. Tests creating ephemeral masters still isolate via savepoint rollback / `ISMTestCase.track()`.
3. **`SimManifest` Contract:** Every profile / `monte_carlo(seed)` invocation returns a JSON-serializable `SimManifest` mapping logical handles (`company`, `warehouse`, `bays.*`, `techs.*`, `packages.*`, `customers.*`, `vehicles.*`, `ros.*`, `pos.*`, `schedule_entries.*`, `anchor_date`, `seed`) to exact DB `name`s.
4. **Realistic Synthetic Catalog:** Deterministic PRNG generates realistic EV part numbers, OEM/Aftermarket variants, pricing tiers, FRT hours, and valid 17-character NHTSA check-digit VINs — no dependency on parsing live Tesla manual files.

---

## Stage 1 — Core Foundation (Linear via `Orchestrator (Carbon)`)

### Phase 1: Layer 1 Canonical Master Catalog (`ism/testing/catalog.py`)
- [x] **1.1 Accounting, Tax & Payment Ledger**
  - Seed canonical shop Company (`Lightspeed EV`, `CAD`, `Canada`) via clean install defaults.
  - Ensure 13% HST Sales & Purchase Tax Templates, default `Price List` (`Standard Selling`, `Standard Buying`), and GL-mapped `Mode of Payment` records (`Debit / Interac`, `Credit Card`, `E-Transfer`, `Cash`, `Bank Draft`, `Wire Transfer`) so deposits, checkout, and supplier advances never fail on missing accounts.
  - Seed canonical `ISM Settings` (`08:00`–`17:00`, `12:00`–`12:30` break, `default_slot_interval=30`, P80 `sigma=0.30`, `default_labor_rate=140.0`, `shop_supplies_percentage`, `shop_supplies_cap`, `cost_tolerance_percentage=10.0`, `cost_tolerance_flat_amount=20.0`).
- [x] **1.2 Workforce, Personas & HRMS Leave**
  - Seed role-mapped test Users (`advisor@`, `manager@`, `parts@`, `tech@`) covering `ISM Manager`, `ISM Service Advisor`, `ISM Technician` (`permlevel=1` rate/discount locks).
  - Seed named Technician `Employee` records (e.g., *Marcus Vance*, *Elena Rostova*, *Devon Brooks*, *Noah Patel*, plus 1 `Left` historical tech for inactive filtering).
  - Seed `Holiday List`, `Leave Type` (`Privilege Leave`, `Sick Leave`), and active `Leave Allocation` rows so `Leave Application` records submit cleanly in simulation profiles.
- [x] **1.3 Shop Floor & Bay Equipment**
  - Seed `Equipment Tag` records (`Lift`, `Alignment Rack`, `EV High Voltage`, `HV Battery Station`, `ADAS Calibration Target`, `Tire Mount & Road Force Balancer`).
  - Seed 5 `Service Bay` records (`Bay 1 - Main Lift`, `Bay 2 - Alignment & Suspension`, `Bay 3 - HV & Diagnostics`, `Bay 4 - Tire & Quick Service`, plus `Bay 5 - Offline` with `is_active=0`).
  - Seed canonical `Schedule Entry Type` set (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`).
- [x] **1.4 Warehouses, Suppliers, Realistic Parts & Stock Distribution**
  - Seed Warehouses (`Main Shop Stores`, `Special Order Staging`, `Core Return Cage`).
  - Seed Suppliers (`Tesla OEM Direct`, `EuroEV Aftermarket Distributors`, `Metro Tire & Wheel Wholesale`, `Shop Consumables Co.`).
  - Generate synthetic realistic EV stock `Item`s (`is_stock_item=1`, `is_vehicle_part=1`, realistic `oem_part_number`, `revision`, `part_source` OEM/Aftermarket, `part_condition` New/Reconditioned, `Item Supplier` rows, `valuation_rate`, markup-tiered `standard_rate` aligned with `ism-agent-os/rules/PARTS_PRICING.md`).
  - Seed initial `Stock Reconciliation` into `Main Shop Stores` with deliberate bands: **abundant** (consumables/fluids/filters), **tight** (2 units on select rotors/links to trigger FIFO commitment contention), and **zero** (pyro fuses/subframe arms to force `Waiting on Parts` + PO/RFQ flows).
- [x] **1.5 Nested `Service Package` Catalog & Inspection Templates**
  - Generate synthetic realistic `Service Package` records covering all package mechanics:
    - Symmetrical `LH`/`RH` packages (`is_symmetrical=1`, dual `correction_code`/`rh_correction_code`) with multi-slot `Service Package Item` rows (`slot_number`, `is_default` OEM vs Aftermarket alternates, `side` = `LH`/`RH`/`Both`).
    - Multi-slot overhaul packages (Pads + Rotors + Fluid), equipment-gated packages (Alignment), `is_flat_rate=1` packages, `is_mobile_capable=1` packages, and hourly diagnostic packages.
  - Seed `Inspection Template`s (`Standard Vehicle Walkaround`, `Comprehensive EV Undercarriage & Brake Inspection`).
- [x] **1.6 Customers, Multi-Owner Vehicles & Deferred Work**
  - Seed retail individual `Customer`s (with `Contact` phone/email & `Address`), a multi-driver household linked via `Repair Vehicle Customer`, and a commercial fleet account (*Metro Green Rideshare Fleet*, 5 EVs).
  - Generate valid 17-character NHTSA check-digit VINs (`Repair Vehicle`) across Models 3/Y/S/X and seed `Vehicle Deferred Service` rows (`Open Recommendation`, `safety_hazard=1`) on select vehicles.

### Phase 2: Clean Site Reinstall, Golden Slot Pipeline & Documentation Cleanup
- [x] **2.1 Clean Site Reinstall & Decoupled Golden Build**
  - Reinstall a clean site (`bench --site <site> --force reinstall`) or add a clean-build mode in `ism-frappe/scripts/wsl/provision-slots.sh` / `provision-bench-slots.ps1` that builds `main_golden.sql.gz` strictly from a freshly installed site + `ism.testing.catalog.seed_all()` — zero `Prestiga-Biz`, zero `RO-2026-00067`, zero `Test ` legacy fixtures.
  - Update `ism-frappe/scripts/promote-to-main.ps1` so refreshing `main_golden.sql.gz` preserves clean master isolation.
- [x] **2.2 Remove Legacy Hygiene & Constants Bloat**
  - Delete `.agents/rules/test-data-hygiene.md` completely so test turns stop paying context-window tax on teardown boilerplate and hardcoded live records.
  - Strip `Prestiga-Biz`, `LRW3F7*`, `RO-2026-00067`, `ACC-SINV-*`, `ACC-PAY-*`, and `LEGACY_TEST_PREFIX` from `ism-frappe/ism/tests/constants.py`.
  - Demote `ism-frappe/ism/tests/cleanup.py` to an optional manual admin tool (remove reliance from normal tests).
  - Refactor `ism-frappe/ism/tests/base.py` (`ISMTestCase`) to remove legacy `Quotation` / `custom_vehicle` helpers, rely on clean Golden Layer 1 masters + Frappe savepoint rollback + `SimBuilder` helpers.
- [x] **2.3 Developer Documentation (Non-Polluting)**
  - Create `ism-frappe/ism/testing/README.md` documenting: Layer 1 master topology, `ISMTestCase` isolation contract, `SimBuilder` fluent API, `SimManifest` handles, `monte_carlo(seed)` usage, and clean-reinstall golden slot workflow.
  - Update `ism-frappe/scripts/README.md` to document the clean golden site reinstall/provisioning flow.

### Phase 3: Layer 2 Lifecycle Transaction Engine (`ism/testing/builder.py` & `manifest.py`)
- [x] **3.1 Seeded Context & Anchor Clock**
  - Implement `SimContext(seed: int, anchor_date: str)` wrapping `random.Random(seed)` and relative date helpers (`today`, `tomorrow`, `T-N`, `T+N`).
- [x] **3.2 Fluent State-Machine `SimBuilder`**
  - Implement target-stage RO builder walking real domain APIs (`RepairOrder.insert`, `add_service_package`, `authorize_jobs`, `submit_procurement_orders`, `create_purchase_invoice`, `ScheduleEntry.insert`, `RepairOrderTimeLog`, `add_supplement`, `record_deposit`, `process_payment_and_release`):
    - `checked_in` / `draft_estimate`
    - `pending_authorization` (valid token + expiry)
    - `authorized_in_stock` (FIFO bin commitment active)
    - `waiting_on_parts` (optional open `Purchase Order` + advance `Payment Entry` or `RFQ`)
    - `partially_approved_with_deferred` (declined rows synced to `Vehicle Deferred Service`)
    - `scheduled_in_progress` (linked `Schedule Entry` + `Repair Order Time Log` + optional supplement / `Shop Action Item`)
    - `billed_paid` / `closed_released` (submitted `Sales Invoice` + `Payment Entry` + `odometer_out`)
- [x] **3.3 Historical Telemetry Seeder (P80 Estimation Data)**
  - Add `seed_historical_telemetry(n_closed_ros=30)` generating closed ROs across `T-60d .. T-1d` with log-normal `actual_duration_hours` so `estimation_service.py` computes real empirical P80 durations.

**Stage 1 Gate:** Reinstall clean site $\rightarrow$ build `main_golden.sql.gz` $\rightarrow$ run `test-in-slot.ps1` unit test verifying Layer 1 masters, nested `Service Package` expansion, `SimBuilder` all-stage transitions, and `SimManifest` schema.

---

## Stage 2 — Profiles, Monte Carlo Discovery & Migration (One `/swarm`)

**Topology:** `workspace: "share"`, 4 parallel authors across `slot-1`..`slot-4` + 1 `monitor` + 1 cross-family `verifier`.

### Worker 1: `profiles-and-monte-carlo`
- **Owns:**
  - `ism-frappe/ism/testing/profiles.py` *(new)*
  - `ism-frappe/ism/testing/monte_carlo.py` *(new)*
  - `ism-frappe/ism/tests/test_simulation_framework.py` *(new)*
- **Tasks:**
  - Implement fixed profiles executable via `bench --site <site> execute ism.testing.profiles.seed_profile`:
    - `clean_master` (Layer 1 only)
    - `eval_standard` (replaces `eval_nl_loop.py` ad-hoc REST setup: 30 historical ROs, tomorrow's conflict + Elena leave, unscheduled approved ROs, open PO with Bank Draft advance, zero-stock RO)
    - `swarm_crunch_day` (85% bay load, competing FIFO parts commitments, over-tolerance supplements, fleet batch intake)
  - Implement parameterized `monte_carlo(seed, **params)` generator varying:
    - Arrival rate & walk-ins vs scheduled appointments
    - Parts stockout probability & FIFO race contention
    - Supplement frequency & cost-tolerance breach probability
    - Technician leave / mid-day absence & bay outage rates
    - Customer partial declines, no-shows, deposits, and split-tender checkouts
  - Write unit tests proving determinism (same `seed` $\rightarrow$ identical `SimManifest` & DB counts) and invariant safety across multiple seeds (`seed=1..10`).

### Worker 2: `frappe-tests-ops`
- **Owns:**
  - `ism-frappe/ism/tests/test_scheduling_capacity.py`
  - `ism-frappe/ism/tests/test_ro_scheduling_e2e.py`
  - `ism-frappe/ism/tests/test_estimation_math.py`
  - `ism-frappe/ism/ism/doctype/schedule_entry/test_schedule_entry.py`
  - `ism-frappe/ism/ism/doctype/service_bay/test_service_bay.py`
  - `ism-frappe/ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py`
  - `ism-frappe/ism/ism/doctype/ism_settings/test_ism_settings.py`
- **Tasks:**
  - Migrate off legacy `test_fixtures.py` (`Test Bay 1`, `Test Tech One`, `EST_ITEM_*`) onto Layer 1 Golden masters + `SimBuilder` + `seed_historical_telemetry`.

### Worker 3: `frappe-tests-commerce`
- **Owns:**
  - `ism-frappe/ism/tests/test_parts_management.py`
  - `ism-frappe/ism/tests/test_pricing_and_discounts.py`
  - `ism-frappe/ism/tests/test_billing_and_deposits.py`
  - `ism-frappe/ism/tests/test_approval_and_inspection.py`
  - `ism-frappe/ism/tests/test_agent_facade.py`
  - `ism-frappe/ism/tests/test_mcp_protocol.py`
  - `ism-frappe/ism/tests/test_tesla_integration.py`
  - `ism-frappe/ism/ism/doctype/repair_order/test_repair_order.py`
  - `ism-frappe/ism/ism/doctype/repair_vehicle/test_repair_vehicle.py`
  - Deprecate/delete `ism-frappe/ism/tests/test_fixtures.py` once no callers remain.
- **Tasks:**
  - Migrate commerce, parts, pricing, billing, approval, and facade tests onto nested `Service Package`s, stocked `Item`s, `CAD` + 13% HST, and `SimBuilder` stages.

### Worker 4: `agent-os-evals-and-swarms`
- **Owns:**
  - `ism-agent-os/scripts/eval_nl_loop.py`
  - `ism-agent-os/scripts/eval_scenarios.py`
  - `ism-agent-os/scripts/demo_shop_flow.py`
  - `ism-agent-os/scripts/sim_runner.py` *(new: `monte_carlo(seed)` batch/swarm evaluation harness)*
  - `ism-agent-os/tests/` *(any scenario/eval unit tests)*
- **Tasks:**
  - Replace `_ensure_*` REST seeding and hardcoded IDs (`RO-2026-00011`, `HR-EMP-00005`, `_EVAL_*`) with `eval_standard` profile seeding + `SimManifest` handle interpolation in prompts and DB verifiers.
  - Add `sim_runner.py` to run Agent OS optimization sweeps across `monte_carlo(seed=1..K)` worlds on bench slots and score edge-case resilience (conflicts, stockouts, tolerance overrides, leave collisions).

### Worker 5: `verifier` (`gemini-4-carbon-high` or `gemini-4-argon-high`)
- **Owns:** Read-only verification across merged Stage 2 branches.
- **Tasks:**
  - Run full `ism-frappe` test suite via `test-in-slot.ps1 -FreshDb`.
  - Run `ism-agent-os` unit tests + `eval_scenarios.py` + `monte_carlo` smoke seeds (`seed=1,2,3`).
  - Confirm `.agents/rules/test-data-hygiene.md` is gone, zero `LEGACY_TEST_PREFIX` / `Prestiga-Biz` references remain, and `ism/testing/README.md` accurately matches implemented APIs.

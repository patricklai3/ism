#!/usr/bin/env python3
"""Blinded natural-language CLI evaluation loop: 40 scenarios across SimManifest worlds, --stage submitter, and KPI scoring."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

from scripts.sim_runner import (  # noqa: E402
	PROFILE_EXECUTE_FNS,
	configure_slot_dotenv,
	default_offline_crunch_manifest,
	default_offline_eval_manifest,
	interpolate_manifest,
	provision_agent_api_keys_in_slot,
	resolve_manifest_arg,
	resolve_manifest_path,
	restore_slot_dotenv,
	seed_profile_in_slot,
	start_slot_server,
	stop_slot_server,
)
from tools.cli import (  # noqa: E402
	READ_ONLY_COMMANDS,
	_batch_item_to_argv,
	_build_parser as _build_cli_parser,
	_load_batch_items,
)
from tools.client import IsmAgentClient  # noqa: E402
from tools.config import IsmConfig  # noqa: E402
from tools.transport import IsmApiError  # noqa: E402

_RESULTS_DIR = Path(__file__).resolve().parent / "eval_results"
_RO_PATTERN = re.compile(r"\bRO-(?:\d{4}-\d+|[A-Z]+-\d+(?:-\d+)?)\b")
_SE_PATTERN = re.compile(r"\bSE-(?:[A-Z]+-)?\d+(?:-\d+)?\b")
_PO_PATTERN = re.compile(r"\b(?:PUR-ORD-\d{4}-\d+|PO-(?:SIM|MC)-\d+(?:-\d+)?)\b")
_PI_PATTERN = re.compile(r"\b(?:ACC-)?PINV-\d{4}-\d+\b")
_PE_PATTERN = re.compile(r"\b(?:ACC-PAY-\d{4}-\d+|PE-(?:SIM|MC)-\d+(?:-\d+)?)\b")
_SI_PATTERN = re.compile(r"\b(?:ACC-)?SINV-\d{4}-\d+\b")
_URL_PATTERN = re.compile(r"https?://[^\s\)\]>\"']+")
_STAGE_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:\d+/\?id=[A-Za-z0-9_-]+&token=[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class NLScenario:
	"""Definition of a single blinded natural-language evaluation scenario."""

	id: str
	category: str
	nl_prompt: str
	expected_commands: list[str]
	optimal_cli_calls: int
	allowed_dry_runs: int = 0
	is_conflict_scenario: bool = False
	allows_server_feedback_retry: bool = False
	notes: str = ""
	profile: str = "eval_standard"
	persona: str = "dispatcher"
	wave: int = 1
	tier: str = "full"
	allowed_staged_calls: int = 0


def _render_prompt_dates(template: str, *, relative_dates: bool) -> str:
	if relative_dates:
		return (
			template.replace("{on_target}", "tomorrow")
			.replace("{target_possessive}", "tomorrow's")
			.replace("{target_bare}", "tomorrow")
		)
	return (
		template.replace("{on_target}", "on {{metadata.target_date}}")
		.replace("{target_possessive}", "{{metadata.target_date}}'s")
		.replace("{target_bare}", "{{metadata.target_date}}")
	)


def _scenario_templates() -> list[NLScenario]:
	return [
		# =====================================================================
		# PART A: eval_standard (S01 - S30)
		# Bucket 1: Floor Telemetry, Capacity & Temporal Windowing (Wave 1)
		# =====================================================================
		NLScenario(
			id="S01_shop_status_relative",
			category="status_relative_date",
			nl_prompt="Hey, what's on the shop floor {on_target}?",
			expected_commands=["status"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="core",
			notes="Target date should map directly to status --date <YYYY-MM-DD> with no extra tech-overview.",
		),
		NLScenario(
			id="S02_tech_util_leave",
			category="tech_overview_leave",
			nl_prompt="Who's on leave {on_target} and how busy are the techs?",
			expected_commands=["tech-overview"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="core",
			notes="Use tech-overview --date <YYYY-MM-DD> and surface Elena on leave + tech utilization.",
		),
		NLScenario(
			id="S03_slots_alignment_rack",
			category="equipment_slots",
			nl_prompt="Customer needs a 1-hour alignment rack slot {on_target}—what's open?",
			expected_commands=["slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="core",
			notes="Call slots --date <YYYY-MM-DD> --duration 60 --tags 'Alignment Rack'.",
		),
		NLScenario(
			id="S04_slots_afternoon_lift",
			category="vague_window_slots",
			nl_prompt="Do we have any 90-minute lift bay openings {target_bare} afternoon?",
			expected_commands=["slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="full",
			notes="Vague afternoon window must query slots --duration 90 --tags Lift --min-time 13:00:00 instead of guessing a fixed time.",
		),
		NLScenario(
			id="S05_unscheduled_queue",
			category="unscheduled_queue",
			nl_prompt="What services on {{ros.unscheduled_ro}} still need a bay?",
			expected_commands=["unscheduled"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="core",
			notes="Direct unscheduled --repair-order {{ros.unscheduled_ro}}.",
		),
		# Bucket 2: Vehicle Intel, Plate Search, Alternates & Batch Reads (Wave 1)
		NLScenario(
			id="S06_vin_decode_history",
			category="vin_and_vehicle_intel",
			nl_prompt="Check VIN {{vehicles.veh_model3_liam}} and pull its service history.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="advisor",
			wave=1,
			tier="core",
			notes="Optimal: 1 batch call combining decode-vin + vehicle-info (no search-vehicles).",
		),
		NLScenario(
			id="S07_plate_search_deferred",
			category="plate_and_deferred_hazards",
			nl_prompt="Look up license plate LEV-301 and list any open deferred recommendations or safety hazards.",
			expected_commands=["search-vehicles", "deferred"],
			optimal_cli_calls=2,
			allowed_dry_runs=0,
			persona="advisor",
			wave=1,
			tier="full",
			notes="Find vehicle by plate LEV-301 and surface open safety_hazard=1 deferred fore-link recommendation.",
		),
		NLScenario(
			id="S08_batch_three_reads",
			category="batching",
			nl_prompt="Check {target_possessive} shop status, tech overview, and unscheduled queue for {{ros.unscheduled_ro}}.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="dispatcher",
			wave=1,
			tier="core",
			notes="Pick 1 batch call combining status, tech-overview, and unscheduled.",
		),
		NLScenario(
			id="S09_alternates_and_templates",
			category="alternates_and_inspections",
			nl_prompt="For {{packages.front_brake_overhaul}} slot 2, what OEM and aftermarket rotor alternates exist, and what check-in inspection templates do we have?",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="parts",
			wave=1,
			tier="full",
			notes="Combine alternates --service-package ... --slot-number 2 and inspection-templates in 1 batch.",
		),
		# Bucket 3: Tesla-Offset Parts Pricing Policy (Wave 1)
		NLScenario(
			id="S10_pricing_baseline_small_ticket",
			category="parts_pricing_baseline_small",
			nl_prompt=(
				"Give customer sell prices (CAD) for two parts: (1) New Aftermarket Aero Duct (our cost $40, Tesla Canada OEM $200) "
				"and (2) New OEM Trim Clip (our cost $4, Tesla Canada OEM $10). Keep any Tesla comparison internal—do not mention Tesla OEM prices in customer quote text."
			),
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="parts",
			wave=1,
			tier="full",
			notes="Verify price-part computes $140.00 (baseline_offset) and $10.00 (small_ticket_match) with zero Tesla leakage.",
		),
		NLScenario(
			id="S11_pricing_cost_floor_fallback",
			category="parts_pricing_floor_fallback",
			nl_prompt=(
				"Calculate customer sell prices (CAD) for: (1) New Aftermarket Skid Shield (our cost $95, Tesla Canada OEM $160, part 1499151-00-B) "
				"and (2) unindexed shop sensor fitting (our cost $25, no Tesla OEM price available). Keep internal pricing audit separate from customer quote text."
			),
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="parts",
			wave=1,
			tier="full",
			notes="Verify price-part enforces cost_floor ($118.75 > $112.00) and cost_plus_fallback ($35.00).",
		),
		# Bucket 4: Advisor Intake, $stepN Chaining, Symmetrical LH/RH, Inspections & Deposits (Wave 2)
		NLScenario(
			id="S12_composite_intake_slots",
			category="composite_intake_slots",
			nl_prompt=(
				"Create a draft quote for {{customers.retail_1}}'s Model 3 ({{vehicles.veh_model3_liam}}, odometer 48,200 km) "
				"for Front Brake Inspection and find {target_bare} morning slots with Marcus in {{bays.main_lift}}."
			),
			expected_commands=["intake-slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="core",
			notes="Composite intake-slots (--stage or --dry-run -> live) with zero follow-up slots.",
		),
		NLScenario(
			id="S13_quote_amend_approval",
			category="quote_amend_approval",
			nl_prompt=(
				"Create a quote for {{customers.retail_1}} on {{vehicles.veh_model3_liam}} (odometer 48,200 km) for Tire Rotation (0.5h), "
				"add Wiper Blade Replacement (0.3h), and get an approval link."
			),
			expected_commands=["quote", "amend-quote", "send-approval"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="core",
			notes="Preferred: 1 --stage batch ($step0.data.name / $prev.data.name); legacy: quote + batch(amend-quote + send-approval).",
		),
		NLScenario(
			id="S14_multi_service_quote_estimate",
			category="multi_service_quote_estimate",
			nl_prompt=(
				"Quote {{customers.retail_1}}'s {{vehicles.veh_model3_liam}} (odometer 48,200 km) for Brake Fluid Flush (1.0h) "
				"and Cabin Air Filter Replacement (0.5h), and give me the P80 estimated shop time and customer total with tax."
			),
			expected_commands=["quote", "estimate"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="core",
			notes="Quote (--stage or live) followed by estimate (using @last.repair_order or RO ID).",
		),
		NLScenario(
			id="S15_symmetrical_lh_rh_quote",
			category="symmetrical_service_duplication",
			nl_prompt=(
				"Create a draft quote for {{customers.retail_1}} on {{vehicles.veh_model3_liam}} (odometer 48,400 km) for "
				"{{packages.symmetrical_fore_link}}, and duplicate that symmetrical link job for the opposite RH side."
			),
			expected_commands=["quote", "duplicate-side"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="full",
			notes="Symmetrical LH/RH package expansion + duplicate-side (supports --stage batch or live sequence).",
		),
		NLScenario(
			id="S16_household_deferred_attach",
			category="household_deferred_attach",
			nl_prompt=(
				"Create a draft quote for {{customers.household_primary}} on {{vehicles.veh_modelx_household}} (odometer 52,450 km) "
				"for {{packages.wiper_blade_service}} and attach any open safety-hazard deferred brake recommendation for that vehicle."
			),
			expected_commands=["deferred", "quote", "add-deferred"],
			optimal_cli_calls=2,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="full",
			notes="Check deferred items for veh_modelx_household, then quote + add-deferred.",
		),
		NLScenario(
			id="S17_quote_apply_inspection",
			category="inspection_template_apply",
			nl_prompt=(
				"Create a draft quote for {{customers.retail_2}} on {{vehicles.veh_modely_sophia}} (odometer 34,150 km) "
				"with concern 'Regen braking clunk over bumps' and apply the {{inspection_templates.undercarriage_brake}} checklist."
			),
			expected_commands=["quote", "apply-inspection"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="full",
			notes="Quote + apply-inspection (supports 1 --stage batch with $step0.data.name).",
		),
		NLScenario(
			id="S18_quote_and_deposit",
			category="quote_and_customer_deposit",
			nl_prompt=(
				"Create a quote for {{customers.retail_3}} on {{vehicles.veh_models_owen}} (odometer 27,690 km) for "
				"{{packages.four_wheel_alignment}} and record a $100 Debit / Interac deposit (ref DEP-401)."
			),
			expected_commands=["quote", "deposit"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="full",
			notes="Flat-rate quote + $100 Debit / Interac Payment Entry deposit.",
		),
		# Bucket 5: Dispatcher Booking, Rescheduling, Walk-In Conversion & Traps (Wave 2)
		NLScenario(
			id="S19_direct_book_human_names",
			category="direct_booking",
			nl_prompt="Book {{ros.unscheduled_ro}} into {{bays.main_lift}} {on_target} at 1pm with Devon for 60 min.",
			expected_commands=["book"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="dispatcher",
			wave=2,
			tier="core",
			notes="Direct book (--stage or --dry-run -> live) with ISO date & 13:00:00.",
		),
		NLScenario(
			id="S20_conflict_and_recovery",
			category="conflict_recovery",
			nl_prompt="Book {{ros.unscheduled_ro}} into {{bays.main_lift}} {on_target} at 9am with Marcus.",
			expected_commands=["book"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			persona="dispatcher",
			wave=2,
			tier="core",
			notes="Book occupied slot ({{ros.conflict_ro}}) without pre-slots; surface 409 conflict + alternatives + deep link.",
		),
		NLScenario(
			id="S21_ambiguous_bay_recovery",
			category="ambiguous_resource_edge",
			nl_prompt=(
				"Schedule a walk-in diagnostic for {{customers.retail_3}} (2022 Tesla Model Y) {on_target} at 10am "
				"in 'Bay' for 60 min—if 'Bay' is ambiguous, pick an open lift bay."
			),
			expected_commands=["book"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			allowed_staged_calls=2,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			persona="dispatcher",
			wave=2,
			tier="core",
			notes="Direct book --bay 'Bay' (417) -> pick [Available] Lift bay without pre-querying slots.",
		),
		NLScenario(
			id="S22_leave_and_lunch_trap",
			category="leave_and_break_rejection",
			nl_prompt="Book {{ros.unscheduled_secondary_ro}} into {{bays.alignment}} {on_target} at 12:00pm with Elena for 60 min.",
			expected_commands=["book"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			persona="dispatcher",
			wave=2,
			tier="full",
			notes="Dual trap: Elena on approved leave + 12:00-12:30 break window; must not create a 12:00 Schedule Entry.",
		),
		NLScenario(
			id="S23_reschedule_and_confirm_batch",
			category="reschedule_and_confirm",
			nl_prompt="Move {{schedule_entries.hv_morning}} to 11:00am {on_target} and mark {{schedule_entries.conflict_main_lift}} as Confirmed.",
			expected_commands=["reschedule", "appointment-status"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="dispatcher",
			wave=2,
			tier="full",
			notes="Combine reschedule + appointment-status --status Confirmed in 1 batch.",
		),
		NLScenario(
			id="S24_walkin_convert_missing_odometer",
			category="walkin_convert_odometer_gate",
			nl_prompt=(
				"Book a walk-in diagnostic for {{customers.retail_2}} (2024 Tesla Model Y) {on_target} at 2:30pm in {{bays.tire_quick}} for 60 min, "
				"and convert that Schedule Entry into a Repair Order."
			),
			expected_commands=["book", "create-ro-from-entry"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="dispatcher",
			wave=2,
			tier="full",
			notes="Must flag odometer_in [MISSING — REQUIRED] on create-ro-from-entry without guessing; operator supplies 61,200 km.",
		),
		# Bucket 6: Anti-Hallucination & Legal Authorization Locks (Wave 2)
		NLScenario(
			id="S25_missing_field_write_gate",
			category="missing_field_write_gate",
			nl_prompt="Create a draft quote for {{customers.retail_1}} on {{vehicles.veh_model3_liam}} for Cabin Air Filter Replacement (0.5h).",
			expected_commands=["quote"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="advisor",
			wave=2,
			tier="core",
			notes="Odometer omitted: must flag odometer_in [MISSING — REQUIRED] without guessing; operator supplies 54,100 km.",
		),
		NLScenario(
			id="S26_unapproved_start_lock",
			category="authorization_lock_guard",
			nl_prompt=(
				"Create a draft quote for {{customers.household_secondary}} on {{vehicles.veh_model3_household}} (odometer 19,820 km) "
				"for {{packages.wiper_blade_service}} and start bay work on it right away before customer approval."
			),
			expected_commands=["quote"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="technician",
			wave=2,
			tier="full",
			notes="Create draft quote ONLY and refuse job-status --action start because approval_status is Estimating (Invariant #6).",
		),
		# Bucket 7: Parts Procurement, Supplier Guards & 2-Step Invoicing (Wave 2)
		NLScenario(
			id="S27_standalone_procure_advance_pay",
			category="standalone_procure_advance_pay",
			nl_prompt=(
				"Order 2 units of Zimmermann Coated Front Brake Rotor (item {{items.front_rotor_aft}}) from {{suppliers.euro_aftermarket}} "
				"for shop stock (no RO) at $88 each, paid in full today via Bank Draft (ref BD-9001)."
			),
			expected_commands=["procure"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="parts",
			wave=2,
			tier="core",
			notes="Standalone procure omitting --repair-order and omitting --paid-amount so full 13% HST grand_total is paid.",
		),
		NLScenario(
			id="S28_purchase_invoice_with_stock",
			category="purchase_invoice_with_stock",
			nl_prompt=(
				"The parts for our open {{suppliers.tesla_oem}} Purchase Order ({{pos.eval_open_po}}) just arrived with supplier invoice INV-9001 "
				"inside the box—post the Purchase Invoice and update stock in one step."
			),
			expected_commands=["purchase-invoice"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			persona="parts",
			wave=2,
			tier="core",
			notes="0 or 1 filtered list-docs + purchase-invoice --update-stock --bill-no INV-9001 attaching PO advance.",
		),
		NLScenario(
			id="S29_wrong_supplier_po_stop",
			category="supplier_po_guard",
			nl_prompt="Receive the open Purchase Order from {{suppliers.shop_consumables}}.",
			expected_commands=["list-docs"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			persona="parts",
			wave=2,
			tier="full",
			notes="Single filtered list-docs returns 0 open POs for Shop Consumables Co.; must STOP and never substitute Tesla OEM Direct's PO.",
		),
		NLScenario(
			id="S30_two_step_electronic_invoice_receive",
			category="electronic_invoice_then_receive",
			nl_prompt=(
				"Order 4 bottles of DOT 4 Brake Fluid (item {{items.dot4_brake_fluid}}) from {{suppliers.shop_consumables}} at $18 each for stock, "
				"post electronic supplier invoice INV-9020 now (goods arriving later), and then record the Purchase Receipt when goods arrive."
			),
			expected_commands=["procure", "purchase-invoice", "receive-po"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			persona="parts",
			wave=2,
			tier="full",
			notes="Scenario A 2-step flow: procure -> purchase-invoice (update_stock=0) -> receive-po.",
		),
		# =====================================================================
		# PART B: swarm_crunch_day High-Load Stress World (C01 - C10)
		# =====================================================================
		NLScenario(
			id="C01_crunch_executive_triage",
			category="executive_floor_triage",
			nl_prompt="Give me an executive briefing for {target_bare}: bay occupancy, tech utilization, and all open shop action item escalations.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			profile="swarm_crunch_day",
			persona="executive",
			wave=1,
			tier="full",
			notes="1 batch (status + tech-overview + action-items --status Open) surfacing ~85.3% load and 2 over-tolerance items.",
		),
		NLScenario(
			id="C02_fleet_wip_and_deferred",
			category="executive_fleet_rollup",
			nl_prompt="Pull all active Repair Orders for commercial fleet {{customers.fleet_1}} and check open deferred work on {{vehicles.veh_fleet_1}}.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			profile="swarm_crunch_day",
			persona="executive",
			wave=1,
			tier="full",
			notes="1 batch (list-docs filtered by fleet_1 + deferred --vehicle veh_fleet_1) rolling up 5 fleet ROs.",
		),
		NLScenario(
			id="C03_packed_floor_afternoon_slot",
			category="packed_floor_slot_search",
			nl_prompt="Can we squeeze in a 60-minute lift bay job {target_bare} afternoon?",
			expected_commands=["slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			profile="swarm_crunch_day",
			persona="dispatcher",
			wave=1,
			tier="full",
			notes="Query slots --tags Lift --min-time 13:00:00 and surface only post-15:45 openings.",
		),
		NLScenario(
			id="C04_fifo_stock_contention_procure",
			category="fifo_deficit_procurement",
			nl_prompt=(
				"Check parts stock on {{ros.crunch_fifo_loser}} to see why it's short, and stage/submit a Purchase Order "
				"to {{suppliers.tesla_oem}} for the missing rotors linked to that RO."
			),
			expected_commands=["check-stock", "procure"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="parts",
			wave=2,
			tier="full",
			notes="Diagnose tight-band FIFO front_rotor_oem deficit (2 units) via check-stock, then procure --repair-order.",
		),
		NLScenario(
			id="C05_floor_supplement_over_tolerance",
			category="mid_job_supplement",
			nl_prompt="Log a mid-job supplement on {{ros.crunch_hv_1}} for 'Corroded HV Ground Strap Replacement' taking 1.5 labor hours.",
			expected_commands=["add-supplement"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="technician",
			wave=2,
			tier="full",
			notes="add-supplement creates $210 supplemental row (> max(10%, $20) tolerance).",
		),
		NLScenario(
			id="C06_supplement_approval_resolve_action",
			category="supplement_approval_and_resolve",
			nl_prompt="Send the customer approval link for {{ros.crunch_fleet_1}}'s supplement and resolve open action item {{action_items.crunch_over_tol_1}}.",
			expected_commands=["send-approval", "resolve-action"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="executive",
			wave=2,
			tier="full",
			notes="1 batch (send-approval --medium link + resolve-action).",
		),
		NLScenario(
			id="C07_parts_hold_check_and_action",
			category="floor_parts_hold_escalation",
			nl_prompt="Put {{ros.crunch_hv_2}} on parts hold, check its parts stock, and log an open action item titled 'HV Connector Backorder'.",
			expected_commands=["job-status", "check-stock", "create-action"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="technician",
			wave=2,
			tier="full",
			notes="Batch job-status --action hold + check-stock + create-action.",
		),
		NLScenario(
			id="C08_floor_complete_missing_odometer_gate",
			category="complete_missing_odometer_gate",
			nl_prompt="Check off all completed services on {{ros.crunch_tire_2}} and mark the job Work Completed.",
			expected_commands=["get-doc", "toggle-service", "job-status"],
			optimal_cli_calls=2,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="technician",
			wave=2,
			tier="full",
			notes="Must flag odometer_out [MISSING — REQUIRED] on job-status --action complete; operator supplies 48,335 km.",
		),
		NLScenario(
			id="C09_checkout_missing_payment_release",
			category="checkout_missing_payment_gate",
			nl_prompt="Finish all services on {{ros.crunch_tire_3}} (odometer out 34,235 km) and check out the customer in full.",
			expected_commands=["get-doc", "toggle-service", "checkout"],
			optimal_cli_calls=2,
			allowed_dry_runs=2,
			allowed_staged_calls=1,
			profile="swarm_crunch_day",
			persona="technician",
			wave=2,
			tier="full",
			notes="Must flag payment_method [MISSING — REQUIRED] without guessing; operator supplies Credit Card + --confirm.",
		),
		NLScenario(
			id="C10_unapproved_supplement_checkout_block",
			category="unapproved_supplement_guard",
			nl_prompt=(
				"Complete and check out {{ros.crunch_align_supp}} (odometer out 27,715 km, Debit / Interac) right now "
				"even though its supplemental camber bolt finding is still awaiting approval."
			),
			expected_commands=["get-doc"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			allowed_staged_calls=1,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			profile="swarm_crunch_day",
			persona="technician",
			wave=2,
			tier="full",
			notes="Verify unapproved Estimating supplement prevents premature Closed / Released checkout.",
		),
	]


def _merge_manifest_defaults(base: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
	merged = dict(base)
	for bucket in ("ros", "schedule_entries", "action_items", "vehicles", "items", "packages", "customers", "suppliers", "bays", "techs", "pos", "metadata"):
		base_sub = merged.get(bucket)
		def_sub = defaults.get(bucket)
		if isinstance(def_sub, dict):
			combined = dict(def_sub)
			if isinstance(base_sub, dict):
				combined.update(base_sub)
			merged[bucket] = combined
	return merged


def get_scenarios(
	manifest: Optional[dict[str, Any]] = None,
	*,
	profile: Optional[str] = None,
	tier: str = "full",
	skill: Optional[str] = None,
	wave: Optional[int] = None,
	ids: Optional[list[str]] = None,
	relative_dates: bool = False,
) -> list[NLScenario]:
	"""Return filtered blinded NL evaluation scenarios interpolated against manifest."""
	if manifest is not None:
		m = manifest
	elif profile == "swarm_crunch_day":
		m = default_offline_crunch_manifest()
	else:
		m = default_offline_eval_manifest()

	resolved_date = str(((m.get("metadata") or {}).get("target_date")) or "") or None
	eval_m = m if "unscheduled_ro" in (m.get("ros") or {}) else _merge_manifest_defaults(m, default_offline_eval_manifest(target_date=resolved_date))
	crunch_m = m if "crunch_fifo_loser" in (m.get("ros") or {}) else _merge_manifest_defaults(m, default_offline_crunch_manifest(target_date=resolved_date))

	resolved_profile = profile or str(m.get("profile") or "eval_standard")
	id_filter = {s.strip() for s in (ids or []) if s.strip()}

	scenarios: list[NLScenario] = []
	for sc in _scenario_templates():
		if resolved_profile != "all" and sc.profile != resolved_profile:
			continue
		if tier == "core" and sc.tier != "core":
			continue
		if skill and sc.persona != skill:
			continue
		if wave is not None and sc.wave != wave:
			continue
		if id_filter and sc.id not in id_filter and not any(sc.id.startswith(tok + "_") or sc.id == tok for tok in id_filter):
			continue

		active_m = crunch_m if sc.profile == "swarm_crunch_day" else eval_m
		prompt_tpl = _render_prompt_dates(sc.nl_prompt, relative_dates=relative_dates)
		notes_tpl = _render_prompt_dates(sc.notes, relative_dates=relative_dates)
		scenarios.append(
			NLScenario(
				id=sc.id,
				category=sc.category,
				nl_prompt=interpolate_manifest(prompt_tpl, active_m),
				expected_commands=list(sc.expected_commands),
				optimal_cli_calls=sc.optimal_cli_calls,
				allowed_dry_runs=sc.allowed_dry_runs,
				is_conflict_scenario=sc.is_conflict_scenario,
				allows_server_feedback_retry=sc.allows_server_feedback_retry,
				notes=interpolate_manifest(notes_tpl, active_m),
				profile=sc.profile,
				persona=sc.persona,
				wave=sc.wave,
				tier=sc.tier,
				allowed_staged_calls=sc.allowed_staged_calls,
			)
		)
	return scenarios


def _resolve_target_date(client: Optional[IsmAgentClient], manifest: Optional[dict[str, Any]] = None) -> str:
	"""Resolve target_date from manifest metadata, server today()+1, or local tomorrow."""
	if manifest is not None:
		meta = manifest.get("metadata") or {}
		if isinstance(meta.get("target_date"), str) and meta["target_date"]:
			return meta["target_date"]
	if client is not None:
		try:
			res = client.get_shop_floor_status()
			d = res.get("data", {}).get("date")
			if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
				return (date.fromisoformat(d) + timedelta(days=1)).isoformat()
		except Exception:
			pass
	return (date.today() + timedelta(days=1)).isoformat()


def _extract_claimed_ids(entry: dict[str, Any]) -> dict[str, list[str]]:
	"""Extract RO, SE, PO, PI, PE, SI, and URL IDs mentioned in final_answer or command summaries."""
	chunks = [str(entry.get("final_answer") or "")]
	for cmd in entry.get("commands_run") or []:
		chunks.append(str(cmd.get("summary") or ""))
		chunks.append(str(cmd.get("stdout_summary") or ""))
	blob = "\n".join(chunks)
	return {
		"repair_orders": list(dict.fromkeys(_RO_PATTERN.findall(blob))),
		"schedule_entries": list(dict.fromkeys(_SE_PATTERN.findall(blob))),
		"purchase_orders": list(dict.fromkeys(_PO_PATTERN.findall(blob))),
		"purchase_invoices": list(dict.fromkeys(_PI_PATTERN.findall(blob))),
		"payment_entries": list(dict.fromkeys(_PE_PATTERN.findall(blob))),
		"sales_invoices": list(dict.fromkeys(_SI_PATTERN.findall(blob))),
		"urls": list(dict.fromkeys(_URL_PATTERN.findall(blob))),
		"stage_urls": list(dict.fromkeys(_STAGE_URL_PATTERN.findall(blob))),
	}


def _safe_resolve(manifest: dict[str, Any], path: str, fallback: str = "") -> str:
	try:
		val = resolve_manifest_path(manifest, path)
		return str(val) if val is not None else fallback
	except KeyError:
		return fallback


def verify_scenario_db(
	client: IsmAgentClient,
	scenario: NLScenario,
	entry: dict[str, Any],
	target_date: Optional[str] = None,
	manifest: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
	"""Verify one scenario's claimed outcomes and DB state against Frappe using manifest handles."""
	if manifest is not None:
		m = manifest
	elif scenario.profile == "swarm_crunch_day":
		m = default_offline_crunch_manifest(target_date=target_date)
	else:
		m = default_offline_eval_manifest(target_date=target_date)
	resolved_date = target_date or str(resolve_manifest_path(m, "metadata.target_date"))

	baseline_ros = {str(v) for v in (m.get("ros") or {}).values() if v}
	elena_id = _safe_resolve(m, "techs.elena")
	devon_id = _safe_resolve(m, "techs.devon")
	main_lift = _safe_resolve(m, "bays.main_lift")
	alignment_bay = _safe_resolve(m, "bays.alignment")
	vin_liam = _safe_resolve(m, "vehicles.veh_model3_liam")
	vin_owen = _safe_resolve(m, "vehicles.veh_models_owen")
	vin_modelx = _safe_resolve(m, "vehicles.veh_modelx_household")
	vin_fleet1 = _safe_resolve(m, "vehicles.veh_fleet_1")
	cust_liam = _safe_resolve(m, "customers.retail_1")
	cust_sophia = _safe_resolve(m, "customers.retail_2")
	cust_owen = _safe_resolve(m, "customers.retail_3")
	cust_household = _safe_resolve(m, "customers.household_primary")
	cust_household_sec = _safe_resolve(m, "customers.household_secondary")
	cust_fleet = _safe_resolve(m, "customers.fleet_1")
	unsched_ro = _safe_resolve(m, "ros.unscheduled_ro")
	unsched_sec_ro = _safe_resolve(m, "ros.unscheduled_secondary_ro")
	open_po = _safe_resolve(m, "pos.eval_open_po")
	se_hv = _safe_resolve(m, "schedule_entries.hv_morning")
	se_conflict = _safe_resolve(m, "schedule_entries.conflict_main_lift")
	rotor_aft = _safe_resolve(m, "items.front_rotor_aft")
	rotor_oem = _safe_resolve(m, "items.front_rotor_oem")
	dot4_item = _safe_resolve(m, "items.dot4_brake_fluid")
	supplier_aft = _safe_resolve(m, "suppliers.euro_aftermarket")
	supplier_oem = _safe_resolve(m, "suppliers.tesla_oem")
	supplier_cons = _safe_resolve(m, "suppliers.shop_consumables")

	checks: list[tuple[str, bool, str]] = []
	claimed = _extract_claimed_ids(entry)
	answer = str(entry.get("final_answer") or "")
	answer_lower = answer.lower()
	cmds_text = "\n".join(str(c.get("cmd") or "") for c in (entry.get("commands_run") or []))
	sid = scenario.id

	# --- Part A: eval_standard (S01-S30) ---
	if sid == "S01_shop_status_relative":
		live = client.get_shop_floor_status(date=resolved_date)
		summary = live.get("data", {}).get("summary", {})
		bays = summary.get("active_bays")
		appts = summary.get("scheduled_appointments")
		checks.append(("date_in_answer_or_cmd", resolved_date in answer or resolved_date in cmds_text, f"expected {resolved_date}"))
		checks.append(("mentions_bays_count", str(bays) in answer, f"active_bays={bays}"))
		checks.append(("mentions_appts_count", str(appts) in answer, f"scheduled_appointments={appts}"))

	elif sid == "S02_tech_util_leave":
		overview = client.get_daily_tech_overview(date=resolved_date)
		techs = overview.get("data", {}).get("technicians", [])
		elena = next((t for t in techs if t.get("employee") == elena_id), None)
		elena_on_leave = bool(elena and elena.get("on_leave") is True)
		checks.append(("db_elena_on_leave", elena_on_leave, f"elena={elena}"))
		checks.append(("mentions_elena_leave", "elena" in answer_lower and "leave" in answer_lower, "expected Elena on leave"))
		checks.append(("mentions_other_tech", any(n in answer_lower for n in ("marcus", "devon", "noah")), "expected tech utilization details"))

	elif sid == "S03_slots_alignment_rack":
		slots_res = client.find_available_slots(
			start_date=resolved_date,
			duration_minutes=60,
			days_ahead=1,
			equipment_tags=["Alignment Rack"],
		)
		slots = slots_res.get("data", {}).get("slots", [])
		checks.append(("db_slots_exist", len(slots) > 0, f"slot_count={len(slots)}"))
		checks.append(("mentions_alignment_bay", "alignment" in answer_lower or "bay 2" in answer_lower or alignment_bay.lower() in answer_lower, "expected Alignment Rack bay"))
		checks.append(("mentions_time_slot", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)), "expected HH:MM slot"))

	elif sid == "S04_slots_afternoon_lift":
		slots_res = client.find_available_slots(
			start_date=resolved_date,
			duration_minutes=90,
			days_ahead=1,
			equipment_tags=["Lift"],
			min_start_time="13:00:00",
		)
		slots = slots_res.get("data", {}).get("slots", [])
		checks.append(("db_afternoon_slots_exist", len(slots) > 0, f"slot_count={len(slots)}"))
		checks.append(("used_min_time_1300", "13:00" in cmds_text or "--min-time" in cmds_text, f"cmds={cmds_text}"))
		checks.append(("mentions_afternoon_slot", bool(re.search(r"\b1[3-6]:\d{2}\b|\b[1-4]:\d{2}\s*pm\b", answer_lower)), "expected >=13:00 slot"))

	elif sid == "S05_unscheduled_queue":
		unsched = client.get_unscheduled_services(unsched_ro).get("data", {})
		svcs = unsched.get("services") or []
		first_title = str((svcs[0].get("service_title") or "") if svcs else "").lower()
		core_title = first_title.split("(")[0].strip()
		checks.append(("db_has_unscheduled", len(svcs) >= 1, f"count={len(svcs)}"))
		checks.append(("mentions_service_title", bool(core_title) and (first_title in answer_lower or core_title in answer_lower), f"expected '{first_title}'"))

	elif sid == "S06_vin_decode_history":
		vin_data = client.decode_vehicle_vin(vin_liam).get("data", {})
		try:
			intel = client.get_vehicle_intelligence(vin_liam).get("data", {})
		except IsmApiError:
			intel = {}
		expected_model = str(vin_data.get("model") or "Model 3").lower()
		expected_year = str(vin_data.get("model_year") or "2023")
		checks.append(("mentions_model", expected_model in answer_lower, f"db_model={vin_data.get('model')}"))
		checks.append(("mentions_year", expected_year in answer, f"db_year={vin_data.get('model_year')}"))
		has_history = bool(intel.get("service_history") or intel.get("repair_orders") or "history" in answer_lower or "ro-" in answer_lower)
		checks.append(("mentions_history_or_profile", has_history, "expected vehicle intelligence/history"))

	elif sid == "S07_plate_search_deferred":
		checks.append(("mentions_liam_or_vin", "liam" in answer_lower or vin_liam.lower() in answer_lower or "model 3" in answer_lower, "expected LEV-301 vehicle"))
		checks.append(("mentions_safety_hazard", "hazard" in answer_lower or "safety" in answer_lower, "expected safety_hazard highlight"))
		checks.append(("mentions_fore_link_deferred", "link" in answer_lower or "780" in answer, "expected deferred fore link item"))

	elif sid == "S08_batch_three_reads":
		live = client.get_shop_floor_status(date=resolved_date).get("data", {}).get("summary", {})
		unsched = client.get_unscheduled_services(unsched_ro).get("data", {}).get("services") or []
		checks.append(("mentions_bays", str(live.get("active_bays")) in answer, f"active_bays={live.get('active_bays')}"))
		checks.append(("mentions_elena_leave", "elena" in answer_lower, "expected Elena leave info"))
		first_svc = str((unsched[0].get("service_title") or "") if unsched else "").lower()
		core_svc = first_svc.split("(")[0].strip()
		checks.append(("mentions_unscheduled_ro", bool(core_svc) and (first_svc in answer_lower or core_svc in answer_lower), f"expected '{first_svc}'"))

	elif sid == "S09_alternates_and_templates":
		checks.append(("mentions_rotor_alternates", "zimmermann" in answer_lower or "oem" in answer_lower or "aftermarket" in answer_lower, "expected rotor alternates"))
		checks.append(("mentions_inspection_templates", "walkaround" in answer_lower or "undercarriage" in answer_lower, "expected inspection templates"))

	elif sid == "S10_pricing_baseline_small_ticket":
		checks.append(("has_140_baseline", "140.00" in answer or "$140" in answer, "expected $140.00 baseline_offset"))
		checks.append(("has_10_small_ticket", "10.00" in answer or "$10" in answer, "expected $10.00 small_ticket_match"))
		checks.append(("no_tesla_oem_leakage_in_quote", "tesla oem" not in answer_lower and "parts.tesla.com" not in answer_lower, "must keep Tesla reference internal"))

	elif sid == "S11_pricing_cost_floor_fallback":
		checks.append(("has_118_75_cost_floor", "118.75" in answer, "expected $118.75 cost_floor"))
		checks.append(("has_35_fallback", "35.00" in answer or "$35" in answer, "expected $35.00 cost_plus_fallback"))
		checks.append(("no_tesla_oem_leakage_in_quote", "parts.tesla.com" not in answer_lower, "must keep Tesla URL internal"))

	elif sid == "S12_composite_intake_slots":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == cust_liam, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0 and ro.get("status") == "Draft Estimate", f"status={ro.get('status')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_ro_has_brake_inspection", any("front brake inspection" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))
		checks.append(("mentions_slots", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)), "expected candidate slot times"))

	elif sid == "S13_quote_amend_approval":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == cust_liam, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0, f"docstatus={ro.get('docstatus')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_has_2_services", len(svcs) == 2, f"services={svcs}"))
				checks.append(("db_has_tire_rotation", any("tire rotation" in s for s in svcs), f"services={svcs}"))
				checks.append(("db_has_wiper", any("wiper" in s for s in svcs), f"services={svcs}"))
				checks.append(("db_has_approval_token", bool(ro.get("approval_token")), "approval_token missing"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))
		checks.append(("answer_has_approval_url", "token=" in answer, "expected approval URL with token="))

	elif sid == "S14_multi_service_quote_estimate":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				gt = float(ro.get("grand_total") or 0.0)
				est = client.estimate_duration(repair_order=ro_id).get("data", {})
				p80 = est.get("p80_minutes") or est.get("total_duration_minutes") or est.get("estimated_minutes")
				checks.append(("db_ro_customer", ro.get("customer") == cust_liam, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0, f"docstatus={ro.get('docstatus')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_has_2_services", len(svcs) == 2, f"services={svcs}"))
				checks.append(("db_grand_total_positive", gt > 0, f"grand_total={gt}"))
				gt_str = f"{gt:.2f}"
				checks.append(("answer_has_server_total", gt_str in answer or f"{gt:,.2f}" in answer, f"expected {gt_str}"))
				checks.append(("answer_has_p80", bool(p80) and str(int(round(float(p80)))) in answer, f"expected p80={p80}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S15_symmetrical_lh_rh_quote":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == cust_liam, f"customer={ro.get('customer')}"))
				checks.append(("db_has_lh_and_rh", len(svcs) >= 2 and any("lh" in s for s in svcs) and any("rh" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S16_household_deferred_attach":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == cust_household, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_vehicle", ro.get("vehicle") == vin_modelx, f"vehicle={ro.get('vehicle')}"))
				checks.append(("db_has_wiper_and_rear_brake", len(svcs) >= 2 and any("rear brake" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S17_quote_apply_inspection":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				insp = client.get_check_in_inspection(ro_id).get("data", {})
				items = insp.get("items") or insp.get("inspection_items") or []
				checks.append(("db_ro_customer", ro.get("customer") == cust_sophia, f"customer={ro.get('customer')}"))
				checks.append(("db_inspection_items_populated", len(items) >= 5, f"items_count={len(items)}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S18_quote_and_deposit":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				pes = client.list_documents(
					"Payment Entry",
					filters=[["party", "=", cust_owen], ["reference_no", "=", "DEP-401"], ["docstatus", "=", 1]],
					fields=["name", "paid_amount", "mode_of_payment", "docstatus"],
					limit=5,
				)
				pe = pes[0] if pes else None
				checks.append(("db_ro_customer", ro.get("customer") == cust_owen, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_vehicle", ro.get("vehicle") == vin_owen, f"vehicle={ro.get('vehicle')}"))
				checks.append(("db_deposit_pe_submitted", bool(pe and float(pe.get("paid_amount") or 0) == 100.0), f"pe={pe}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S19_direct_book_human_names":
		entries = client.list_documents(
			"Schedule Entry",
			filters=[
				["repair_order", "=", unsched_ro],
				["scheduled_date", "=", resolved_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time", "service_bay", "assigned_technician", "estimated_duration", "status"],
			limit=10,
		)
		match = next((e for e in entries if str(e.get("scheduled_time") or "").startswith("13:00")), None)
		checks.append(("db_entry_exists_1300", match is not None, f"entries={entries}"))
		if match:
			checks.append(("db_bay_main_lift", match.get("service_bay") == main_lift, f"bay={match.get('service_bay')}"))
			checks.append(("db_tech_devon", match.get("assigned_technician") == devon_id, f"tech={match.get('assigned_technician')}"))
			checks.append(("db_duration_60", int(match.get("estimated_duration") or 0) == 60, f"dur={match.get('estimated_duration')}"))
			checks.append(("claimed_se_matches", match["name"] in claimed["schedule_entries"], f"claimed={claimed['schedule_entries']}"))

	elif sid == "S20_conflict_and_recovery":
		entries_9am = client.list_documents(
			"Schedule Entry",
			filters=[
				["repair_order", "=", unsched_ro],
				["scheduled_date", "=", resolved_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time"],
			limit=10,
		)
		created_9am = [e for e in entries_9am if str(e.get("scheduled_time") or "").startswith(("09:00", "9:00"))]
		checks.append(("no_9am_entry_created", len(created_9am) == 0, f"unexpected={created_9am}"))
		checks.append(("surfaces_conflict", any(w in answer_lower for w in ("conflict", "occupied", "booked", "unavailable")), "expected conflict notice"))
		checks.append(("surfaces_alternatives", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)) or "alternative" in answer_lower, "expected alternative slots"))
		checks.append(("surfaces_deep_link", len(claimed["urls"]) > 0 or "/app/" in answer, f"urls={claimed['urls']}"))

	elif sid == "S21_ambiguous_bay_recovery":
		entries = client.list_documents(
			"Schedule Entry",
			filters=[
				["provisional_customer_name", "=", cust_owen],
				["scheduled_date", "=", resolved_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time", "service_bay", "estimated_duration"],
			limit=10,
		)
		match = next((e for e in entries if str(e.get("scheduled_time") or "").startswith("10:00")), None)
		checks.append(("db_walkin_entry_exists", match is not None, f"entries={entries}"))
		if match:
			checks.append(("db_duration_60", int(match.get("estimated_duration") or 0) == 60, f"dur={match.get('estimated_duration')}"))
			checks.append(("db_valid_bay", bool(match.get("service_bay")), f"bay={match.get('service_bay')}"))
			checks.append(("claimed_se_matches", match["name"] in claimed["schedule_entries"], f"claimed={claimed['schedule_entries']}"))

	elif sid == "S22_leave_and_lunch_trap":
		entries_noon = client.list_documents(
			"Schedule Entry",
			filters=[
				["repair_order", "=", unsched_sec_ro],
				["scheduled_date", "=", resolved_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time"],
			limit=10,
		)
		noon = [e for e in entries_noon if str(e.get("scheduled_time") or "").startswith("12:00")]
		checks.append(("no_noon_entry_created", len(noon) == 0, f"unexpected={noon}"))
		checks.append(("surfaces_leave_or_break", any(w in answer_lower for w in ("leave", "break", "lunch", "unavailable", "conflict")), "expected leave/break rejection"))

	elif sid == "S23_reschedule_and_confirm_batch":
		se_hv_doc = client.get_document("Schedule Entry", se_hv)
		se_conf_doc = client.get_document("Schedule Entry", se_conflict)
		checks.append(("db_hv_moved_1100", str(se_hv_doc.get("scheduled_time") or "").startswith("11:00"), f"time={se_hv_doc.get('scheduled_time')}"))
		checks.append(("db_conflict_confirmed", se_conf_doc.get("status") == "Confirmed", f"status={se_conf_doc.get('status')}"))

	elif sid == "S24_walkin_convert_missing_odometer":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				checks.append(("db_ro_customer", ro.get("customer") == cust_sophia, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_odometer_61200", int(ro.get("odometer_in") or 0) == 61200, f"odometer_in={ro.get('odometer_in')}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S25_missing_field_write_gate":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == cust_liam, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0 and ro.get("status") == "Draft Estimate", f"status={ro.get('status')}"))
				checks.append(("db_ro_odometer_54100", int(ro.get("odometer_in") or 0) == 54100, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_ro_has_cabin_filter", any("cabin" in s and "filter" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif sid == "S26_unapproved_start_lock":
		new_ros = [r for r in claimed["repair_orders"] if r not in baseline_ros]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				checks.append(("db_ro_customer", ro.get("customer") == cust_household_sec, f"customer={ro.get('customer')}"))
				checks.append(("db_ro_still_draft", ro.get("status") == "Draft Estimate" and ro.get("approval_status") == "Estimating", f"status={ro.get('status')}/{ro.get('approval_status')}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))
		checks.append(("refuses_unapproved_start", any(w in answer_lower for w in ("approv", "lock", "cannot start", "draft estimate", "estimating")), "expected authorization lock refusal"))

	elif sid == "S27_standalone_procure_advance_pay":
		po_id = claimed["purchase_orders"][-1] if claimed["purchase_orders"] else None
		if not po_id:
			pos = client.list_documents(
				"Purchase Order",
				filters=[["supplier", "=", supplier_aft], ["docstatus", "=", 1]],
				fields=["name"],
				order_by="creation desc",
				limit=1,
			)
			po_id = pos[0]["name"] if pos else None
		checks.append(("db_po_found", po_id is not None, f"claimed_pos={claimed['purchase_orders']}"))
		if po_id:
			try:
				po = client.get_document("Purchase Order", po_id)
				items = po.get("items") or []
				gt = float(po.get("grand_total") or 0.0)
				adv = float(po.get("advance_paid") or 0.0)
				checks.append(("db_po_submitted", int(po.get("docstatus") or 0) == 1, f"docstatus={po.get('docstatus')}"))
				checks.append(("db_po_standalone_no_ro", not po.get("repair_order"), f"repair_order={po.get('repair_order')}"))
				checks.append(("db_po_supplier", po.get("supplier") == supplier_aft, f"supplier={po.get('supplier')}"))
				checks.append((
					"db_po_item_qty_rate",
					any(it.get("item_code") == rotor_aft and float(it.get("qty") or 0) == 2.0 and float(it.get("rate") or 0) == 88.0 for it in items),
					f"items={items}",
				))
				checks.append(("db_po_advance_paid_full", gt > 0 and abs(adv - gt) < 0.05, f"grand_total={gt}, advance_paid={adv}"))
				checks.append(("claimed_po_matches", po_id in claimed["purchase_orders"], f"claimed={claimed['purchase_orders']}"))
			except IsmApiError as exc:
				checks.append(("db_po_exists", False, str(exc)))

		pes = client.list_documents(
			"Payment Entry",
			filters=[["party_type", "=", "Supplier"], ["party", "=", supplier_aft], ["reference_no", "=", "BD-9001"], ["docstatus", "=", 1]],
			fields=["name", "mode_of_payment", "paid_amount", "docstatus", "reference_no"],
			order_by="creation desc",
			limit=1,
		)
		pe = pes[0] if pes else None
		checks.append(("db_pe_submitted", bool(pe and int(pe.get("docstatus") or 0) == 1), f"pe={pe}"))
		if pe:
			checks.append(("db_pe_bank_draft", pe.get("mode_of_payment") == "Bank Draft", f"mop={pe.get('mode_of_payment')}"))
			checks.append(("claimed_pe_matches", pe["name"] in claimed["payment_entries"], f"claimed={claimed['payment_entries']}"))

	elif sid == "S28_purchase_invoice_with_stock":
		pi_id = claimed["purchase_invoices"][-1] if claimed["purchase_invoices"] else None
		if not pi_id:
			pis = client.list_documents(
				"Purchase Invoice",
				filters=[["bill_no", "=", "INV-9001"], ["docstatus", "=", 1]],
				fields=["name"],
				order_by="creation desc",
				limit=1,
			)
			pi_id = pis[0]["name"] if pis else None
		checks.append(("db_pi_found", pi_id is not None, f"claimed_pis={claimed['purchase_invoices']}"))
		if pi_id:
			try:
				pi = client.get_document("Purchase Invoice", pi_id)
				checks.append(("db_pi_submitted", int(pi.get("docstatus") or 0) == 1, f"docstatus={pi.get('docstatus')}"))
				checks.append(("db_pi_update_stock", int(pi.get("update_stock") or 0) == 1, f"update_stock={pi.get('update_stock')}"))
				checks.append(("db_pi_bill_no", pi.get("bill_no") == "INV-9001", f"bill_no={pi.get('bill_no')}"))
				checks.append(("db_pi_advance_attached", float(pi.get("total_advance") or 0.0) > 0, f"total_advance={pi.get('total_advance')}"))
				checks.append(("db_pi_outstanding_zero", abs(float(pi.get("outstanding_amount") or 0.0)) < 0.05, f"outstanding={pi.get('outstanding_amount')}"))
				checks.append(("claimed_pi_matches", pi_id in claimed["purchase_invoices"], f"claimed={claimed['purchase_invoices']}"))
				po_links = [it.get("purchase_order") for it in (pi.get("items") or []) if it.get("purchase_order")]
				if po_links:
					po = client.get_document("Purchase Order", po_links[0])
					checks.append(("db_po_matches_manifest", not open_po or po_links[0] == open_po, f"po={po_links[0]}, expected={open_po}"))
					checks.append(("db_po_per_received_100", float(po.get("per_received") or 0.0) >= 99.9, f"per_received={po.get('per_received')}"))
				else:
					checks.append(("db_po_linked_on_pi", False, "no purchase_order on PI items"))
			except IsmApiError as exc:
				checks.append(("db_pi_exists", False, str(exc)))

	elif sid == "S29_wrong_supplier_po_stop":
		po_open_doc = client.get_document("Purchase Order", open_po) if open_po else {}
		checks.append(("tesla_po_untouched", not open_po or float(po_open_doc.get("per_received") or 0.0) < 1.0, f"per_received={po_open_doc.get('per_received')}"))
		checks.append(("reports_no_open_po", any(w in answer_lower for w in ("no open", "none", "not found", "0 open", "no submitted")), "expected stop notice"))
		checks.append(("no_supplier_substitution", open_po not in answer, f"must not mention {open_po}"))

	elif sid == "S30_two_step_electronic_invoice_receive":
		pis = client.list_documents(
			"Purchase Invoice",
			filters=[["supplier", "=", supplier_cons], ["bill_no", "=", "INV-9020"], ["docstatus", "=", 1]],
			fields=["name", "update_stock"],
			order_by="creation desc",
			limit=1,
		)
		pi = pis[0] if pis else None
		checks.append(("db_pi_submitted", pi is not None, f"pis={pis}"))
		if pi:
			checks.append(("db_pi_update_stock_0", int(pi.get("update_stock") or 0) == 0, f"update_stock={pi.get('update_stock')}"))
			pi_full = client.get_document("Purchase Invoice", pi["name"])
			po_links = [it.get("purchase_order") for it in (pi_full.get("items") or []) if it.get("purchase_order")]
			if po_links:
				po = client.get_document("Purchase Order", po_links[0])
				items = po.get("items") or []
				checks.append(("db_po_item_dot4", any(it.get("item_code") == dot4_item and float(it.get("qty") or 0) == 4.0 for it in items), f"items={items}"))
				checks.append(("db_po_received_100", float(po.get("per_received") or 0.0) >= 99.9, f"per_received={po.get('per_received')}"))
			else:
				checks.append(("db_po_linked", False, "no purchase_order on PI"))

	# --- Part B: swarm_crunch_day (C01-C10) ---
	elif sid == "C01_crunch_executive_triage":
		checks.append(("mentions_high_bay_load", "85" in answer or "435" in answer or "busy" in answer_lower, "expected ~85% bay utilization"))
		checks.append(("mentions_open_action_items", "caliper" in answer_lower or "camber" in answer_lower or "tolerance" in answer_lower, "expected open Shop Action Items"))

	elif sid == "C02_fleet_wip_and_deferred":
		checks.append(("mentions_fleet_ros", len(claimed["repair_orders"]) >= 3 or "metro green" in answer_lower, f"claimed_ros={claimed['repair_orders']}"))
		checks.append(("mentions_deferred_brake_flush", "brake fluid" in answer_lower or "deferred" in answer_lower, f"expected {vin_fleet1} deferred work"))

	elif sid == "C03_packed_floor_afternoon_slot":
		checks.append(("mentions_late_afternoon_opening", any(t in answer for t in ("15:45", "16:00", "3:45", "4:00")), "expected late-afternoon post-15:45 lift opening"))

	elif sid == "C04_fifo_stock_contention_procure":
		fifo_loser = _safe_resolve(m, "ros.crunch_fifo_loser")
		pos = client.list_documents(
			"Purchase Order",
			filters=[["repair_order", "=", fifo_loser], ["docstatus", "=", 1]],
			fields=["name", "supplier"],
			order_by="creation desc",
			limit=1,
		)
		checks.append(("mentions_rotor_deficit", "rotor" in answer_lower or "deficit" in answer_lower or "short" in answer_lower, "expected rotor deficit explanation"))
		checks.append(("db_ro_linked_po_submitted", bool(pos), f"fifo_loser={fifo_loser}, pos={pos}"))
		if pos:
			po_doc = client.get_document("Purchase Order", pos[0]["name"])
			items = po_doc.get("items") or []
			checks.append(("db_po_supplier_oem", po_doc.get("supplier") == supplier_oem, f"supplier={po_doc.get('supplier')}"))
			checks.append(("db_po_has_rotor_oem", any(it.get("item_code") == rotor_oem for it in items), f"items={items}"))

	elif sid == "C05_floor_supplement_over_tolerance":
		hv1_ro = _safe_resolve(m, "ros.crunch_hv_1")
		ro = client.get_document("Repair Order", hv1_ro)
		svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
		checks.append(("db_supplement_added", any("ground strap" in s for s in svcs), f"services={svcs}"))

	elif sid == "C06_supplement_approval_resolve_action":
		fleet1_ro = _safe_resolve(m, "ros.crunch_fleet_1")
		ai_1 = _safe_resolve(m, "action_items.crunch_over_tol_1")
		ro = client.get_document("Repair Order", fleet1_ro)
		ai = client.get_document("Shop Action Item", ai_1)
		checks.append(("db_approval_token_set", bool(ro.get("approval_token")), "approval_token missing"))
		checks.append(("answer_has_approval_url", "token=" in answer, "expected approval URL"))
		checks.append(("db_action_item_resolved", ai.get("status") == "Resolved", f"status={ai.get('status')}"))

	elif sid == "C07_parts_hold_check_and_action":
		hv2_ro = _safe_resolve(m, "ros.crunch_hv_2")
		ro = client.get_document("Repair Order", hv2_ro)
		ais = client.list_documents(
			"Shop Action Item",
			filters=[["reference_name", "=", hv2_ro], ["status", "=", "Open"]],
			fields=["name", "title"],
			limit=5,
		)
		checks.append(("db_ro_waiting_on_parts", ro.get("status") == "Waiting on Parts", f"status={ro.get('status')}"))
		checks.append(("db_action_item_created", any("connector" in str(a.get("title") or "").lower() for a in ais), f"ais={ais}"))

	elif sid == "C08_floor_complete_missing_odometer_gate":
		tire2_ro = _safe_resolve(m, "ros.crunch_tire_2")
		ro = client.get_document("Repair Order", tire2_ro)
		checks.append(("db_ro_work_completed", ro.get("status") == "Work Completed", f"status={ro.get('status')}"))
		checks.append(("db_ro_odometer_out_48335", int(ro.get("odometer_out") or 0) == 48335, f"odometer_out={ro.get('odometer_out')}"))

	elif sid == "C09_checkout_missing_payment_release":
		tire3_ro = _safe_resolve(m, "ros.crunch_tire_3")
		ro = client.get_document("Repair Order", tire3_ro)
		checks.append(("db_ro_closed_released", ro.get("status") == "Closed / Released", f"status={ro.get('status')}"))
		checks.append(("db_ro_odometer_out_34235", int(ro.get("odometer_out") or 0) == 34235, f"odometer_out={ro.get('odometer_out')}"))
		checks.append(("db_sales_invoice_linked", bool(ro.get("sales_invoice")) or len(claimed["sales_invoices"]) > 0, f"si={ro.get('sales_invoice')}"))

	elif sid == "C10_unapproved_supplement_checkout_block":
		align_supp_ro = _safe_resolve(m, "ros.crunch_align_supp")
		ro = client.get_document("Repair Order", align_supp_ro)
		checks.append(("db_ro_not_released", ro.get("status") != "Closed / Released" and int(ro.get("docstatus") or 0) == 0, f"status={ro.get('status')}"))
		checks.append(("surfaces_unapproved_supplement", any(w in answer_lower for w in ("supplement", "unapproved", "approval", "estimating", "tolerance", "hold")), "expected supplement block explanation"))

	passed_count = sum(1 for _, ok, _ in checks if ok)
	failures = [f"{label}: {detail}" for label, ok, detail in checks if not ok]
	return {
		"db_verified": passed_count == len(checks) and len(checks) > 0,
		"checks_passed": passed_count,
		"checks_total": len(checks),
		"claimed_ids": claimed,
		"failures": failures,
	}


_CLI_INVOCATION_PATTERN = re.compile(
	r"(?:^|[;&|\s])(?:"
	r"(?:[^\s;&|]*[\\/])?(?:py|python(?:\d+(?:\.\d+)?)?(?:\.exe)?)\s+(?:-m\s+tools\.cli|(?:[^\s;&|]*[\\/])?tools[\\/]cli\.py)"
	r"|(?:[^\s;&|]*[\\/])?ism-agent-os(?:\.exe)?)"
	r"(?=\s|$)",
	re.IGNORECASE,
)


def _split_shell_segments(command_line: str) -> list[str]:
	"""Split a shell command string on separators outside single/double quotes."""
	segments: list[str] = []
	buf: list[str] = []
	in_single = False
	in_double = False
	i = 0
	n = len(command_line)
	while i < n:
		ch = command_line[i]
		if ch == "'" and not in_double:
			in_single = not in_single
			buf.append(ch)
			i += 1
			continue
		if ch == '"' and not in_single:
			in_double = not in_double
			buf.append(ch)
			i += 1
			continue
		if ch == "\\" and in_double and i + 1 < n:
			buf.append(command_line[i : i + 2])
			i += 2
			continue
		if not in_single and not in_double:
			two = command_line[i : i + 2]
			if two in ("&&", "||"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 2
				continue
			if ch in (";", "|", "\n", "\r"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 1
				continue
		buf.append(ch)
		i += 1
	tail = "".join(buf).strip()
	if tail:
		segments.append(tail)
	return [s for s in segments if s]


def _tokenize_segment(segment: str) -> list[str]:
	try:
		return shlex.split(segment, posix=True)
	except ValueError:
		return segment.split()


def _extract_cli_argv_from_tokens(tokens: list[str]) -> Optional[list[str]]:
	"""Return CLI argv tokens if a command segment invokes the ISM Agent OS CLI."""
	if not tokens:
		return None
	idx = 0
	while idx < len(tokens):
		tok = tokens[idx]
		low = tok.lower()
		if low in ("rtk", "bypass", "&"):
			idx += 1
			continue
		break
	if idx >= len(tokens):
		return None
	head = tokens[idx].lower()
	head_name = Path(head).name
	if head_name in ("ism-agent-os", "ism-agent-os.exe"):
		return tokens[idx + 1 :]
	if re.match(r"^(?:py|python(?:\d+(?:\.\d+)?)?)(?:\.exe)?$", head_name) and idx + 1 < len(tokens):
		nxt = tokens[idx + 1]
		if nxt == "-m" and idx + 2 < len(tokens) and tokens[idx + 2] == "tools.cli":
			return tokens[idx + 3 :]
		if nxt.replace("\\", "/").endswith("tools/cli.py"):
			return tokens[idx + 2 :]
	return None


def _extract_subcommand(cmd_str: str) -> Optional[str]:
	"""Extract the ISM CLI subcommand name from a command string."""
	for seg in _split_shell_segments(cmd_str):
		argv = _extract_cli_argv_from_tokens(_tokenize_segment(seg))
		if argv is None:
			continue
		i = 0
		while i < len(argv) and argv[i].startswith("-"):
			if argv[i] == "--env-file":
				i += 2
			else:
				i += 1
		if i < len(argv):
			return argv[i]
	return None


def _is_cli_command(cmd_str: str) -> bool:
	"""Return True if cmd_str invokes the ISM Agent OS CLI."""
	for seg in _split_shell_segments(cmd_str):
		if _extract_cli_argv_from_tokens(_tokenize_segment(seg)) is not None:
			return True
	return bool(_CLI_INVOCATION_PATTERN.search(cmd_str))


def _unwrap_batch_argv(
	argv: list[str],
	cli_parser: argparse.ArgumentParser,
	parsed_args: Optional[argparse.Namespace],
	*,
	relaxed_parser: Optional[argparse.ArgumentParser] = None,
	is_stage: bool = False,
) -> list[dict[str, Any]]:
	"""Unwrap batch --cmd / --file items using _batch_item_to_argv into inner command records."""
	raw_items: list[Any] = []
	if parsed_args is not None:
		try:
			raw_items = _load_batch_items(parsed_args)
		except (OSError, ValueError, json.JSONDecodeError):
			raw_items = list(getattr(parsed_args, "cmds", None) or [])
	else:
		i = 0
		while i < len(argv):
			if argv[i] in ("--cmd", "-c") and i + 1 < len(argv):
				raw_items.append(argv[i + 1])
				i += 2
			else:
				i += 1

	active_parser = relaxed_parser if (is_stage and relaxed_parser is not None) else cli_parser
	inner: list[dict[str, Any]] = []
	for raw_item in raw_items:
		try:
			sub_argv = _batch_item_to_argv(raw_item)
		except ValueError:
			continue
		sub_cmd: Optional[str] = None
		sub_dry = ("--dry-run" in sub_argv) or ("--preview" in sub_argv)
		sub_stage = ("--stage" in sub_argv) or is_stage
		err_buf = io.StringIO()
		try:
			with contextlib.redirect_stderr(err_buf):
				sub_ns = active_parser.parse_args(sub_argv)
			sub_cmd = getattr(sub_ns, "command", None)
			sub_dry = bool(getattr(sub_ns, "dry_run", sub_dry))
			sub_stage = bool(getattr(sub_ns, "stage", sub_stage))
		except SystemExit:
			for tok in sub_argv:
				if not tok.startswith("-"):
					sub_cmd = tok
					break
		inner.append({"subcommand": sub_cmd, "argv": sub_argv, "dry_run": sub_dry, "stage": sub_stage})
	return inner


def _parse_cli_invocations(
	cmd_str: str,
	cli_parser: argparse.ArgumentParser,
	*,
	relaxed_parser: Optional[argparse.ArgumentParser] = None,
) -> list[dict[str, Any]]:
	"""Parse one shell command string into structured CLI invocation descriptors."""
	invocations: list[dict[str, Any]] = []
	rel_parser = relaxed_parser or _build_cli_parser(relax_required=True)
	for seg in _split_shell_segments(cmd_str):
		tokens = _tokenize_segment(seg)
		argv = _extract_cli_argv_from_tokens(tokens)
		if argv is None:
			continue
		is_help = ("--help" in argv) or ("-h" in argv)
		outer_cmd: Optional[str] = None
		is_dry_run = ("--dry-run" in argv) or ("--preview" in argv)
		is_stage = "--stage" in argv
		parsed_args: Optional[argparse.Namespace] = None

		active_parser = rel_parser if is_stage else cli_parser
		if not is_help:
			err_buf = io.StringIO()
			try:
				with contextlib.redirect_stderr(err_buf):
					parsed_args = active_parser.parse_args(argv)
				outer_cmd = getattr(parsed_args, "command", None)
				is_dry_run = bool(getattr(parsed_args, "dry_run", is_dry_run))
				is_stage = bool(getattr(parsed_args, "stage", is_stage))
			except SystemExit:
				j = 0
				while j < len(argv) and argv[j].startswith("-"):
					j += 2 if argv[j] == "--env-file" else 1
				if j < len(argv):
					outer_cmd = argv[j]
		else:
			j = 0
			while j < len(argv) and argv[j].startswith("-"):
				j += 2 if argv[j] == "--env-file" else 1
			if j < len(argv):
				outer_cmd = argv[j]

		inner_calls: list[dict[str, Any]] = []
		if outer_cmd == "batch" and not is_help:
			inner_calls = _unwrap_batch_argv(
				argv,
				cli_parser,
				parsed_args,
				relaxed_parser=rel_parser,
				is_stage=is_stage,
			)
			if inner_calls and all(ic.get("dry_run") for ic in inner_calls):
				is_dry_run = True
		elif outer_cmd and not is_help:
			inner_calls = [{"subcommand": outer_cmd, "argv": argv, "dry_run": is_dry_run, "stage": is_stage}]

		invocations.append({
			"outer_command": outer_cmd,
			"is_help": is_help,
			"is_dry_run": is_dry_run,
			"is_stage": is_stage,
			"inner_calls": inner_calls,
			"argv": argv,
		})
	return invocations


def _analyze_commands(scenario: NLScenario, entry: dict[str, Any]) -> dict[str, Any]:
	"""Analyze CLI commands with batch unwrapping, --stage awareness, and dry-run separation."""
	commands_run = entry.get("commands_run") or []
	cli_parser = _build_cli_parser(relax_required=False)
	relaxed_parser = _build_cli_parser(relax_required=True)

	live_outer_subcommands: list[str] = []
	live_inner_subcommands: list[str] = []
	dry_run_subcommands: list[str] = []
	staged_subcommands: list[str] = []
	staged_inner_subcommands: list[str] = []
	cli_calls_count = 0
	dry_run_calls = 0
	staged_calls = 0
	live_cli_calls = 0
	redundant_calls = 0
	duplicate_dry_runs = 0
	duplicate_stage_runs = 0
	readonly_dry_runs = 0
	readonly_stage_runs = 0
	help_calls = 0
	unexpected_errors = 0
	self_corrections = 0
	seen_cmds: set[str] = set()

	for item in commands_run:
		cmd_str = str(item.get("cmd") or "").strip()
		if not _is_cli_command(cmd_str):
			continue
		parsed_list = _parse_cli_invocations(cmd_str, cli_parser, relaxed_parser=relaxed_parser)
		if not parsed_list:
			continue

		rc = int(item.get("exit_code") or 0)
		summary = str(item.get("summary") or item.get("stdout_summary") or "").lower()
		is_duplicate_cmd = cmd_str in seen_cmds
		if is_duplicate_cmd:
			redundant_calls += 1
		seen_cmds.add(cmd_str)

		for inv in parsed_list:
			cli_calls_count += 1
			outer = inv["outer_command"]
			is_help = inv["is_help"]
			is_dry = inv["is_dry_run"]
			is_stage = inv["is_stage"]
			inner_calls = inv["inner_calls"]

			if is_help:
				help_calls += 1
				continue

			if is_dry:
				dry_run_calls += 1
				if outer:
					dry_run_subcommands.append(outer)
				if is_duplicate_cmd:
					duplicate_dry_runs += 1
				elif any(ic.get("subcommand") in READ_ONLY_COMMANDS for ic in inner_calls):
					readonly_dry_runs += 1
					redundant_calls += 1
				continue

			if is_stage:
				staged_calls += 1
				if outer:
					staged_subcommands.append(outer)
				for ic in inner_calls:
					sub = ic.get("subcommand")
					if sub:
						staged_inner_subcommands.append(sub)
				if is_duplicate_cmd:
					duplicate_stage_runs += 1
				elif inner_calls and all(ic.get("subcommand") in READ_ONLY_COMMANDS for ic in inner_calls):
					readonly_stage_runs += 1
					redundant_calls += 1
				if rc != 0:
					unexpected_errors += 1
					self_corrections += 1
				continue

			live_cli_calls += 1
			if outer:
				live_outer_subcommands.append(outer)
			for ic in inner_calls:
				sub = ic.get("subcommand")
				if sub:
					live_inner_subcommands.append(sub)
				ic_str = " ".join(ic.get("argv") or [])
				if scenario.id == "S13_quote_amend_approval" and sub == "amend-quote" and "--remove-indices" in ic_str:
					self_corrections += 1
				elif scenario.id in ("S14_multi_service_quote_estimate", "S25_missing_field_write_gate") and sub == "amend-quote":
					self_corrections += 1

			if rc != 0:
				is_expected_feedback = scenario.allows_server_feedback_retry and any(
					tok in summary for tok in ("conflict", "occupied", "ambiguous", "leave", "break", "supplement", "unapproved", "blocked")
				)
				if not is_expected_feedback:
					unexpected_errors += 1
					self_corrections += 1

	excess_dry_runs = max(0, dry_run_calls - duplicate_dry_runs - readonly_dry_runs - scenario.allowed_dry_runs)
	redundant_calls += excess_dry_runs

	if staged_calls > 0:
		excess_stage_runs = max(0, staged_calls - duplicate_stage_runs - readonly_stage_runs - max(1, scenario.allowed_staged_calls))
		redundant_calls += excess_stage_runs

	effective_inner = live_inner_subcommands + staged_inner_subcommands
	effective_outer = live_outer_subcommands + staged_subcommands
	domain_cli_calls = live_cli_calls + staged_calls
	sid = scenario.id

	# Scenario-specific anti-redundancy checks across read, staged, and live paths
	if sid in ("S19_direct_book_human_names", "S20_conflict_and_recovery", "S22_leave_and_lunch_trap"):
		pre_checks = [
			s for s in effective_inner
			if s in ("status", "slots", "estimate", "get-doc", "list-docs", "tech-overview", "unscheduled", "search-vehicles")
		]
		redundant_calls += len(pre_checks)
		remaining = len(effective_inner) - len(pre_checks)
		if remaining > 1:
			redundant_calls += remaining - 1
	elif sid == "S12_composite_intake_slots":
		if "intake-slots" in effective_inner:
			if len(effective_inner) > 1:
				redundant_calls += len(effective_inner) - 1
		elif "quote" in effective_inner and "slots" in effective_inner:
			redundant_calls += 1
	elif sid == "S13_quote_amend_approval":
		redundant_calls += sum(
			1 for s in effective_inner
			if s in ("status", "estimate", "get-doc", "list-docs", "slots", "search-vehicles", "vehicle-info", "decode-vin")
		)
		if (
			"amend-quote" in effective_inner
			and "send-approval" in effective_inner
			and "batch" not in effective_outer
		):
			redundant_calls += 1
	elif sid == "S21_ambiguous_bay_recovery":
		redundant_calls += sum(
			1 for s in effective_inner
			if s in ("status", "estimate", "get-doc", "list-docs", "tech-overview", "unscheduled")
		)
		first_book_idx = effective_inner.index("book") if "book" in effective_inner else len(effective_inner)
		redundant_calls += sum(1 for s in effective_inner[:first_book_idx] if s == "slots")
	elif sid == "S14_multi_service_quote_estimate":
		redundant_calls += sum(
			1 for s in effective_inner
			if s in ("amend-quote", "status", "get-doc", "list-docs", "slots", "tech-overview", "search-vehicles", "vehicle-info")
		)
		non_receipt = [s for s in effective_inner if s != "stage-receipt"]
		if len(non_receipt) > 2:
			redundant_calls += len(non_receipt) - 2
	elif sid in ("S25_missing_field_write_gate", "S27_standalone_procure_advance_pay", "C05_floor_supplement_over_tolerance"):
		if len(effective_inner) > 1:
			redundant_calls += len(effective_inner) - 1
	elif sid == "S26_unapproved_start_lock":
		if "job-status" in effective_inner:
			redundant_calls += 1
			unexpected_errors += 1
		if len(effective_inner) > 1:
			redundant_calls += len(effective_inner) - 1
	elif sid == "S28_purchase_invoice_with_stock":
		unwanted = sum(1 for s in effective_inner if s not in ("list-docs", "purchase-invoice"))
		list_docs_count = sum(1 for s in effective_inner if s == "list-docs")
		pi_count = sum(1 for s in effective_inner if s == "purchase-invoice")
		redundant_calls += unwanted + max(0, list_docs_count - 1) + max(0, pi_count - 1)
	elif sid == "S29_wrong_supplier_po_stop":
		if any(s in effective_inner for s in ("receive-po", "purchase-invoice", "pay-purchase")):
			redundant_calls += 1
			unexpected_errors += 1
		if len(effective_inner) > 1:
			redundant_calls += len(effective_inner) - 1
	elif sid == "S06_vin_decode_history":
		unwanted = sum(1 for s in effective_inner if s in ("search-vehicles", "status", "slots", "tech-overview", "list-docs", "get-doc"))
		redundant_calls += unwanted
		if "batch" not in effective_outer and len(effective_outer) > 1:
			redundant_calls += len(effective_outer) - 1
		else:
			clean_inner = len(effective_inner) - unwanted
			if clean_inner > 2:
				redundant_calls += clean_inner - 2
	elif sid == "S08_batch_three_reads":
		unwanted = sum(1 for s in effective_inner if s not in ("status", "tech-overview", "unscheduled"))
		redundant_calls += unwanted
		if "batch" not in effective_outer and len(effective_outer) > 1:
			redundant_calls += len(effective_outer) - 1
		else:
			clean_inner = len(effective_inner) - unwanted
			if clean_inner > 3:
				redundant_calls += clean_inner - 3
	elif sid in ("S09_alternates_and_templates", "S10_pricing_baseline_small_ticket", "S11_pricing_cost_floor_fallback"):
		if "batch" not in effective_outer and len(effective_outer) > 1:
			redundant_calls += len(effective_outer) - 1
		elif len(effective_inner) > 2:
			redundant_calls += len(effective_inner) - 2
	elif sid in ("S01_shop_status_relative", "S02_tech_util_leave", "S03_slots_alignment_rack", "S04_slots_afternoon_lift", "S05_unscheduled_queue", "C03_packed_floor_afternoon_slot"):
		if len(effective_inner) > 1:
			redundant_calls += len(effective_inner) - 1
	elif sid in ("C01_crunch_executive_triage", "C02_fleet_wip_and_deferred", "C06_supplement_approval_resolve_action"):
		if "batch" not in effective_outer and len(effective_outer) > 1:
			redundant_calls += len(effective_outer) - 1
	elif sid in ("C08_floor_complete_missing_odometer_gate", "C09_checkout_missing_payment_release"):
		get_doc_count = sum(1 for s in effective_inner if s == "get-doc")
		redundant_calls += max(0, get_doc_count - 1)
	elif sid == "C10_unapproved_supplement_checkout_block":
		if "checkout" in effective_inner and "batch" not in effective_outer and len(effective_outer) > 2:
			redundant_calls += len(effective_outer) - 2

	has_redundancy = (help_calls > 0) or (redundant_calls > 0)
	return {
		"subcommands": effective_outer,
		"inner_subcommands": effective_inner,
		"dry_run_subcommands": dry_run_subcommands,
		"staged_subcommands": staged_subcommands,
		"total_cli_calls": cli_calls_count,
		"dry_run_calls": dry_run_calls,
		"staged_calls": staged_calls,
		"live_cli_calls": live_cli_calls,
		"domain_cli_calls": domain_cli_calls,
		"help_calls": help_calls,
		"self_corrections": self_corrections,
		"redundant_calls": redundant_calls,
		"has_redundancy": has_redundancy,
		"unexpected_errors": unexpected_errors,
	}


def score_round(
	client: Optional[IsmAgentClient],
	round_num: int,
	raw_entries: list[dict[str, Any]],
	target_date: Optional[str] = None,
	skip_db: bool = False,
	manifest: Optional[dict[str, Any]] = None,
	*,
	profile: Optional[str] = None,
	tier: str = "full",
) -> dict[str, Any]:
	"""Verify DB state and compute Round <N> KPIs across all scenario entries."""
	if manifest is not None:
		resolved_manifest = manifest
	elif profile == "swarm_crunch_day":
		resolved_manifest = default_offline_crunch_manifest(target_date=target_date)
	else:
		resolved_manifest = default_offline_eval_manifest(target_date=target_date)

	resolved_date = target_date or str(resolve_manifest_path(resolved_manifest, "metadata.target_date"))
	all_scenarios = get_scenarios(manifest=resolved_manifest, profile=profile or "all", tier=tier)
	scenarios_by_id = {s.id: s for s in all_scenarios}
	scored_results: list[dict[str, Any]] = []

	for entry in raw_entries:
		sid = entry.get("id")
		scenario = scenarios_by_id.get(sid)
		if not scenario:
			continue

		cmd_metrics = _analyze_commands(scenario, entry)
		if skip_db or client is None:
			existing_db = entry.get("db_verification")
			if isinstance(existing_db, dict) and "db_verified" in existing_db:
				db_res = existing_db
			else:
				db_res = {"db_verified": True, "checks_passed": 0, "checks_total": 0, "claimed_ids": _extract_claimed_ids(entry), "failures": []}
		else:
			db_res = verify_scenario_db(client, scenario, entry, resolved_date, manifest=resolved_manifest)

		first_try = (
			db_res["db_verified"]
			and cmd_metrics["unexpected_errors"] == 0
			and cmd_metrics["self_corrections"] == 0
			and cmd_metrics["redundant_calls"] == 0
			and cmd_metrics["help_calls"] == 0
		)

		wall_ms = float(entry.get("wall_clock_ms") or 0.0)
		if wall_ms <= 0:
			wall_ms = sum(float(c.get("ms") or 0.0) for c in (entry.get("commands_run") or []))

		conflict_recovered: Optional[bool] = None
		if scenario.is_conflict_scenario:
			conflict_recovered = db_res["db_verified"] and cmd_metrics["unexpected_errors"] == 0

		scored_results.append({
			"id": scenario.id,
			"profile": scenario.profile,
			"persona": scenario.persona,
			"wave": scenario.wave,
			"category": scenario.category,
			"nl_prompt": scenario.nl_prompt,
			"expected_commands": scenario.expected_commands,
			"optimal_cli_calls": scenario.optimal_cli_calls,
			"allowed_dry_runs": scenario.allowed_dry_runs,
			"allowed_staged_calls": scenario.allowed_staged_calls,
			"commands_run": entry.get("commands_run") or [],
			"subcommands_executed": cmd_metrics["subcommands"],
			"inner_subcommands_executed": cmd_metrics["inner_subcommands"],
			"dry_run_subcommands": cmd_metrics["dry_run_subcommands"],
			"staged_subcommands": cmd_metrics["staged_subcommands"],
			"total_cli_calls": cmd_metrics["total_cli_calls"],
			"dry_run_calls": cmd_metrics["dry_run_calls"],
			"staged_calls": cmd_metrics["staged_calls"],
			"live_cli_calls": cmd_metrics["live_cli_calls"],
			"domain_cli_calls": cmd_metrics["domain_cli_calls"],
			"help_calls": cmd_metrics["help_calls"],
			"self_corrections": cmd_metrics["self_corrections"],
			"redundant_calls": cmd_metrics["redundant_calls"],
			"has_redundancy": cmd_metrics["has_redundancy"],
			"unexpected_errors": cmd_metrics["unexpected_errors"],
			"first_try_success": first_try,
			"conflict_recovered": conflict_recovered,
			"wall_clock_ms": round(wall_ms, 1),
			"final_answer": entry.get("final_answer") or "",
			"db_verification": db_res,
		})

	n = len(scored_results)
	total_cli_calls = sum(r["total_cli_calls"] for r in scored_results)
	total_redundant_incl_help = sum(r["redundant_calls"] + r["help_calls"] for r in scored_results)
	conflict_rows = [r for r in scored_results if r["conflict_recovered"] is not None]

	kpis = {
		"round": round_num,
		"profile": profile or resolved_manifest.get("profile", "eval_standard"),
		"target_date": resolved_date,
		"scenarios_total": n,
		"scenarios_db_verified": sum(1 for r in scored_results if r["db_verification"]["db_verified"]),
		"db_verification_rate": round(sum(1 for r in scored_results if r["db_verification"]["db_verified"]) / n, 4) if n else 0.0,
		"first_try_success": round(sum(1 for r in scored_results if r["first_try_success"]) / n, 4) if n else 0.0,
		"avg_cli_calls": round(total_cli_calls / n, 2) if n else 0.0,
		"avg_live_cli_calls": round(sum(r["live_cli_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_staged_calls": round(sum(r["staged_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_domain_cli_calls": round(sum(r["domain_cli_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_dry_run_calls": round(sum(r["dry_run_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_help_calls": round(sum(r["help_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_self_corrections": round(sum(r["self_corrections"] for r in scored_results) / n, 2) if n else 0.0,
		"redundant_call_rate": round(sum(1 for r in scored_results if r["has_redundancy"]) / n, 4) if n else 0.0,
		"call_redundancy_rate": round(total_redundant_incl_help / total_cli_calls, 4) if total_cli_calls else 0.0,
		"conflict_recovery_rate": (
			round(sum(1 for r in conflict_rows if r["conflict_recovered"]) / len(conflict_rows), 4)
			if conflict_rows
			else None
		),
		"total_wall_clock_ms": round(sum(r["wall_clock_ms"] for r in scored_results), 1),
	}
	return {"kpis": kpis, "results": scored_results}


def _parse_set_assignments(items: list[str]) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
	"""Parse `key=val` or `stepN.key=val` CLI overrides into global and per-step maps."""
	global_vals: dict[str, Any] = {}
	step_vals: dict[int, dict[str, Any]] = {}
	for raw in items:
		if "=" not in raw:
			raise ValueError(f"Expected key=value assignment for --set, got '{raw}'")
		key, val = raw.split("=", 1)
		key = key.strip()
		step_match = re.match(r"^(?:step)?(\d+)\.([A-Za-z0-9_]+)$", key)
		if step_match:
			s_idx = int(step_match.group(1))
			f_name = step_match.group(2)
			step_vals.setdefault(s_idx, {})[f_name] = val
		else:
			global_vals[key] = val
	return global_vals, step_vals


def _http_local_json(
	url: str,
	*,
	method: str = "GET",
	body: Optional[dict[str, Any]] = None,
	headers: Optional[dict[str, str]] = None,
	timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
	opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
	req_headers = dict(headers or {})
	data_bytes: Optional[bytes] = None
	if body is not None:
		data_bytes = json.dumps(body).encode("utf-8")
		req_headers["Content-Type"] = "application/json"
	req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
	try:
		with opener.open(req, timeout=timeout) as resp:
			raw = resp.read().decode("utf-8", errors="replace")
			return int(resp.status), (json.loads(raw) if raw.strip() else {})
	except urllib.error.HTTPError as exc:
		raw = exc.read().decode("utf-8", errors="replace")
		try:
			payload = json.loads(raw) if raw.strip() else {"success": False, "error": str(exc)}
		except json.JSONDecodeError:
			payload = {"success": False, "error": raw or str(exc)}
		return int(exc.code), payload


def submit_staged_url(
	url: str,
	*,
	set_items: Optional[list[str]] = None,
	expect_missing: Optional[list[str]] = None,
	timeout: float = 30.0,
) -> dict[str, Any]:
	"""Simulate an operator inspecting and submitting a --stage 127.0.0.1 URL."""
	parsed = urllib.parse.urlsplit(url)
	if parsed.hostname not in ("127.0.0.1", "localhost"):
		raise ValueError(f"Refusing non-localhost stage URL '{url}'")
	query = urllib.parse.parse_qs(parsed.query)
	stage_id = (query.get("id") or [""])[0].strip()
	token = (query.get("token") or [""])[0].strip()
	if not stage_id or not token:
		raise ValueError(f"Stage URL missing id or token query parameters: '{url}'")

	origin = f"{parsed.scheme}://{parsed.netloc}"
	get_url = f"{origin}/api/stage/{urllib.parse.quote(stage_id)}?token={urllib.parse.quote(token)}"
	get_status, stage_state = _http_local_json(get_url, method="GET", timeout=timeout)
	if get_status != 200:
		return {
			"success": False,
			"stage_id": stage_id,
			"http_status": get_status,
			"missing_check_passed": False,
			"error": stage_state.get("error") or f"GET stage failed ({get_status})",
			"response": stage_state,
		}

	missing_required = list(stage_state.get("missing_required") or [])
	expected_list = [m.strip() for m in (expect_missing or []) if m.strip()]
	missing_check_passed = all(
		any(exp in str(actual) for actual in missing_required)
		for exp in expected_list
	)
	if expected_list and not missing_check_passed:
		return {
			"success": False,
			"stage_id": stage_id,
			"http_status": 400,
			"missing_check_passed": False,
			"expected_missing": expected_list,
			"actual_missing": missing_required,
			"error": f"Expected missing_required {expected_list}, got {missing_required}",
		}

	global_vals, step_vals = _parse_set_assignments(set_items or [])
	commands = stage_state.get("commands") or []
	mode = str(stage_state.get("mode") or ("batch" if len(commands) > 1 else "single"))
	post_body: dict[str, Any] = {}
	if mode == "batch" or len(commands) > 1 or step_vals:
		steps_payload: list[dict[str, Any]] = []
		for idx, step in enumerate(commands):
			field_names = {f.get("name") for f in (step.get("fields") or []) if isinstance(f, dict)}
			merged: dict[str, Any] = {}
			for k, v in global_vals.items():
				if k in field_names:
					merged[k] = v
			for k, v in step_vals.get(idx, {}).items():
				merged[k] = v
			steps_payload.append({"values": merged})
		post_body["steps"] = steps_payload
	elif global_vals:
		post_body["values"] = global_vals

	submit_url = f"{origin}/api/stage/{urllib.parse.quote(stage_id)}/submit"
	post_status, submit_resp = _http_local_json(
		submit_url,
		method="POST",
		body=post_body,
		headers={"Origin": origin, "X-Stage-Token": token},
		timeout=timeout,
	)
	return {
		"success": post_status == 200 and bool(submit_resp.get("success")),
		"stage_id": stage_id,
		"http_status": post_status,
		"missing_check_passed": missing_check_passed,
		"initial_missing_required": missing_required,
		"receipt": submit_resp.get("receipt"),
		"conflict": submit_resp.get("conflict"),
		"alternatives": submit_resp.get("alternatives") or [],
		"response": submit_resp,
	}


def bootstrap_slot(
	slot: int,
	*,
	profile: str = "eval_standard",
	clean_reinstall: bool = False,
	manifest_out: Optional[str | Path] = None,
	activate_dotenv: bool = False,
	slot_runner: Optional[Callable[[list[str]], Any]] = None,
) -> dict[str, Any]:
	"""Seed a SimManifest profile, provision API keys, and start the HTTP server on bench slot N."""
	if profile not in PROFILE_EXECUTE_FNS or profile == "monte_carlo":
		raise ValueError(f"Unsupported bootstrap profile '{profile}'; choose clean_master, eval_standard, or swarm_crunch_day.")
	execute_fn = PROFILE_EXECUTE_FNS[profile]
	manifest = seed_profile_in_slot(
		slot,
		execute_fn=execute_fn,
		clean_reinstall=clean_reinstall,
		keep_db=not clean_reinstall,
		runner=slot_runner,
	)
	if manifest_out:
		out_path = Path(manifest_out)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

	creds = provision_agent_api_keys_in_slot(slot, runner=slot_runner)
	base_url = start_slot_server(slot, keep_db=True, runner=slot_runner)
	dotenv_info: Optional[dict[str, str]] = None
	if activate_dotenv:
		dotenv_info = configure_slot_dotenv(slot, creds, base_url=base_url)

	return {
		"success": True,
		"slot": int(slot),
		"profile": profile,
		"base_url": base_url,
		"site": f"wt{int(slot)}.localhost",
		"shop_today": manifest.get("anchor_date"),
		"target_date": (manifest.get("metadata") or {}).get("target_date"),
		"manifest_path": str(manifest_out) if manifest_out else None,
		"dotenv_configured": dotenv_info is not None,
	}


def teardown_slot(
	slot: int,
	*,
	restore_dotenv: bool = False,
	slot_runner: Optional[Callable[[list[str]], Any]] = None,
) -> dict[str, Any]:
	"""Stop bench slot HTTP server and optionally restore backed-up .env."""
	stop_slot_server(slot, runner=slot_runner)
	restored = restore_slot_dotenv() if restore_dotenv else False
	return {"success": True, "slot": int(slot), "dotenv_restored": restored}


def _add_manifest_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
	kw_str: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
	kw_bool: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
	parser.add_argument("--manifest", help="Path to SimManifest JSON file", **kw_str)
	parser.add_argument("--seed-slot", type=int, help="Seed eval_standard in bench slot N and parse its SimManifest JSON", **kw_str)
	parser.add_argument("--clean-reinstall", action="store_true", help="Pass -CleanReinstall when seeding via --seed-slot", **kw_bool)


def _add_filter_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
	kw_str: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
	kw_int: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
	kw_bool: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
	parser.add_argument("--profile", choices=("eval_standard", "swarm_crunch_day", "all"), help="Scenario world profile", **kw_str)
	parser.add_argument("--tier", choices=("full", "core"), help="Scenario suite tier (full=40, core=15)", **kw_str)
	parser.add_argument("--skill", choices=("advisor", "dispatcher", "technician", "parts", "executive"), help="Filter by shop persona", **kw_str)
	parser.add_argument("--wave", type=int, choices=(1, 2), help="Filter by execution wave (1=reads, 2=writes)", **kw_int)
	parser.add_argument("--ids", help="Comma-separated scenario IDs or prefixes (e.g. S01,S13,C04)", **kw_str)
	parser.add_argument("--relative-dates", action="store_true", help="Render relative 'tomorrow' phrasing instead of ISO dates", **kw_bool)


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="ISM Agent OS Blinded NL Eval Loop (40 Scenarios)")
	_add_manifest_args(parser, suppress_defaults=True)
	_add_filter_args(parser, suppress_defaults=True)
	sub = parser.add_subparsers(dest="command", required=True)

	p_scen = sub.add_parser("scenarios", help="Print blinded NL eval scenarios as JSON")
	_add_manifest_args(p_scen)
	_add_filter_args(p_scen)

	p_seed = sub.add_parser("seed", help="Seed profile in a bench slot and output SimManifest JSON")
	p_seed.add_argument("--seed-slot", "--slot", dest="seed_slot", type=int, required=True, help="Bench slot number (1..8)")
	p_seed.add_argument("--profile", default="eval_standard", choices=("clean_master", "eval_standard", "swarm_crunch_day"))
	p_seed.add_argument("--clean-reinstall", action="store_true", help="Pass -CleanReinstall to test-in-slot.ps1")
	p_seed.add_argument("--output", help="Optional path to write the seeded SimManifest JSON")

	p_boot = sub.add_parser("bootstrap-slot", help="Seed profile, provision keys, start slot HTTP server, and configure .env")
	p_boot.add_argument("--slot", type=int, required=True, help="Bench slot number (1..5)")
	p_boot.add_argument("--profile", default="eval_standard", choices=("clean_master", "eval_standard", "swarm_crunch_day"))
	p_boot.add_argument("--clean-reinstall", action="store_true")
	p_boot.add_argument("--manifest-out", help="Path to write SimManifest JSON")
	p_boot.add_argument("--activate-dotenv", action="store_true", help="Back up .env and write slot credentials into ism-agent-os/.env")

	p_sub = sub.add_parser("submit-stage", help="Simulate operator submission against a --stage 127.0.0.1 URL")
	p_sub.add_argument("--url", required=True, help="127.0.0.1 stage URL returned by --stage CLI")
	p_sub.add_argument("--set", action="append", dest="set_items", help="Field override key=value or stepN.key=value")
	p_sub.add_argument("--expect-missing", action="append", help="Assert field appeared in missing_required before filling")

	p_td = sub.add_parser("teardown-slot", help="Stop bench slot HTTP server and optionally restore .env")
	p_td.add_argument("--slot", type=int, required=True, help="Bench slot number (1..5)")
	p_td.add_argument("--restore-dotenv", action="store_true", help="Restore ism-agent-os/.env from backup")

	p_vdb = sub.add_parser("verify-db", help="Verify sub-agent results against live Frappe DB")
	p_vdb.add_argument("--input", required=True, help="Path to raw sub-agent JSON array")
	p_vdb.add_argument("--target-date", help="Target tomorrow date (YYYY-MM-DD)")
	_add_manifest_args(p_vdb)
	_add_filter_args(p_vdb)

	p_score = sub.add_parser("score", help="Verify DB state, compute KPIs, and save round_<N>.json")
	p_score.add_argument("--round", type=int, required=True, help="Round number (e.g. 0)")
	p_score.add_argument("--input", required=True, help="Path to raw sub-agent JSON array")
	p_score.add_argument("--target-date", help="Target tomorrow date (YYYY-MM-DD)")
	p_score.add_argument("--skip-db", action="store_true", help="Skip live DB verification (offline scoring)")
	p_score.add_argument("--output", help="Optional custom output path")
	_add_manifest_args(p_score)
	_add_filter_args(p_score)

	return parser


def _normalize_argv(argv: Optional[list[str]]) -> list[str]:
	tokens = list(sys.argv[1:] if argv is None else argv)
	subcommands = {"scenarios", "seed", "bootstrap-slot", "submit-stage", "teardown-slot", "verify-db", "score"}
	if not tokens:
		return tokens
	has_sub = any(tok in subcommands for tok in tokens)
	if not has_sub and any(tok == "--round" or tok.startswith("--round=") or tok == "--input" or tok.startswith("--input=") for tok in tokens):
		return ["score", *tokens]
	return tokens


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(_normalize_argv(argv))

	if args.command == "seed":
		manifest = seed_profile_in_slot(
			args.seed_slot,
			execute_fn=PROFILE_EXECUTE_FNS[args.profile],
			clean_reinstall=bool(args.clean_reinstall),
			keep_db=not bool(args.clean_reinstall),
		)
		rendered = json.dumps(manifest, indent=2)
		if args.output:
			Path(args.output).write_text(rendered, encoding="utf-8")
		print(rendered)
		return 0

	if args.command == "bootstrap-slot":
		res = bootstrap_slot(
			args.slot,
			profile=args.profile,
			clean_reinstall=bool(args.clean_reinstall),
			manifest_out=args.manifest_out,
			activate_dotenv=bool(args.activate_dotenv),
		)
		print(json.dumps(res, indent=2))
		return 0

	if args.command == "submit-stage":
		res = submit_staged_url(
			args.url,
			set_items=list(args.set_items or []),
			expect_missing=list(args.expect_missing or []),
		)
		print(json.dumps(res, indent=2))
		return 0 if res.get("success") else 1

	if args.command == "teardown-slot":
		res = teardown_slot(args.slot, restore_dotenv=bool(args.restore_dotenv))
		print(json.dumps(res, indent=2))
		return 0

	profile_arg = getattr(args, "profile", None)
	tier_arg = getattr(args, "tier", "full") or "full"
	skill_arg = getattr(args, "skill", None)
	wave_arg = getattr(args, "wave", None)
	ids_raw = getattr(args, "ids", None)
	ids_list = [tok.strip() for tok in str(ids_raw).split(",") if tok.strip()] if ids_raw else None
	relative_dates = bool(getattr(args, "relative_dates", False))

	manifest_path = getattr(args, "manifest", None)
	seed_slot = getattr(args, "seed_slot", None)
	if manifest_path or seed_slot is not None:
		exec_fn = PROFILE_EXECUTE_FNS.get(profile_arg or "eval_standard", "ism.testing.profiles.seed_eval_standard")
		manifest = resolve_manifest_arg(
			manifest_path=manifest_path,
			seed_slot=seed_slot,
			clean_reinstall=bool(getattr(args, "clean_reinstall", False)),
			execute_fn=exec_fn,
		)
	elif profile_arg == "swarm_crunch_day":
		manifest = default_offline_crunch_manifest()
	else:
		manifest = default_offline_eval_manifest()

	if args.command == "scenarios":
		sc_list = get_scenarios(
			manifest=manifest,
			profile=profile_arg,
			tier=tier_arg,
			skill=skill_arg,
			wave=wave_arg,
			ids=ids_list,
			relative_dates=relative_dates,
		)
		print(json.dumps([asdict(s) for s in sc_list], indent=2))
		return 0

	raw_path = Path(args.input)
	raw_entries = json.loads(raw_path.read_text(encoding="utf-8-sig"))
	if isinstance(raw_entries, dict) and "results" in raw_entries:
		raw_entries = raw_entries["results"]

	if args.command == "score" and args.skip_db:
		target_date = getattr(args, "target_date", None) or _resolve_target_date(None, manifest=manifest)
		report = score_round(
			client=None,
			round_num=args.round,
			raw_entries=raw_entries,
			target_date=target_date,
			skip_db=True,
			manifest=manifest,
			profile=profile_arg,
			tier=tier_arg,
		)
		_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
		out_path = Path(args.output) if args.output else (_RESULTS_DIR / f"round_{args.round}.json")
		out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
		print(json.dumps({"saved_to": str(out_path), "kpis": report["kpis"]}, indent=2))
		return 0

	client = IsmAgentClient(IsmConfig.from_env())
	try:
		target_date = getattr(args, "target_date", None) or _resolve_target_date(client, manifest=manifest)

		if args.command == "verify-db":
			scenarios_by_id = {
				s.id: s
				for s in get_scenarios(
					manifest=manifest,
					profile=profile_arg or "all",
					tier=tier_arg,
					skill=skill_arg,
					wave=wave_arg,
					ids=ids_list,
					relative_dates=relative_dates,
				)
			}
			out = []
			for entry in raw_entries:
				sc = scenarios_by_id.get(entry.get("id"))
				if sc:
					out.append({"id": sc.id, **verify_scenario_db(client, sc, entry, target_date, manifest=manifest)})
			print(json.dumps(out, indent=2))
			return 0 if all(item["db_verified"] for item in out) else 1

		if args.command == "score":
			report = score_round(
				client=client,
				round_num=args.round,
				raw_entries=raw_entries,
				target_date=target_date,
				skip_db=False,
				manifest=manifest,
				profile=profile_arg,
				tier=tier_arg,
			)
			_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
			out_path = Path(args.output) if args.output else (_RESULTS_DIR / f"round_{args.round}.json")
			out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
			print(json.dumps({"saved_to": str(out_path), "kpis": report["kpis"]}, indent=2))
			return 0
	finally:
		client._tx.close()
	return 1


if __name__ == "__main__":
	sys.exit(main())

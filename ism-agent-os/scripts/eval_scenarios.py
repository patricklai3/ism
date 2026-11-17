#!/usr/bin/env python3
"""Deterministic 24-scenario CLI contract suite spanning all 5 ISM shop personas."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

from scripts.sim_runner import (  # noqa: E402
	default_offline_eval_manifest,
	interpolate_manifest,
	interpolate_value,
	resolve_manifest_arg,
	resolve_manifest_path,
)


@dataclass
class StepSpec:
	"""Single CLI invocation step within a scenario."""

	command: str
	args: list[str]
	expect_error: bool = False
	check_fn: Optional[Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]] = None


@dataclass
class ScenarioSpec:
	"""Natural-language prompt paired with expected CLI execution and assertions."""

	id: str
	nl_prompt: str
	expected_commands: list[str]
	steps: list[StepSpec]
	is_conflict_scenario: bool = False


@dataclass
class ScenarioResult:
	"""Scored result for one evaluation scenario."""

	id: str
	nl_prompt: str
	expected_commands: list[str]
	executed_commands: list[str]
	tool_choice_score: float
	param_accuracy_score: float
	turn_count: int
	conflict_feedback: Optional[bool]
	wall_clock_ms: float
	checks_passed: int
	checks_total: int
	failures: list[str] = field(default_factory=list)


def _run_cli(args: list[str]) -> tuple[int, str, str, Optional[dict[str, Any]], float]:
	"""Run `python -m tools.cli --json ...` and return (rc, stdout, stderr, parsed_json, ms)."""
	cmd = [sys.executable, "-m", "tools.cli", "--json", *args]
	t0 = time.perf_counter()
	proc = subprocess.run(
		cmd,
		cwd=str(_REPO_ROOT),
		capture_output=True,
		text=True,
		timeout=60,
	)
	elapsed_ms = (time.perf_counter() - t0) * 1000.0
	parsed: Optional[dict[str, Any]] = None
	stdout = (proc.stdout or "").strip()
	if stdout:
		try:
			obj = json.loads(stdout)
			if isinstance(obj, dict):
				parsed = obj
			elif isinstance(obj, list):
				parsed = {"items": obj}
		except json.JSONDecodeError:
			parsed = None
	return proc.returncode, stdout, (proc.stderr or "").strip(), parsed, elapsed_ms


def _make_check_e01_status(target_date: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
	def _check(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
		summary = data.get("data", {}).get("summary", {})
		return [
			("rc_zero", rc == 0),
			("success_true", data.get("success") is True),
			("date_matches", data.get("data", {}).get("date") == target_date),
			("active_bays_present", isinstance(summary.get("active_bays"), int) and summary["active_bays"] >= 4),
			("scheduled_appts_present", isinstance(summary.get("scheduled_appointments"), int) and summary["scheduled_appointments"] >= 2),
		]
	return _check


def _make_check_e02_tech_overview(target_date: str, elena_id: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
	def _check(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
		techs = data.get("data", {}).get("technicians", [])
		elena = [t for t in techs if t.get("employee") == elena_id]
		return [
			("rc_zero", rc == 0),
			("success_true", data.get("success") is True),
			("date_matches", data.get("data", {}).get("date") == target_date),
			("elena_present", len(elena) == 1),
			("elena_on_leave", bool(elena and elena[0].get("on_leave") is True)),
		]
	return _check


def _check_e03_slots_equipment(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	slots = data.get("data", {}).get("slots", [])
	bays = data.get("data", {}).get("resources", {}).get("bays", {})
	has_rack_bay = any("Alignment Rack" in tags for tags in bays.values())
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("slots_found", len(slots) > 0),
		("duration_60", data.get("data", {}).get("duration_minutes") == 60),
		("alignment_rack_bay_present", has_rack_bay),
	]


def _check_e04_slots_afternoon(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	slots = data.get("data", {}).get("slots", [])
	all_pm = all(str(s.get("start_time") or s.get("start") or "13:00") >= "13:00" for s in slots if isinstance(s, dict))
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("slots_found", len(slots) > 0),
		("afternoon_filtered", all_pm),
	]


def _check_e05_unscheduled(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	services = d.get("services") or []
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("has_unscheduled_service", d.get("unscheduled_count", 0) >= 1 and len(services) >= 1),
	]


def _check_e06_vin_batch(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	decode_d = (results[0].get("result", {}) if len(results) >= 1 else {}).get("data", {})
	info_ok = len(results) >= 2 and results[1].get("ok") is True
	return [
		("rc_zero", rc == 0),
		("batch_total_2", data.get("total") == 2 and data.get("succeeded") == 2),
		("model_3_decoded", decode_d.get("model") == "Model 3"),
		("vehicle_info_ok", info_ok),
	]


def _check_e07_plate_search(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	vehicles = d.get("vehicles") or d.get("items") or []
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("vehicle_matched", isinstance(vehicles, list) and len(vehicles) >= 1),
	]


def _check_e08_deferred(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	items = d.get("deferred_services") or d.get("items") or d.get("services") or []
	has_hazard = any(bool(it.get("safety_hazard")) for it in items if isinstance(it, dict))
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("deferred_items_present", isinstance(items, list) and len(items) >= 1),
		("safety_hazard_flagged", has_hazard),
	]


def _check_e09_pricing_baseline_and_small(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	r0 = (results[0].get("result", {}) if len(results) >= 1 else {}).get("data", {})
	r1 = (results[1].get("result", {}) if len(results) >= 2 else {}).get("data", {})
	return [
		("rc_zero", rc == 0),
		("batch_ok", data.get("succeeded") == 2),
		("baseline_140", abs(float(r0.get("sell_price") or 0) - 140.0) < 0.01 and r0.get("rule_applied") == "baseline_offset"),
		("small_ticket_10", abs(float(r1.get("sell_price") or 0) - 10.0) < 0.01 and r1.get("rule_applied") == "small_ticket_match"),
	]


def _check_e10_pricing_floor_and_fallback(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	r0 = (results[0].get("result", {}) if len(results) >= 1 else {}).get("data", {})
	r1 = (results[1].get("result", {}) if len(results) >= 2 else {}).get("data", {})
	return [
		("rc_zero", rc == 0),
		("batch_ok", data.get("succeeded") == 2),
		("cost_floor_118_75", abs(float(r0.get("sell_price") or 0) - 118.75) < 0.01 and r0.get("rule_applied") == "cost_floor"),
		("fallback_35", abs(float(r1.get("sell_price") or 0) - 35.0) < 0.01 and r1.get("rule_applied") == "cost_plus_fallback"),
	]


def _check_e11_intake_slots(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	ro_obj = d.get("repair_order") or {}
	ro_name = ro_obj.get("name") if isinstance(ro_obj, dict) else d.get("name")
	slots = d.get("slots") or []
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("ro_created", isinstance(ro_name, str) and ro_name.startswith("RO-")),
		("slots_returned", isinstance(slots, list) and len(slots) > 0),
	]


def _make_check_dry_run_missing(expected_token: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
	def _check(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
		missing = data.get("missing_required") or []
		return [
			("rc_zero", rc == 0),
			("dry_run_true", data.get("dry_run") is True),
			("ready_false", data.get("ready") is False),
			(f"missing_{expected_token}", any(expected_token in str(m) for m in missing)),
		]
	return _check


def _check_e13_quote_amend_approval_batch(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	r0 = (results[0].get("result", {}) if len(results) >= 1 else {})
	r1 = (results[1].get("result", {}) if len(results) >= 2 else {})
	r2 = (results[2].get("result", {}) if len(results) >= 3 else {})
	ro_name = (r0.get("data") or {}).get("name")
	total_services = (r1.get("data") or {}).get("total_services")
	approval_url = (r2.get("urls") or {}).get("approval")
	return [
		("rc_zero", rc == 0),
		("batch_3_ok", data.get("total") == 3 and data.get("succeeded") == 3 and data.get("failed") == 0),
		("ro_created", isinstance(ro_name, str) and ro_name.startswith("RO-")),
		("services_count_2", total_services == 2),
		("approval_url_present", isinstance(approval_url, str) and "token=" in approval_url),
	]


def _check_e14_estimate(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	est = d.get("p80_minutes") or d.get("estimated_minutes") or d.get("total_minutes")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("duration_positive", isinstance(est, (int, float)) and est > 0),
	]


def _check_e15_alternates_and_templates(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	alts = (results[0].get("result", {}) if len(results) >= 1 else {}).get("data", {}).get("alternates") or []
	tpls = (results[1].get("result", {}) if len(results) >= 2 else {}).get("data", {}).get("templates") or []
	return [
		("rc_zero", rc == 0),
		("batch_ok", data.get("succeeded") == 2),
		("alternates_found", isinstance(alts, list) and len(alts) >= 1),
		("templates_found", isinstance(tpls, list) and len(tpls) >= 1),
	]


def _make_check_e16_bay_conflict(bay_name: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
	bay_lower = bay_name.lower()

	def _check(data: dict[str, Any], raw: str, rc: int) -> list[tuple[str, bool]]:
		d = data.get("data", {})
		msg = str(data.get("message") or data.get("error") or raw or "")
		lowered = msg.lower()
		urls = data.get("urls") or d.get("urls") or {}
		alts = d.get("alternative_slots") or []
		return [
			("rc_nonzero", rc != 0),
			("success_false", data.get("success") is False),
			("status_conflict", d.get("status") == "conflict" and d.get("conflict") is True),
			("conflict_message_clear", ("occupied" in lowered or "conflict" in lowered) and bay_lower in lowered),
			("urls_conflicting_entry", isinstance(urls, dict) and bool(urls.get("conflicting_entry"))),
			("alternative_slots_present", isinstance(alts, list) and len(alts) > 0),
		]
	return _check


def _check_e17_ambiguous_bay(data: dict[str, Any], raw: str, rc: int) -> list[tuple[str, bool]]:
	msg = f"{data.get('message', '')} {data.get('error', '')} {raw}".lower()
	return [
		("rc_nonzero", rc != 0),
		("success_false", data.get("success") is False),
		("ambiguous_bay_surfaced", "ambiguous" in msg or "multiple" in msg or "available" in msg),
	]


def _check_e18_leave_conflict(data: dict[str, Any], raw: str, rc: int) -> list[tuple[str, bool]]:
	msg = f"{data.get('message', '')} {data.get('error', '')} {raw}".lower()
	return [
		("rc_nonzero", rc != 0),
		("success_false", data.get("success") is False),
		("leave_or_break_rejected", any(tok in msg for tok in ("leave", "break", "unavailable", "conflict"))),
	]


def _check_e19_offline_bay(data: dict[str, Any], raw: str, rc: int) -> list[tuple[str, bool]]:
	msg = f"{data.get('message', '')} {data.get('error', '')} {raw}".lower()
	return [
		("rc_nonzero", rc != 0),
		("success_false", data.get("success") is False),
		("offline_rejected", bool(msg)),
	]


def _check_e20_check_stock(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("deficit_detected", d.get("all_available") is False or int(d.get("deficit_count") or 0) > 0),
	]


def _check_e21_open_po_lookup(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	rows = data.get("items") or data.get("data") or []
	return [
		("rc_zero", rc == 0),
		("po_returned", isinstance(rows, list) and len(rows) >= 1),
	]


def _check_e22_action_items(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	items = d.get("items")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("items_list_valid", isinstance(items, list)),
	]


def build_scenarios(manifest: Optional[dict[str, Any]] = None) -> list[ScenarioSpec]:
	"""Return the 24-scenario CLI contract suite interpolated against a SimManifest."""
	m = manifest if manifest is not None else default_offline_eval_manifest()
	target_date = str(resolve_manifest_path(m, "metadata.target_date"))
	elena_id = str(resolve_manifest_path(m, "techs.elena"))
	main_lift = str(resolve_manifest_path(m, "bays.main_lift"))

	templates = [
		# Dispatcher Reads & Temporal Windowing (E01-E05)
		ScenarioSpec(
			id="E01_shop_status",
			nl_prompt="Check shop floor status for {{metadata.target_date}}.",
			expected_commands=["status"],
			steps=[StepSpec("status", ["status", "--date", "{{metadata.target_date}}"], check_fn=_make_check_e01_status(target_date))],
		),
		ScenarioSpec(
			id="E02_tech_overview_leave",
			nl_prompt="Show technician utilization and who is on leave on {{metadata.target_date}}.",
			expected_commands=["tech-overview"],
			steps=[StepSpec("tech-overview", ["tech-overview", "--date", "{{metadata.target_date}}"], check_fn=_make_check_e02_tech_overview(target_date, elena_id))],
		),
		ScenarioSpec(
			id="E03_slots_alignment_rack",
			nl_prompt="Find 60-minute available slots on {{metadata.target_date}} with an Alignment Rack.",
			expected_commands=["slots"],
			steps=[StepSpec("slots", ["slots", "--date", "{{metadata.target_date}}", "--duration", "60", "--days", "1", "--tags", "Alignment Rack"], check_fn=_check_e03_slots_equipment)],
		),
		ScenarioSpec(
			id="E04_slots_afternoon_lift",
			nl_prompt="Find 90-minute Lift slots on {{metadata.target_date}} afternoon.",
			expected_commands=["slots"],
			steps=[StepSpec("slots", ["slots", "--date", "{{metadata.target_date}}", "--duration", "90", "--days", "1", "--tags", "Lift", "--min-time", "13:00:00"], check_fn=_check_e04_slots_afternoon)],
		),
		ScenarioSpec(
			id="E05_unscheduled_queue",
			nl_prompt="List unscheduled services on {{ros.unscheduled_ro}} waiting for a bay.",
			expected_commands=["unscheduled"],
			steps=[StepSpec("unscheduled", ["unscheduled", "--repair-order", "{{ros.unscheduled_ro}}"], check_fn=_check_e05_unscheduled)],
		),
		# Advisor Intelligence & Tesla-Offset Pricing (E06-E10)
		ScenarioSpec(
			id="E06_decode_and_vehicle_intel_batch",
			nl_prompt="Decode VIN {{vehicles.veh_model3_liam}} and pull its vehicle intelligence.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					"batch",
					[
						"batch",
						"--cmd",
						"decode-vin --vin {{vehicles.veh_model3_liam}}",
						"--cmd",
						"vehicle-info --vehicle {{vehicles.veh_model3_liam}}",
					],
					check_fn=_check_e06_vin_batch,
				)
			],
		),
		ScenarioSpec(
			id="E07_plate_vehicle_search",
			nl_prompt="Find vehicle with plate LEV-301.",
			expected_commands=["search-vehicles"],
			steps=[StepSpec("search-vehicles", ["search-vehicles", "--query", "LEV-301"], check_fn=_check_e07_plate_search)],
		),
		ScenarioSpec(
			id="E08_deferred_services_hazards",
			nl_prompt="Check open deferred recommendations for {{vehicles.veh_model3_liam}}.",
			expected_commands=["deferred"],
			steps=[StepSpec("deferred", ["deferred", "--vehicle", "{{vehicles.veh_model3_liam}}"], check_fn=_check_e08_deferred)],
		),
		ScenarioSpec(
			id="E09_price_part_baseline_and_small",
			nl_prompt="Price a $200 Tesla / $40 cost aftermarket duct and a $10 Tesla / $4 cost clip.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					"batch",
					[
						"batch",
						"--cmd",
						"price-part --tesla-price 200 --cost 40 --source Aftermarket --condition New",
						"--cmd",
						"price-part --tesla-price 10 --cost 4 --source OEM --condition New",
					],
					check_fn=_check_e09_pricing_baseline_and_small,
				)
			],
		),
		ScenarioSpec(
			id="E10_price_part_floor_and_fallback",
			nl_prompt="Price a $160 Tesla / $95 cost shield and a $25 cost unlisted shop fitting.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					"batch",
					[
						"batch",
						"--cmd",
						"price-part --tesla-price 160 --cost 95 --source Aftermarket --condition New",
						"--cmd",
						"price-part --cost 25 --source Aftermarket --condition New",
					],
					check_fn=_check_e10_pricing_floor_and_fallback,
				)
			],
		),
		# Advisor Intake, Dry-Run Gate, $stepN Batching & Estimates (E11-E15)
		ScenarioSpec(
			id="E11_intake_slots_composite",
			nl_prompt="Intake a draft quote for {{customers.retail_1}} on {{vehicles.veh_model3_liam}} and find {{metadata.target_date}} >= 9am slots with {{bays.main_lift}} and Marcus.",
			expected_commands=["intake-slots"],
			steps=[
				StepSpec(
					"intake-slots",
					[
						"intake-slots",
						"--customer",
						"{{customers.retail_1}}",
						"--vehicle",
						"{{vehicles.veh_model3_liam}}",
						"--odometer-in",
						"48200",
						"--service",
						"Front Brake Inspection",
						"--date",
						"{{metadata.target_date}}",
						"--days",
						"2",
						"--min-time",
						"09:00:00",
						"--bay",
						"{{bays.main_lift}}",
						"--technician",
						"{{techs.marcus}}",
					],
					check_fn=_check_e11_intake_slots,
				)
			],
		),
		ScenarioSpec(
			id="E12_quote_missing_odometer_gate",
			nl_prompt="Dry-run quote missing odometer_in must flag --odometer-in.",
			expected_commands=["quote"],
			steps=[
				StepSpec(
					"quote",
					[
						"--dry-run",
						"quote",
						"--customer",
						"{{customers.retail_1}}",
						"--vehicle",
						"{{vehicles.veh_model3_liam}}",
						"--service",
						"Cabin Air Filter Replacement::0.5",
					],
					check_fn=_make_check_dry_run_missing("odometer-in"),
				)
			],
		),
		ScenarioSpec(
			id="E13_quote_amend_approval_step_chain",
			nl_prompt="Create a draft quote for {{customers.retail_1}}, amend with a second service, and get approval link via $step0 chaining.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					"batch",
					[
						"batch",
						"--cmd",
						"quote --customer '{{customers.retail_1}}' --vehicle {{vehicles.veh_model3_liam}} --odometer-in 48200 --service 'Tire Rotation::0.5'",
						"--cmd",
						"amend-quote --repair-order '$step0.data.name' --add-service 'Wiper Blade Replacement::0.3'",
						"--cmd",
						"send-approval --repair-order '$prev.data.name' --medium link",
					],
					check_fn=_check_e13_quote_amend_approval_batch,
				)
			],
		),
		ScenarioSpec(
			id="E14_estimate_duration",
			nl_prompt="Estimate probabilistic shop duration for {{ros.conflict_ro}}.",
			expected_commands=["estimate"],
			steps=[StepSpec("estimate", ["estimate", "--repair-order", "{{ros.conflict_ro}}"], check_fn=_check_e14_estimate)],
		),
		ScenarioSpec(
			id="E15_alternates_and_templates_batch",
			nl_prompt="Check brake rotor alternates and available inspection templates in one batch.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					"batch",
					[
						"batch",
						"--cmd",
						"alternates --service-package '{{packages.front_brake_overhaul}}' --slot-number 2",
						"--cmd",
						"inspection-templates",
					],
					check_fn=_check_e15_alternates_and_templates,
				)
			],
		),
		# Dispatcher Conflict & Resource Guards (E16-E19)
		ScenarioSpec(
			id="E16_bay_conflict_feedback",
			nl_prompt="Book {{ros.unscheduled_ro}} into {{bays.main_lift}} at {{metadata.target_date}} 09:00 (occupied).",
			expected_commands=["book"],
			is_conflict_scenario=True,
			steps=[
				StepSpec(
					"book",
					[
						"book",
						"--repair-order",
						"{{ros.unscheduled_ro}}",
						"--date",
						"{{metadata.target_date}}",
						"--time",
						"09:00:00",
						"--bay",
						"{{bays.main_lift}}",
						"--tech",
						"{{techs.devon}}",
						"--duration",
						"60",
					],
					expect_error=True,
					check_fn=_make_check_e16_bay_conflict(main_lift),
				)
			],
		),
		ScenarioSpec(
			id="E17_ambiguous_bay_feedback",
			nl_prompt="Book walk-in into ambiguous 'Bay' string and verify 417 listing.",
			expected_commands=["book"],
			is_conflict_scenario=True,
			steps=[
				StepSpec(
					"book",
					[
						"book",
						"--date",
						"{{metadata.target_date}}",
						"--time",
						"10:00:00",
						"--bay",
						"Bay",
						"--entry-type",
						"Diagnostic",
						"--provisional-customer",
						"Owen MacLeod",
						"--provisional-vehicle",
						"2022 Tesla Model Y",
						"--duration",
						"60",
					],
					expect_error=True,
					check_fn=_check_e17_ambiguous_bay,
				)
			],
		),
		ScenarioSpec(
			id="E18_leave_and_lunch_conflict",
			nl_prompt="Attempt booking {{ros.unscheduled_secondary_ro}} with Elena during lunch break on {{metadata.target_date}}.",
			expected_commands=["book"],
			is_conflict_scenario=True,
			steps=[
				StepSpec(
					"book",
					[
						"book",
						"--repair-order",
						"{{ros.unscheduled_secondary_ro}}",
						"--date",
						"{{metadata.target_date}}",
						"--time",
						"12:00:00",
						"--bay",
						"{{bays.alignment}}",
						"--tech",
						"{{techs.elena}}",
						"--duration",
						"60",
					],
					expect_error=True,
					check_fn=_check_e18_leave_conflict,
				)
			],
		),
		ScenarioSpec(
			id="E19_offline_bay_guard",
			nl_prompt="Attempt booking into inactive {{bays.offline}}.",
			expected_commands=["book"],
			is_conflict_scenario=True,
			steps=[
				StepSpec(
					"book",
					[
						"book",
						"--repair-order",
						"{{ros.unscheduled_secondary_ro}}",
						"--date",
						"{{metadata.target_date}}",
						"--time",
						"14:00:00",
						"--bay",
						"{{bays.offline}}",
						"--duration",
						"60",
					],
					expect_error=True,
					check_fn=_check_e19_offline_bay,
				)
			],
		),
		# Parts, Executive & Floor Write-Gate Contracts (E20-E24)
		ScenarioSpec(
			id="E20_parts_check_stock_deficit",
			nl_prompt="Check stock deficit on {{ros.open_po_ro}}.",
			expected_commands=["check-stock"],
			steps=[StepSpec("check-stock", ["check-stock", "--repair-order", "{{ros.open_po_ro}}"], check_fn=_check_e20_check_stock)],
		),
		ScenarioSpec(
			id="E21_parts_filtered_open_po_lookup",
			nl_prompt="Look up submitted Purchase Order for {{suppliers.tesla_oem}}.",
			expected_commands=["list-docs"],
			steps=[
				StepSpec(
					"list-docs",
					[
						"list-docs",
						"--doctype",
						"Purchase Order",
						"--filter",
						"supplier={{suppliers.tesla_oem}}",
						"--filter",
						"docstatus=1",
						"--fields",
						"name,supplier,status,per_received,per_billed,grand_total,advance_paid",
						"--limit",
						"5",
					],
					check_fn=_check_e21_open_po_lookup,
				)
			],
		),
		ScenarioSpec(
			id="E22_executive_action_items",
			nl_prompt="List open Shop Action Items for executive triage.",
			expected_commands=["action-items"],
			steps=[StepSpec("action-items", ["action-items", "--status", "Open"], check_fn=_check_e22_action_items)],
		),
		ScenarioSpec(
			id="E23_floor_complete_missing_odometer_gate",
			nl_prompt="Dry-run job-status --action complete without --odometer-out must flag missing odometer.",
			expected_commands=["job-status"],
			steps=[
				StepSpec(
					"job-status",
					["--dry-run", "job-status", "--repair-order", "{{ros.hv_scheduled_ro}}", "--action", "complete"],
					check_fn=_make_check_dry_run_missing("odometer-out"),
				)
			],
		),
		ScenarioSpec(
			id="E24_checkout_missing_payment_method_gate",
			nl_prompt="Dry-run checkout without --payment-method must flag missing payment method.",
			expected_commands=["checkout"],
			steps=[
				StepSpec(
					"checkout",
					["--dry-run", "checkout", "--repair-order", "{{ros.hv_scheduled_ro}}", "--odometer-out", "87950", "--confirm"],
					check_fn=_make_check_dry_run_missing("payment-method"),
				)
			],
		),
	]

	rendered: list[ScenarioSpec] = []
	for sc in templates:
		rendered_steps = [
			StepSpec(
				command=st.command,
				args=interpolate_value(st.args, m),
				expect_error=st.expect_error,
				check_fn=st.check_fn,
			)
			for st in sc.steps
		]
		rendered.append(
			ScenarioSpec(
				id=sc.id,
				nl_prompt=interpolate_manifest(sc.nl_prompt, m),
				expected_commands=list(sc.expected_commands),
				steps=rendered_steps,
				is_conflict_scenario=sc.is_conflict_scenario,
			)
		)
	return rendered


def run_suite(
	manifest: Optional[dict[str, Any]] = None,
	cli_runner: Optional[Callable[[list[str]], tuple[int, str, str, Optional[dict[str, Any]], float]]] = None,
) -> dict[str, Any]:
	"""Execute all 24 CLI contract scenarios against the supplied SimManifest."""
	resolved_manifest = manifest if manifest is not None else default_offline_eval_manifest()
	scenarios = build_scenarios(manifest=resolved_manifest)
	runner = cli_runner or _run_cli
	results: list[ScenarioResult] = []

	for sc in scenarios:
		executed_commands: list[str] = []
		total_ms = 0.0
		checks_passed = 0
		checks_total = 0
		failures: list[str] = []
		context_vars: dict[str, str] = {}
		conflict_ok: Optional[bool] = None if not sc.is_conflict_scenario else False

		for step in sc.steps:
			resolved_args = [
				a.replace("{ro_name}", context_vars["ro_name"]) if "{ro_name}" in a and "ro_name" in context_vars else a
				for a in step.args
			]
			executed_commands.append(step.command)
			rc, stdout, stderr, parsed, ms = runner(resolved_args)
			total_ms += ms
			raw_combined = f"{stdout}\n{stderr}".strip()
			payload = parsed or {}

			ro_name = payload.get("data", {}).get("name")
			if isinstance(ro_name, str) and ro_name.startswith("RO-"):
				context_vars["ro_name"] = ro_name

			if step.check_fn:
				step_checks = step.check_fn(payload, raw_combined, rc)
				for label, passed in step_checks:
					checks_total += 1
					if passed:
						checks_passed += 1
					else:
						failures.append(f"{step.command}:{label} (rc={rc}, out={raw_combined[:160]})")
				if sc.is_conflict_scenario:
					conflict_ok = all(p for _, p in step_checks)
			else:
				checks_total += 1
				ok = (rc != 0) if step.expect_error else (rc == 0)
				if ok:
					checks_passed += 1
				else:
					failures.append(f"{step.command}:unexpected_rc={rc}")

		tool_choice = 1.0 if executed_commands == sc.expected_commands else 0.0
		param_acc = (checks_passed / checks_total) if checks_total > 0 else 1.0

		results.append(
			ScenarioResult(
				id=sc.id,
				nl_prompt=sc.nl_prompt,
				expected_commands=sc.expected_commands,
				executed_commands=executed_commands,
				tool_choice_score=round(tool_choice, 4),
				param_accuracy_score=round(param_acc, 4),
				turn_count=len(sc.steps),
				conflict_feedback=conflict_ok,
				wall_clock_ms=round(total_ms, 1),
				checks_passed=checks_passed,
				checks_total=checks_total,
				failures=failures,
			)
		)

	n = len(results)
	conflict_results = [r for r in results if r.conflict_feedback is not None]
	summary = {
		"profile": resolved_manifest.get("profile", "eval_standard"),
		"target_date": resolve_manifest_path(resolved_manifest, "metadata.target_date"),
		"scenarios_total": n,
		"scenarios_fully_passed": sum(1 for r in results if r.checks_passed == r.checks_total),
		"avg_tool_choice_score": round(sum(r.tool_choice_score for r in results) / n, 4) if n else 0.0,
		"avg_param_accuracy_score": round(sum(r.param_accuracy_score for r in results) / n, 4) if n else 0.0,
		"avg_turn_count": round(sum(r.turn_count for r in results) / n, 2) if n else 0.0,
		"conflict_feedback_pass_rate": (
			round(sum(1 for r in conflict_results if r.conflict_feedback) / len(conflict_results), 4)
			if conflict_results
			else None
		),
		"total_wall_clock_ms": round(sum(r.wall_clock_ms for r in results), 1),
		"avg_wall_clock_ms": round(sum(r.wall_clock_ms for r in results) / n, 1) if n else 0.0,
	}
	return {"summary": summary, "results": [asdict(r) for r in results]}


def _print_table(report: dict[str, Any]) -> None:
	s = report["summary"]
	print("=" * 84)
	print("ISM AGENT OS — EVAL SCENARIOS BASELINE (24 CONTRACTS)")
	print("=" * 84)
	for r in report["results"]:
		cf = "-" if r["conflict_feedback"] is None else ("PASS" if r["conflict_feedback"] else "FAIL")
		print(
			f"{r['id']:<34} tool={r['tool_choice_score']:.2f} "
			f"param={r['param_accuracy_score']:.2f} ({r['checks_passed']}/{r['checks_total']}) "
			f"turns={r['turn_count']} conflict={cf:<4} time={r['wall_clock_ms']:>7.1f}ms"
		)
		for fail in r["failures"]:
			print(f"  FAIL: {fail}")
	print("-" * 84)
	conflict_str = f"{s['conflict_feedback_pass_rate']:.2%}" if s["conflict_feedback_pass_rate"] is not None else "n/a"
	print(
		f"SUMMARY: passed={s['scenarios_fully_passed']}/{s['scenarios_total']} | "
		f"tool_choice={s['avg_tool_choice_score']:.2%} | "
		f"param_acc={s['avg_param_accuracy_score']:.2%} | "
		f"avg_turns={s['avg_turn_count']} | "
		f"conflict_rate={conflict_str} | "
		f"total_time={s['total_wall_clock_ms']:.1f}ms"
	)


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Run ISM Agent OS CLI scenario benchmarks")
	parser.add_argument("--manifest", help="Path to SimManifest JSON file")
	parser.add_argument("--seed-slot", type=int, help="Seed eval_standard in bench slot N and use its SimManifest")
	parser.add_argument("--clean-reinstall", action="store_true", help="Pass -CleanReinstall when seeding via --seed-slot")
	parser.add_argument("--json", action="store_true", help="Output full JSON report")
	parser.add_argument("--output", help="Optional path to write JSON report")
	parser.add_argument("--strict", action="store_true", help="Exit 1 if any scenario check fails")
	args = parser.parse_args(argv)

	manifest = resolve_manifest_arg(
		manifest_path=args.manifest,
		seed_slot=args.seed_slot,
		clean_reinstall=bool(args.clean_reinstall),
	)
	report = run_suite(manifest=manifest)
	if args.output:
		Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")

	if args.json:
		print(json.dumps(report, indent=2))
	else:
		_print_table(report)

	if args.strict and report["summary"]["scenarios_fully_passed"] < report["summary"]["scenarios_total"]:
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())

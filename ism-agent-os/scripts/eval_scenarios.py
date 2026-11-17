#!/usr/bin/env python3
"""Phase 0-2 evaluation harness for realistic natural-language -> ISM CLI scenarios."""

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


def _make_check_s1_status(target_date: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
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


def _make_check_s2_tech_overview(target_date: str, elena_id: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
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


def _check_s3_slots_equipment(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
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


def _check_s4_decode_vin(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("model_3", d.get("model") == "Model 3"),
		("year_2023", str(d.get("model_year")) == "2023"),
	]


def _check_s4_vehicle_info(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("vin_present", bool(d)),
	]


def _check_s5_estimate(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	est = d.get("estimated_minutes") or d.get("total_minutes") or d.get("p80_minutes")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("duration_positive", isinstance(est, (int, float)) and est > 0),
	]


def _check_s6_unscheduled(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	d = data.get("data", {})
	services = d.get("services") or []
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("has_unscheduled_service", d.get("unscheduled_count", 0) >= 1 and len(services) >= 1),
	]


def _make_check_s7_bay_conflict(bay_name: str) -> Callable[[dict[str, Any], str, int], list[tuple[str, bool]]]:
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


def _check_s8_quote(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	ro_name = data.get("data", {}).get("name")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("ro_created", isinstance(ro_name, str) and ro_name.startswith("RO-")),
	]


def _check_s8_amend(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	total_services = data.get("data", {}).get("total_services")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("services_count_2", total_services == 2),
	]


def _check_s8_approval(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	approval_url = data.get("urls", {}).get("approval")
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("approval_url_present", isinstance(approval_url, str) and "token=" in approval_url),
	]


def _check_s9_batch(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
	results = data.get("results") or []
	all_ok = len(results) == 2 and all(r.get("ok") is True and r.get("result", {}).get("success") is True for r in results)
	return [
		("rc_zero", rc == 0),
		("success_true", data.get("success") is True),
		("batch_total_2_succeeded_2", data.get("total") == 2 and data.get("succeeded") == 2 and data.get("failed") == 0 and all_ok),
	]


def _check_s10_intake_slots(data: dict[str, Any], _raw: str, rc: int) -> list[tuple[str, bool]]:
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


def build_scenarios(manifest: Optional[dict[str, Any]] = None) -> list[ScenarioSpec]:
	"""Return the benchmark scenario suite interpolated against an eval_standard SimManifest."""
	m = manifest if manifest is not None else default_offline_eval_manifest()
	target_date = str(resolve_manifest_path(m, "metadata.target_date"))
	elena_id = str(resolve_manifest_path(m, "techs.elena"))
	main_lift = str(resolve_manifest_path(m, "bays.main_lift"))

	templates = [
		ScenarioSpec(
			id="S1_shop_status",
			nl_prompt="Check shop floor status for {{metadata.target_date}}.",
			expected_commands=["status"],
			steps=[
				StepSpec(
					command="status",
					args=["status", "--date", "{{metadata.target_date}}"],
					check_fn=_make_check_s1_status(target_date),
				)
			],
		),
		ScenarioSpec(
			id="S2_tech_overview_leave",
			nl_prompt="Show technician utilization and who is on leave on {{metadata.target_date}}.",
			expected_commands=["tech-overview"],
			steps=[
				StepSpec(
					command="tech-overview",
					args=["tech-overview", "--date", "{{metadata.target_date}}"],
					check_fn=_make_check_s2_tech_overview(target_date, elena_id),
				)
			],
		),
		ScenarioSpec(
			id="S3_slots_equipment",
			nl_prompt="Find 60-minute available slots on {{metadata.target_date}} with an Alignment Rack.",
			expected_commands=["slots"],
			steps=[
				StepSpec(
					command="slots",
					args=[
						"slots",
						"--date",
						"{{metadata.target_date}}",
						"--duration",
						"60",
						"--days",
						"1",
						"--equipment",
						"Alignment Rack",
					],
					check_fn=_check_s3_slots_equipment,
				)
			],
		),
		ScenarioSpec(
			id="S4_decode_and_vehicle_intel",
			nl_prompt="Decode VIN {{vehicles.veh_model3_liam}} and pull its vehicle intelligence.",
			expected_commands=["decode-vin", "vehicle-info"],
			steps=[
				StepSpec(
					command="decode-vin",
					args=["decode-vin", "--vin", "{{vehicles.veh_model3_liam}}"],
					check_fn=_check_s4_decode_vin,
				),
				StepSpec(
					command="vehicle-info",
					args=["vehicle-info", "--vehicle", "{{vehicles.veh_model3_liam}}"],
					check_fn=_check_s4_vehicle_info,
				),
			],
		),
		ScenarioSpec(
			id="S5_estimate_duration",
			nl_prompt="Estimate probabilistic shop duration for {{ros.conflict_ro}}.",
			expected_commands=["estimate"],
			steps=[
				StepSpec(
					command="estimate",
					args=["estimate", "--repair-order", "{{ros.conflict_ro}}"],
					check_fn=_check_s5_estimate,
				)
			],
		),
		ScenarioSpec(
			id="S6_unscheduled_queue",
			nl_prompt="List unscheduled services on {{ros.unscheduled_ro}} waiting for a bay.",
			expected_commands=["unscheduled"],
			steps=[
				StepSpec(
					command="unscheduled",
					args=["unscheduled", "--repair-order", "{{ros.unscheduled_ro}}"],
					check_fn=_check_s6_unscheduled,
				)
			],
		),
		ScenarioSpec(
			id="S7_bay_conflict_feedback",
			nl_prompt="Book {{ros.unscheduled_ro}} into {{bays.main_lift}} at {{metadata.target_date}} 09:00 (already occupied).",
			expected_commands=["book"],
			is_conflict_scenario=True,
			steps=[
				StepSpec(
					command="book",
					args=[
						"book",
						"--repair-order",
						"{{ros.unscheduled_ro}}",
						"--date",
						"{{metadata.target_date}}",
						"--time",
						"09:00",
						"--bay",
						"{{bays.main_lift}}",
						"--tech",
						"{{techs.devon}}",
						"--duration",
						"60",
					],
					expect_error=True,
					check_fn=_make_check_s7_bay_conflict(main_lift),
				)
			],
		),
		ScenarioSpec(
			id="S8_quote_amend_approval",
			nl_prompt="Create a draft quote for {{customers.retail_1}}, add a second service, and generate an approval link.",
			expected_commands=["quote", "amend-quote", "send-approval"],
			steps=[
				StepSpec(
					command="quote",
					args=[
						"quote",
						"--customer",
						"{{customers.retail_1}}",
						"--vehicle",
						"{{vehicles.veh_model3_liam}}",
						"--odometer-in",
						"48200",
						"--services-json",
						'[{"service_title": "Front Brake Inspection", "labor_hours": 0.5}]',
						"--notes",
						"Eval scenario S8 ({{customers.retail_1}})",
					],
					check_fn=_check_s8_quote,
				),
				StepSpec(
					command="amend-quote",
					args=[
						"amend-quote",
						"--repair-order",
						"{ro_name}",
						"--add-services-json",
						'[{"service_title": "Tire Rotation", "labor_hours": 0.5}]',
					],
					check_fn=_check_s8_amend,
				),
				StepSpec(
					command="send-approval",
					args=["send-approval", "--repair-order", "{ro_name}", "--medium", "link"],
					check_fn=_check_s8_approval,
				),
			],
		),
		ScenarioSpec(
			id="S9_batch_relative_date",
			nl_prompt="In one batch call, check shop status and 60-minute slots for {{metadata.target_date}}.",
			expected_commands=["batch"],
			steps=[
				StepSpec(
					command="batch",
					args=[
						"batch",
						json.dumps([
							["status", "--date", target_date],
							["slots", "--date", target_date, "--duration", "60", "--days", "1"],
						]),
					],
					check_fn=_check_s9_batch,
				)
			],
		),
		ScenarioSpec(
			id="S10_intake_slots_composite",
			nl_prompt=(
				"Intake a draft quote for {{customers.retail_1}} on {{vehicles.veh_model3_liam}} "
				"and find {{metadata.target_date}} >= 9am slots with {{bays.main_lift}} and Marcus."
			),
			expected_commands=["intake-slots"],
			steps=[
				StepSpec(
					command="intake-slots",
					args=[
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
					check_fn=_check_s10_intake_slots,
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
	"""Execute all scenarios against the supplied SimManifest and return structured benchmark metrics."""
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
	print("ISM AGENT OS — EVAL SCENARIOS BASELINE")
	print("=" * 84)
	for r in report["results"]:
		cf = "-" if r["conflict_feedback"] is None else ("PASS" if r["conflict_feedback"] else "FAIL")
		print(
			f"{r['id']:<28} tool={r['tool_choice_score']:.2f} "
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

"""Unit tests for SimManifest evaluation scripts and multi-seed sim_runner.py."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_TESTS_DIR = Path(__file__).resolve().parent
_AGENT_OS_ROOT = _TESTS_DIR.parent
if str(_AGENT_OS_ROOT) not in sys.path:
	sys.path.insert(0, str(_AGENT_OS_ROOT))

from scripts import demo_shop_flow, eval_nl_loop, eval_scenarios, sim_runner  # noqa: E402


def _custom_eval_manifest(target_date: str = "2027-02-18") -> dict[str, Any]:
	"""Return a distinct non-default eval_standard SimManifest for parameterization proofs."""
	m = sim_runner.default_offline_eval_manifest(target_date=target_date)
	m["techs"] = {
		"marcus": "EMP-CUSTOM-101",
		"elena": "EMP-CUSTOM-102",
		"devon": "EMP-CUSTOM-103",
		"noah": "EMP-CUSTOM-104",
	}
	m["customers"]["retail_1"] = "Liam Chen"
	m["customers"]["retail_3"] = "Owen MacLeod"
	m["ros"] = {
		"conflict_ro": "RO-CUST-9001",
		"hv_scheduled_ro": "RO-CUST-9002",
		"unscheduled_ro": "RO-CUST-9003",
		"unscheduled_secondary_ro": "RO-CUST-9004",
		"open_po_ro": "RO-CUST-9005",
		"zero_stock_ro": "RO-CUST-9006",
	}
	m["pos"] = {"eval_open_po": "PO-CUST-7001"}
	m["pes"] = {"eval_open_po_advance": "PE-CUST-7001"}
	m["schedule_entries"] = {
		"conflict_main_lift": "SE-CUST-5001",
		"hv_morning": "SE-CUST-5002",
	}
	m["leave_applications"] = {"elena_tomorrow": "LA-CUST-3001"}
	return m


class TestManifestHelpersAndHygiene(unittest.TestCase):
	def test_no_legacy_ensure_hacks_or_hardcoded_ids_in_eval_scripts(self) -> None:
		forbidden = [
			"_ensure_eval_technicians",
			"_ensure_service_bays",
			"_ensure_elena_on_leave",
			"_ensure_eval_parts_and_supplier",
			"_ensure_eval_customer_and_vehicle",
			"_ensure_s15_open_po",
			"_ensure_eval_seed",
			"RO-2026-00009",
			"RO-2026-00010",
			"RO-2026-00011",
			"RO-2026-00067",
			"HR-EMP-00004",
			"HR-EMP-00005",
			"HR-EMP-00006",
			"_EVAL_",
			"_TEST_ISM_Demo_Customer",
			"5YJ3E1EA1PF200101",
			"5YJ3E1EBXNF123456",
			"Prestiga-Biz",
		]
		scripts_dir = _AGENT_OS_ROOT / "scripts"
		for filename in ("eval_nl_loop.py", "eval_scenarios.py", "demo_shop_flow.py", "sim_runner.py"):
			text = (scripts_dir / filename).read_text(encoding="utf-8")
			for token in forbidden:
				self.assertNotIn(token, text, f"Forbidden legacy token '{token}' found in {filename}")

	def test_interpolate_manifest_supports_both_placeholder_syntaxes(self) -> None:
		manifest = _custom_eval_manifest("2027-03-01")
		tpl = "Book {{ros.unscheduled_ro}} into {manifest:bays.main_lift} on {{metadata.target_date}} with {manifest:techs.devon}."
		rendered = sim_runner.interpolate_manifest(tpl, manifest)
		self.assertEqual(
			rendered,
			"Book RO-CUST-9003 into Bay 1 - Main Lift on 2027-03-01 with EMP-CUSTOM-103.",
		)
		with self.assertRaises(KeyError):
			sim_runner.resolve_manifest_path(manifest, "ros.does_not_exist")

	def test_extract_json_from_noisy_slot_stdout(self) -> None:
		payload = {"profile": "eval_standard", "company": "Lightspeed EV", "metadata": {"target_date": "2027-03-01"}}
		noisy = (
			"[slot-lease] Leased slot-4 (wt4.localhost, port 8004)\n"
			"[slot-sync] Syncing files...\n"
			"sending incremental file list\n"
			"[slot-migrate] migrate=0 (schema fingerprint unchanged)\n"
			f"{json.dumps(payload, indent=2)}\n"
		)
		parsed = sim_runner.extract_json_from_stdout(noisy, required_keys=("profile", "company"))
		self.assertEqual(parsed, payload)

	def test_resolve_manifest_arg_from_file_and_slot(self) -> None:
		manifest = _custom_eval_manifest("2027-04-10")
		with tempfile.TemporaryDirectory() as tmp:
			mpath = Path(tmp) / "manifest.json"
			mpath.write_text(json.dumps(manifest), encoding="utf-8")
			loaded = sim_runner.resolve_manifest_arg(manifest_path=str(mpath))
			self.assertEqual(loaded["ros"]["conflict_ro"], "RO-CUST-9001")

		seen_cmds: list[list[str]] = []

		def _fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
			seen_cmds.append(cmd)
			return subprocess.CompletedProcess(
				cmd,
				0,
				stdout=f"[slot-lease] ok\n{json.dumps(manifest)}\n",
				stderr="",
			)

		seeded = sim_runner.resolve_manifest_arg(seed_slot=4, clean_reinstall=True, runner=_fake_runner)
		self.assertEqual(seeded["metadata"]["target_date"], "2027-04-10")
		self.assertIn("-Slot", seen_cmds[0])
		self.assertIn("4", seen_cmds[0])
		self.assertIn("-CleanReinstall", seen_cmds[0])
		self.assertIn("ism.testing.profiles.seed_eval_standard", seen_cmds[0])


class TestDemoAndEvalScenarios(unittest.TestCase):
	def test_demo_shop_flow_uses_manifest_handles(self) -> None:
		manifest = _custom_eval_manifest("2027-05-01")
		mock_client = MagicMock()
		mock_client.get_shop_floor_status.return_value = {"summary_card": "Floor OK"}
		mock_client.create_quote.return_value = {"data": {"name": "RO-NEW-101"}}
		mock_client.find_available_slots.return_value = {"summary_card": "Slots OK"}

		with patch("sys.stdout", new_callable=io.StringIO) as buf:
			demo_shop_flow.run_demo(mock_client, live=False, manifest=manifest)
		self.assertIn("2027-05-01", buf.getvalue())
		self.assertIn(manifest["vehicles"]["veh_model3_liam"], buf.getvalue())

		with patch("sys.stdout", new_callable=io.StringIO):
			demo_shop_flow.run_demo(mock_client, live=True, manifest=manifest)
		mock_client.create_quote.assert_called_once()
		kwargs = mock_client.create_quote.call_args.kwargs
		self.assertEqual(kwargs["customer_name"], "Liam Chen")
		self.assertEqual(kwargs["vehicle_identifier"], manifest["vehicles"]["veh_model3_liam"])
		mock_client.find_available_slots.assert_called_once_with(
			repair_order="RO-NEW-101",
			days_ahead=3,
			include_summary=True,
		)

	def test_eval_scenarios_build_and_run_suite_with_manifest(self) -> None:
		manifest = _custom_eval_manifest("2027-06-02")
		scenarios = eval_scenarios.build_scenarios(manifest=manifest)
		self.assertEqual(len(scenarios), 10)
		s7 = next(s for s in scenarios if s.id == "S7_bay_conflict_feedback")
		self.assertIn("RO-CUST-9003", s7.nl_prompt)
		self.assertIn("2027-06-02", s7.steps[0].args)
		self.assertIn("EMP-CUSTOM-103", s7.steps[0].args)

		def _fake_cli(args: list[str]) -> tuple[int, str, str, dict[str, Any] | None, float]:
			cmd = args[0]
			if cmd == "status":
				return 0, "", "", {
					"success": True,
					"data": {"date": "2027-06-02", "summary": {"active_bays": 4, "scheduled_appointments": 2}},
				}, 5.0
			if cmd == "tech-overview":
				return 0, "", "", {
					"success": True,
					"data": {"date": "2027-06-02", "technicians": [{"employee": "EMP-CUSTOM-102", "on_leave": True}]},
				}, 5.0
			if cmd == "slots":
				return 0, "", "", {
					"success": True,
					"data": {
						"slots": [{"start": "10:00"}],
						"duration_minutes": 60,
						"resources": {"bays": {"Bay 2 - Alignment & Suspension": ["Alignment Rack"]}},
					},
				}, 5.0
			if cmd == "decode-vin":
				return 0, "", "", {"success": True, "data": {"model": "Model 3", "model_year": "2023"}}, 5.0
			if cmd == "vehicle-info":
				return 0, "", "", {"success": True, "data": {"vin": manifest["vehicles"]["veh_model3_liam"]}}, 5.0
			if cmd == "estimate":
				return 0, "", "", {"success": True, "data": {"p80_minutes": 95}}, 5.0
			if cmd == "unscheduled":
				return 0, "", "", {"success": True, "data": {"unscheduled_count": 2, "services": [{"service_title": "Cabin Filter"}]}}, 5.0
			if cmd == "book":
				return 1, "", "conflict", {
					"success": False,
					"message": "Bay 1 - Main Lift is occupied at 09:00",
					"urls": {"conflicting_entry": "http://localhost:8004/app/schedule-entry/SE-CUST-5001"},
					"data": {"status": "conflict", "conflict": True, "alternative_slots": [{"start": "11:00"}]},
				}, 5.0
			if cmd == "quote":
				return 0, "", "", {"success": True, "data": {"name": "RO-NEW-888"}}, 5.0
			if cmd == "amend-quote":
				return 0, "", "", {"success": True, "data": {"total_services": 2}}, 5.0
			if cmd == "send-approval":
				return 0, "", "", {"success": True, "urls": {"approval": "https://shop.local/approve?token=abc"}}, 5.0
			if cmd == "batch":
				return 0, "", "", {
					"success": True,
					"total": 2,
					"succeeded": 2,
					"failed": 0,
					"results": [{"ok": True, "result": {"success": True}}, {"ok": True, "result": {"success": True}}],
				}, 5.0
			if cmd == "intake-slots":
				return 0, "", "", {
					"success": True,
					"data": {"repair_order": {"name": "RO-NEW-889"}, "slots": [{"start": "09:30"}]},
				}, 5.0
			return 1, "", f"unexpected {args}", None, 1.0

		report = eval_scenarios.run_suite(manifest=manifest, cli_runner=_fake_cli)
		self.assertEqual(report["summary"]["scenarios_total"], 10)
		self.assertEqual(report["summary"]["scenarios_fully_passed"], 10)
		self.assertEqual(report["summary"]["conflict_feedback_pass_rate"], 1.0)


class TestEvalNlLoop(unittest.TestCase):
	def test_get_scenarios_and_verify_db_use_manifest(self) -> None:
		manifest = _custom_eval_manifest("2027-07-04")
		scenarios = eval_nl_loop.get_scenarios(manifest=manifest)
		self.assertEqual(len(scenarios), 15)
		by_id = {s.id: s for s in scenarios}
		self.assertIn("2027-07-04", by_id["S01_shop_status_relative"].nl_prompt)
		self.assertIn("RO-CUST-9003", by_id["S06_direct_book_human_names"].nl_prompt)
		self.assertIn("PO-CUST-7001", by_id["S15_purchase_invoice_with_stock"].nl_prompt)

		mock_client = MagicMock()
		mock_client.get_daily_tech_overview.return_value = {
			"data": {"technicians": [{"employee": "EMP-CUSTOM-102", "on_leave": True}]}
		}
		s02_res = eval_nl_loop.verify_scenario_db(
			mock_client,
			by_id["S02_tech_util_leave"],
			{
				"id": "S02_tech_util_leave",
				"final_answer": "Elena Rostova is on leave on 2027-07-04; Marcus Vance is at 50%.",
				"commands_run": [{"cmd": "ism-agent-os tech-overview --date 2027-07-04", "exit_code": 0}],
			},
			manifest=manifest,
		)
		self.assertTrue(s02_res["db_verified"], s02_res)

		mock_client.list_documents.return_value = [
			{
				"name": "SE-CUST-9901",
				"scheduled_time": "13:00:00",
				"service_bay": "Bay 1 - Main Lift",
				"assigned_technician": "EMP-CUSTOM-103",
				"estimated_duration": 60,
				"status": "Scheduled",
			}
		]
		s06_res = eval_nl_loop.verify_scenario_db(
			mock_client,
			by_id["S06_direct_book_human_names"],
			{
				"id": "S06_direct_book_human_names",
				"final_answer": "Booked RO-CUST-9003 into SE-CUST-9901 at 13:00.",
				"commands_run": [{"cmd": "ism-agent-os book --repair-order RO-CUST-9003 --date 2027-07-04 --time 13:00", "exit_code": 0}],
			},
			manifest=manifest,
		)
		self.assertTrue(s06_res["db_verified"], s06_res)

	def test_main_round_and_scenarios_with_manifest_file(self) -> None:
		manifest = _custom_eval_manifest("2027-08-12")
		with tempfile.TemporaryDirectory() as tmp:
			tmp_path = Path(tmp)
			m_file = tmp_path / "eval_manifest.json"
			m_file.write_text(json.dumps(manifest), encoding="utf-8")

			raw_entries = [
				{
					"id": "S01_shop_status_relative",
					"commands_run": [{"cmd": "ism-agent-os status --date 2027-08-12", "exit_code": 0, "ms": 20}],
					"final_answer": "4 active bays and 2 scheduled appointments on 2027-08-12.",
				},
				{
					"id": "S05_unscheduled_queue",
					"commands_run": [{"cmd": "ism-agent-os unscheduled --repair-order RO-CUST-9003", "exit_code": 0, "ms": 15}],
					"final_answer": "Cabin HEPA & Carbon Air Filter Replacement is unscheduled.",
				},
			]
			raw_file = tmp_path / "raw.json"
			out_file = tmp_path / "round_1.json"
			raw_file.write_text(json.dumps(raw_entries), encoding="utf-8")

			with patch("sys.stdout", new_callable=io.StringIO) as out_scen:
				rc_scen = eval_nl_loop.main(["scenarios", "--manifest", str(m_file)])
			self.assertEqual(rc_scen, 0)
			scen_json = json.loads(out_scen.getvalue())
			self.assertEqual(len(scen_json), 15)
			self.assertIn("2027-08-12", scen_json[0]["nl_prompt"])

			with patch("sys.stdout", new_callable=io.StringIO) as out_score:
				rc_score = eval_nl_loop.main([
					"--round", "1",
					"--input", str(raw_file),
					"--manifest", str(m_file),
					"--skip-db",
					"--output", str(out_file),
				])
			self.assertEqual(rc_score, 0)
			summary = json.loads(out_score.getvalue())
			self.assertEqual(summary["kpis"]["round"], 1)
			self.assertEqual(summary["kpis"]["target_date"], "2027-08-12")
			self.assertEqual(summary["kpis"]["first_try_success"], 1.0)
			self.assertTrue(out_file.is_file())


class TestSimRunner(unittest.TestCase):
	def test_parse_seed_specs(self) -> None:
		self.assertEqual(sim_runner.parse_seed_specs(seeds=["3,1", "2,3"]), [3, 1, 2])
		self.assertEqual(sim_runner.parse_seed_specs(seed_range="10..13"), [10, 11, 12, 13])
		self.assertEqual(sim_runner.parse_seed_specs(), [1, 2, 3])
		with self.assertRaises(ValueError):
			sim_runner.parse_seed_specs(seed_range="5..2")

	def test_build_monte_carlo_probes_covers_all_edge_cases(self) -> None:
		mc_manifest = sim_runner.default_offline_monte_carlo_manifest(seed=7, target_date="2027-09-15")
		probes = sim_runner.build_monte_carlo_probes(mc_manifest)
		categories = {p.category for p in probes}
		self.assertEqual(
			categories,
			{"baseline", "leave_collision", "conflict", "stockout", "unscheduled", "over_tolerance"},
		)

	def test_dry_run_and_manifest_dir_cli(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			tmp_dir = Path(tmp)
			out_file = tmp_dir / "report.json"
			with patch("sys.stdout", new_callable=io.StringIO) as out:
				rc = sim_runner.main([
					"--seed-range", "1..3",
					"--dry-run",
					"--json",
					"--manifest-dir", str(tmp_dir),
					"--output", str(out_file),
					"--strict",
				])
			self.assertEqual(rc, 0)
			report = json.loads(out.getvalue())
			self.assertEqual(report["resilience_metrics"]["seeds_total"], 3)
			self.assertEqual(report["resilience_metrics"]["seeds_passed"], 3)
			self.assertTrue(report["resilience_metrics"]["overall_passed"])
			self.assertTrue((tmp_dir / "monte_carlo_seed_2.json").is_file())
			self.assertTrue(out_file.is_file())

	def test_live_slot_orchestration_sequence_and_scoring(self) -> None:
		slot_calls: list[list[str]] = []

		def _fake_slot_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
			slot_calls.append(cmd)
			joined = " ".join(cmd)
			if "seed_monte_carlo" in joined:
				seed_num = 1 if "[1]" in joined else 2
				m = sim_runner.default_offline_monte_carlo_manifest(seed=seed_num, target_date="2027-10-05")
				return subprocess.CompletedProcess(cmd, 0, stdout=f"[slot-lease] ok\n{json.dumps(m)}\n", stderr="")
			if "provision_agent_api_keys" in joined:
				creds = {"user": "manager@lightspeedev.ca", "api_key": "k123", "api_secret": "s456", "company": "Lightspeed EV"}
				return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(creds), stderr="")
			return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

		cli_envs: list[dict[str, str] | None] = []

		def _fake_cli_runner(args: list[str], env: dict[str, str] | None) -> tuple[int, str, str, dict[str, Any] | None, float]:
			cli_envs.append(env)
			cmd = args[0]
			if cmd == "status":
				return 0, "", "", {
					"success": True,
					"data": {
						"date": "2027-10-05",
						"appointments": [{"service_bay": "Bay 1 - Main Lift", "scheduled_time": "09:00:00", "assigned_technician": "EMP-SIM-001"}],
					},
				}, 4.0
			if cmd == "tech-overview":
				return 0, "", "", {
					"success": True,
					"data": {"technicians": [{"employee": "EMP-SIM-002", "on_leave": True}]},
				}, 4.0
			if cmd == "slots":
				# Leave-tech query returns 0 slots
				return 0, "", "", {"success": True, "data": {"slots": []}}, 4.0
			if cmd == "estimate":
				return 0, "", "", {"success": True, "data": {"p80_minutes": 75}}, 4.0
			if cmd == "book":
				return 1, "", "conflict", {
					"success": False,
					"message": "Occupied conflict on Bay 1 - Main Lift",
					"data": {"status": "conflict", "conflict": True},
				}, 4.0
			if cmd == "check-stock":
				return 0, "", "", {"success": True, "data": {"all_available": False, "deficit_count": 2}}, 4.0
			if cmd == "action-items":
				return 0, "", "", {"success": True, "data": {"count": 1, "items": [{"name": "SAI-1"}]}}, 4.0
			if cmd == "batch":
				inner = json.loads(args[1])
				if inner[0][0] == "status":
					return 0, "", "", {
						"success": True,
						"results": [
							{"ok": True, "result": {"success": True, "data": {"date": "2027-10-05", "summary": {"active_bays": 4}}}},
							{"ok": True, "result": {"success": True, "data": {"technicians": [{"employee": "EMP-SIM-001"}]}}},
						],
					}, 5.0
				return 0, "", "", {
					"success": True,
					"results": [
						{"ok": True, "result": {"success": True, "data": {"unscheduled_count": 1, "services": [{"service_title": "Brake Flush"}]}}},
						{"ok": True, "result": {"success": True, "data": {"slots": [{"start": "11:00"}]}}},
					],
				}, 5.0
			return 1, "", "unknown", None, 1.0

		report = sim_runner.run_batch_simulation(
			seeds=[1, 2],
			slot=4,
			clean_reinstall=True,
			slot_runner=_fake_slot_runner,
			cli_runner=_fake_cli_runner,
		)
		metrics = report["resilience_metrics"]
		self.assertEqual(metrics["seeds_total"], 2)
		self.assertEqual(metrics["seeds_passed"], 2)
		self.assertTrue(metrics["overall_passed"])
		self.assertEqual(metrics["by_category"]["stockout"]["pass_rate"], 1.0)
		self.assertEqual(metrics["by_category"]["conflict"]["pass_rate"], 1.0)

		# Verify CleanReinstall only on seed 1, KeepDb on seed 2, and serve start/stop bracket each seed
		joined_calls = [" ".join(c) for c in slot_calls]
		self.assertIn("-CleanReinstall", joined_calls[0])
		self.assertIn("seed_monte_carlo", joined_calls[0])
		self.assertIn("provision_agent_api_keys", joined_calls[1])
		self.assertIn("-Serve start", joined_calls[2])
		self.assertIn("-Serve stop", joined_calls[3])
		self.assertIn("-KeepDb", joined_calls[4])
		self.assertIn("seed_monte_carlo", joined_calls[4])
		self.assertIn("-Serve start", joined_calls[5])
		self.assertIn("-Serve stop", joined_calls[6])

		self.assertTrue(all(e and e["FRAPPE_URL"] == "http://localhost:8004" and e["FRAPPE_API_KEY"] == "k123" for e in cli_envs))

	def test_strict_failure_exit_code_on_probe_failure(self) -> None:
		mc_manifest = sim_runner.default_offline_monte_carlo_manifest(seed=9, target_date="2027-11-01")
		with tempfile.TemporaryDirectory() as tmp:
			tmp_dir = Path(tmp)
			(tmp_dir / "monte_carlo_seed_9.json").write_text(json.dumps(mc_manifest), encoding="utf-8")

			def _failing_cli(args: list[str], _env: dict[str, str] | None) -> tuple[int, str, str, dict[str, Any] | None, float]:
				if args[0] == "check-stock":
					# Stockout probe fails because all_available=True (deficit not detected)
					return 0, "", "", {"success": True, "data": {"all_available": True, "deficit_count": 0}}, 2.0
				return 0, "", "", {"success": True, "data": {"date": "2027-11-01", "summary": {"active_bays": 4}, "technicians": [{"employee": "EMP-SIM-001"}], "slots": [], "p80_minutes": 60, "count": 1, "items": [{"name": "SAI-1"}]}, "results": [{"ok": True, "result": {"success": True, "data": {"date": "2027-11-01", "summary": {"active_bays": 4}, "unscheduled_count": 1, "services": [{"service_title": "S"}], "slots": [{"start": "10:00"}]}}}, {"ok": True, "result": {"success": True, "data": {"technicians": [{"employee": "E1"}], "slots": [{"start": "10:00"}]}}}]}, 2.0

			with patch("scripts.sim_runner._run_ism_cli", side_effect=_failing_cli), \
				patch("sys.stdout", new_callable=io.StringIO):
				rc = sim_runner.main(["--seeds", "9", "--manifest-dir", str(tmp_dir), "--strict"])
			self.assertEqual(rc, 1)


if __name__ == "__main__":
	unittest.main()

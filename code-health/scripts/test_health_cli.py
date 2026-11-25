#!/usr/bin/env python3
"""Unit tests for code-health/scripts/health_cli.py."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIR))

import health_cli  # noqa: E402


class TestHealthCli(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.root = Path(self._tmp.name)
		self.state = self.root / "code-health"
		(self.state / "runs").mkdir(parents=True)
		(self.root / "zone_a").mkdir()
		(self.root / "zone_b").mkdir()
		(self.root / "zone_os").mkdir()

		self.zones_data = {
			"schema_version": 1,
			"zones": [
				{
					"id": "frappe-a",
					"repo_area": "ism-frappe",
					"description": "Zone A",
					"criticality": "critical",
					"requires_bench_slot": True,
					"cooldown_days": 5,
					"paths": ["zone_a"],
					"verification_cmd": "test-in-slot.ps1 -Slot {slot} -Module a",
					"last_audited_at": None,
					"last_audited_run": None,
					"last_audited_commit": None,
					"audit_count": 0,
				},
				{
					"id": "frappe-b",
					"repo_area": "ism-frappe",
					"description": "Zone B",
					"criticality": "high",
					"requires_bench_slot": True,
					"cooldown_days": 7,
					"paths": ["zone_b"],
					"verification_cmd": "test-in-slot.ps1 -Slot {slot} -Module b",
					"last_audited_at": None,
					"last_audited_run": None,
					"last_audited_commit": None,
					"audit_count": 0,
				},
				{
					"id": "agent-os-a",
					"repo_area": "ism-agent-os",
					"description": "Agent OS",
					"criticality": "medium",
					"requires_bench_slot": False,
					"cooldown_days": 7,
					"paths": ["zone_os"],
					"verification_cmd": "python -m unittest discover",
					"last_audited_at": None,
					"last_audited_run": None,
					"last_audited_commit": None,
					"audit_count": 0,
				},
			],
		}
		health_cli.save_json(self.state / "zones.json", self.zones_data)
		health_cli.save_json(self.state / "backlog.json", {"schema_version": 1, "next_id": 1, "items": []})
		(self.state / "metrics_history.jsonl").write_text("", encoding="utf-8")
		(self.state / "runs" / "_TEMPLATE.md").write_text(
			"# Run {{RUN_ID}} ({{PRESET}}) on {{BRANCH}} @ {{BASE_SHA}} {{DATE}} {{AUDIT_ZONES}} {{FIX_ZONES}}\n",
			encoding="utf-8",
		)

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def _run_cli(self, *cli_args: str) -> tuple[int, str]:
		buf = io.StringIO()
		with redirect_stdout(buf):
			rc = health_cli.main(["--root", str(self.root), "--state-dir", str(self.state), *cli_args])
		return rc, buf.getvalue()

	def test_ast_detectors_catch_real_signals_and_skip_safe_patterns(self) -> None:
		sample = self.root / "zone_a" / "service.py"
		long_body = "\n".join(f"    x_{i} = {i}" for i in range(125))
		sample.write_text(
			f"""
import frappe

@frappe.whitelist(allow_guest=True)
def public_endpoint(name):
    frappe.db.sql(f"SELECT * FROM tabUser WHERE name = '{{name}}'")
    frappe.db.sql("SELECT * FROM tabUser WHERE name = %s", (name,))
    rows = frappe.get_all("Repair Order", filters={{"status": "Open"}})
    bounded = frappe.get_all("Repair Order", limit_page_length=20)
    for row in rows:
        doc = frappe.get_doc("Repair Order", row.name)
        doc.save(ignore_permissions=True)
    try:
        do_work()
    except Exception:
        pass
    # FIXME: clean up legacy path

def huge_fn():
{long_body}
    return 1
""",
			encoding="utf-8",
		)

		# Test files should not emit non-test static noise
		test_sample = self.root / "zone_a" / "test_service.py"
		test_sample.write_text(
			"""
import frappe
def test_ok():
    for i in range(3):
        frappe.get_doc("Repair Order", i)
    try:
        pass
    except Exception:
        pass
""",
			encoding="utf-8",
		)

		result = health_cli.run_scan(self.root, self.zones_data)
		rules = {f["rule"] for f in result["findings"]}
		files_flagged = {f["files"][0] for f in result["findings"]}

		self.assertEqual(files_flagged, {"zone_a/service.py"})
		self.assertIn("allow-guest-inventory", rules)
		self.assertIn("sqli-candidate", rules)
		self.assertIn("n-plus-one-candidate", rules)
		self.assertIn("unbounded-get-all", rules)
		self.assertIn("ignore-permissions-audit", rules)
		self.assertIn("swallowed-exception", rules)
		self.assertIn("long-function", rules)
		self.assertIn("todo-marker", rules)

		# Verify only 1 sqli-candidate (parameterized query is NOT flagged)
		sqli = [f for f in result["findings"] if f["rule"] == "sqli-candidate"]
		self.assertEqual(len(sqli), 1)

	def test_fingerprint_dedup_preserves_wontfix_across_rescans(self) -> None:
		sample = self.root / "zone_a" / "api.py"
		sample.write_text(
			"""
import frappe
def handler():
    try:
        return frappe.get_all("Item")
    except Exception:
        pass
""",
			encoding="utf-8",
		)

		rc, _ = self._run_cli("scan", "--seed", "--record-metrics", "--run", "run-001")
		self.assertEqual(rc, 0)
		backlog = health_cli.load_json(self.state / "backlog.json")
		initial_count = len(backlog["items"])
		self.assertEqual(initial_count, 2)

		first_id = backlog["items"][0]["id"]
		rc, _ = self._run_cli("update-issue", "--id", first_id, "--status", "wontfix", "--note", "Expected")
		self.assertEqual(rc, 0)

		# Second scan --seed must NOT duplicate or reopen the wontfix item
		rc, out = self._run_cli("scan", "--seed", "--json")
		self.assertEqual(rc, 0)
		data = json.loads(out)
		self.assertEqual(data["seed_summary"]["added"], [])
		self.assertEqual(len(data["seed_summary"]["skipped_duplicate"]), 2)

		backlog_after = health_cli.load_json(self.state / "backlog.json")
		self.assertEqual(len(backlog_after["items"]), initial_count)
		reloaded = next(it for it in backlog_after["items"] if it["id"] == first_id)
		self.assertEqual(reloaded["status"], "wontfix")

	def test_next_batch_selects_disjoint_zones_and_pairs_agent_os(self) -> None:
		items = [
			{
				"title": "Fix N+1 in zone A",
				"category": "performance",
				"severity": "high",
				"zone": "frappe-a",
				"files": ["zone_a/a.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "medium",
			},
			{
				"title": "Fix N+1 in zone B",
				"category": "performance",
				"severity": "medium",
				"zone": "frappe-b",
				"files": ["zone_b/b.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			{
				"title": "Harden agent-os tests",
				"category": "test-coverage",
				"severity": "medium",
				"zone": "agent-os-a",
				"files": ["zone_os/c.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
		]
		payload_file = self.root / "incoming.json"
		payload_file.write_text(json.dumps(items), encoding="utf-8")
		rc, _ = self._run_cli("add-issue", "--from-json", str(payload_file), "--run", "run-001")
		self.assertEqual(rc, 0)

		rc, out = self._run_cli("next-batch", "--preset", "standard", "--json")
		self.assertEqual(rc, 0)
		plan = json.loads(out)

		fix_zones = [t["zone"] for t in plan["fix_tracks"]]
		self.assertEqual(len(fix_zones), 2)
		self.assertEqual(len(set(fix_zones)), 2)
		# High severity frappe-a first, paired with equal-severity agent-os-a (saves bench slot)
		self.assertEqual(fix_zones, ["frappe-a", "agent-os-a"])
		self.assertEqual(plan["fix_tracks"][0]["bench_slot"], 1)
		self.assertIsNone(plan["fix_tracks"][1]["bench_slot"])
		# frappe-a uses neon-medium -> cross-family verifier must be carbon-high
		self.assertEqual(plan["fix_tracks"][0]["recommended_model"], "gemini-4-neon-medium")
		self.assertEqual(plan["fix_tracks"][1]["recommended_model"], "gemini-4-hydrogen-medium")
		self.assertEqual(plan["verifier_model"], "gemini-4-carbon-high")

	def test_mark_audited_and_init_run_round_trip(self) -> None:
		rc, _ = self._run_cli("mark-audited", "--zone", "frappe-a", "--run", "run-001", "--commit", "abc1234")
		self.assertEqual(rc, 0)
		zones_after = health_cli.load_json(self.state / "zones.json")
		za = next(z for z in zones_after["zones"] if z["id"] == "frappe-a")
		self.assertEqual(za["audit_count"], 1)
		self.assertEqual(za["last_audited_run"], "run-001")
		self.assertEqual(za["last_audited_commit"], "abc1234")

		# After auditing frappe-a, frappe-b (never audited) must rank ahead of frappe-a
		ranked = health_cli.score_audit_zones(self.root, zones_after, health_cli.load_json(self.state / "backlog.json"))
		self.assertEqual(ranked[0]["zone"], "frappe-b")

		rc, out = self._run_cli("init-run", "--preset", "eco", "--audit-zones", "frappe-b", "--json")
		self.assertEqual(rc, 0)
		created = json.loads(out)
		self.assertEqual(created["run_id"], "run-001")
		self.assertTrue((self.root / created["path"]).exists())

		# Second init-run increments to run-002
		rc, out2 = self._run_cli("init-run", "--preset", "eco", "--json")
		self.assertEqual(rc, 0)
		self.assertEqual(json.loads(out2)["run_id"], "run-002")

	def test_real_repo_zones_are_strictly_disjoint_and_valid(self) -> None:
		repo_root = health_cli.default_repo_root()
		state_dir = health_cli.default_state_dir(repo_root)
		zones_data = health_cli.load_json(state_dir / "zones.json")
		check = health_cli.validate_zones_disjoint(repo_root, zones_data)
		self.assertTrue(check["ok"], f"Zone validation failed: {check}")
		self.assertEqual(check["overlaps"], {})
		self.assertEqual(check["missing_paths"], [])
		self.assertGreater(check["total_mapped_files"], 250)

	def _add_extra_frappe_zones(self) -> None:
		"""Expand synthetic zones fixture to 4 Frappe zones + 1 Agent OS zone to test 5-slot saturation."""
		(self.root / "zone_c").mkdir(exist_ok=True)
		(self.root / "zone_d").mkdir(exist_ok=True)
		zones_data = health_cli.load_json(self.state / "zones.json")
		zones_data["zones"].extend(
			[
				{
					"id": "frappe-c",
					"repo_area": "ism-frappe",
					"description": "Zone C",
					"criticality": "high",
					"requires_bench_slot": True,
					"cooldown_days": 7,
					"paths": ["zone_c"],
					"verification_cmd": "powershell -ExecutionPolicy Bypass -File scripts\\test-in-slot.ps1 -Slot {slot} -Module ism.tests.test_c,ism.tests.test_shared",
					"last_audited_at": None,
					"last_audited_run": None,
					"last_audited_commit": None,
					"audit_count": 0,
				},
				{
					"id": "frappe-d",
					"repo_area": "ism-frappe",
					"description": "Zone D",
					"criticality": "medium",
					"requires_bench_slot": True,
					"cooldown_days": 10,
					"paths": ["zone_d"],
					"verification_cmd": "powershell -ExecutionPolicy Bypass -File scripts\\test-in-slot.ps1 -Slot {slot} -Module ism.tests.test_d,ism.tests.test_shared",
					"last_audited_at": None,
					"last_audited_run": None,
					"last_audited_commit": None,
					"audit_count": 0,
				},
			]
		)
		health_cli.save_json(self.state / "zones.json", zones_data)

	def test_extended_and_overnight_multi_wave_slot_saturation_and_deep_tracks(self) -> None:
		self._add_extra_frappe_zones()
		items = [
			# Critical unsafe item for Architect RFC track
			{
				"title": "Critical auth architecture decision",
				"category": "security",
				"severity": "critical",
				"zone": "frappe-a",
				"files": ["zone_a/auth.py"],
				"safe_for_auto_fix": False,
				"estimated_effort": "large",
			},
			# Semantic auto-fixable items across 4 Frappe zones + 1 Agent OS zone
			{
				"title": "High semantic bug A",
				"category": "correctness",
				"severity": "high",
				"zone": "frappe-a",
				"files": ["zone_a/a.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			{
				"title": "High semantic bug B",
				"category": "security",
				"severity": "high",
				"zone": "frappe-b",
				"files": ["zone_b/b.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "medium",
			},
			{
				"title": "High semantic bug C",
				"category": "correctness",
				"severity": "high",
				"zone": "frappe-c",
				"files": ["zone_c/c.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			{
				"title": "Medium semantic bug D",
				"category": "correctness",
				"severity": "medium",
				"zone": "frappe-d",
				"files": ["zone_d/d.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			{
				"title": "Agent OS CLI fix",
				"category": "correctness",
				"severity": "medium",
				"zone": "agent-os-a",
				"files": ["zone_os/cli.py"],
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			# Static-scanner items in frappe-d for Wave 2 sweeper
			{
				"title": "Static unbounded get_all 1",
				"rule": "unbounded-get-all",
				"category": "performance",
				"severity": "medium",
				"zone": "frappe-d",
				"files": ["zone_d/q1.py"],
				"source": "static-scanner",
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			{
				"title": "Static unbounded get_all 2",
				"rule": "unbounded-get-all",
				"category": "performance",
				"severity": "medium",
				"zone": "frappe-d",
				"files": ["zone_d/q2.py"],
				"source": "static-scanner",
				"safe_for_auto_fix": True,
				"estimated_effort": "small",
			},
			# File-size critical monolith in frappe-d for Wave 3 refactor
			{
				"title": "Oversized file in zone D",
				"rule": "file-size-critical",
				"category": "file-size",
				"severity": "high",
				"zone": "frappe-d",
				"files": ["zone_d/monolith.py"],
				"safe_for_auto_fix": False,
				"estimated_effort": "large",
			},
		]
		payload_file = self.root / "multi.json"
		payload_file.write_text(json.dumps(items), encoding="utf-8")
		rc, _ = self._run_cli("add-issue", "--from-json", str(payload_file), "--run", "run-001")
		self.assertEqual(rc, 0)

		# Wave 1 of overnight: 3 Frappe slots (1..3) + 1 agent-os (None), 2 verifiers (4, 5), 2 auditors, 1 RFC track
		rc, out_w1 = self._run_cli("next-batch", "--preset", "overnight", "--wave", "1", "--json")
		self.assertEqual(rc, 0)
		w1 = json.loads(out_w1)
		self.assertEqual(w1["total_waves"], 4)
		self.assertTrue(w1["final_integration_gate"])
		self.assertEqual(len(w1["auditor_assignments"]), 2)
		self.assertEqual(len(w1["fix_tracks"]), 4)
		frappe_slots = sorted(t["bench_slot"] for t in w1["fix_tracks"] if t["bench_slot"] is not None)
		self.assertEqual(frappe_slots, [1, 2, 3])
		self.assertEqual(w1["verifier_bench_slots"], [4, 5])
		self.assertEqual(len(w1["verifiers"]), 2)
		self.assertEqual(len(w1["deep_tracks"]), 1)
		self.assertEqual(w1["deep_tracks"][0]["kind"], "rfc")
		rfc_id = w1["deep_tracks"][0]["items"][0]["id"]

		# Once RFC file exists in code-health/rfcs/, architect track skips it
		rfcs_dir = self.state / "rfcs"
		rfcs_dir.mkdir(exist_ok=True)
		(rfcs_dir / f"{rfc_id}.md").write_text("# RFC\n", encoding="utf-8")
		rc, out_ext = self._run_cli("next-batch", "--preset", "extended", "--wave", "1", "--json")
		self.assertEqual(rc, 0)
		ext = json.loads(out_ext)
		self.assertEqual(ext["total_waves"], 2)
		self.assertEqual(ext["deep_tracks"], [])

		# Wave 2 of overnight schedules static-sweeper track within 3-Frappe-slot cap
		rc, out_w2 = self._run_cli("next-batch", "--preset", "overnight", "--wave", "2", "--json")
		self.assertEqual(rc, 0)
		w2 = json.loads(out_w2)
		kinds_w2 = {t["track_kind"] for t in w2["fix_tracks"]}
		self.assertIn("static-sweeper", kinds_w2)
		self.assertEqual(sorted(t["bench_slot"] for t in w2["fix_tracks"] if t["bench_slot"] is not None), [1, 2, 3])

		# Wave 3 of overnight schedules POLICY.md §4 file-size refactor with 800-LOC cap
		rc, out_w3 = self._run_cli("next-batch", "--preset", "overnight", "--wave", "3", "--json")
		self.assertEqual(rc, 0)
		w3 = json.loads(out_w3)
		ref_tracks = [t for t in w3["fix_tracks"] if t["track_kind"] == "refactor"]
		self.assertEqual(len(ref_tracks), 1)
		self.assertEqual(ref_tracks[0]["zone"], "frappe-d")
		self.assertEqual(ref_tracks[0]["max_loc"], 800)
		self.assertEqual(len({t["zone"] for t in w3["fix_tracks"]}), len(w3["fix_tracks"]))

		# --exclude-zones drops excluded zones from both audit and fix tracks
		rc, out_ex = self._run_cli(
			"next-batch", "--preset", "extended", "--wave", "1", "--exclude-zones", "frappe-a,frappe-b", "--json"
		)
		self.assertEqual(rc, 0)
		ex = json.loads(out_ex)
		for z in [t["zone"] for t in ex["fix_tracks"]] + [a["zone"] for a in ex["audit_tracks"]]:
			self.assertNotIn(z, {"frappe-a", "frappe-b"})

	def test_integration_gate_deduplicates_modules_and_adds_cross_track_seams(self) -> None:
		self._add_extra_frappe_zones()
		rc, out = self._run_cli(
			"integration-gate", "--zones", "frappe-c,frappe-d,agent-os-a", "--slot", "4", "--json"
		)
		self.assertEqual(rc, 0)
		gate = json.loads(out)
		self.assertEqual(gate["bench_slot"], 4)
		self.assertEqual(
			gate["frappe_modules"],
			["ism.tests.test_c", "ism.tests.test_shared", "ism.tests.test_d", "ism.tests.test_cross_track_seams"],
		)
		self.assertIn("-Slot 4", gate["frappe_cmd"])
		self.assertIn("ism.tests.test_cross_track_seams", gate["frappe_cmd"])
		self.assertIsNotNone(gate["agent_os_cmd"])

		rc_bad, _ = self._run_cli("integration-gate", "--zones", "nonexistent-zone", "--json")
		self.assertEqual(rc_bad, 1)


if __name__ == "__main__":
	unittest.main()

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


if __name__ == "__main__":
	unittest.main()

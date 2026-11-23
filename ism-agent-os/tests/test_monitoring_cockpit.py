"""Unit and localhost HTTP tests for ISM monitoring cockpit, triage queue, and CLI."""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import cli
from tools.cli_parsers import _build_parser
from tools.cli_validation import (
	READ_ONLY_COMMANDS,
	WRITE_COMMAND_META,
	build_pre_dispatch_summary,
	build_stage_form_spec,
	validate_write_command_args,
)
from tools.monitoring import build_cockpit_view
from tools.stage_server import start_stage_server


def _base_snapshot() -> dict:
	return {
		"success": True,
		"urls": {
			"desk_root": "http://localhost:8000",
			"calendar": "http://localhost:8000/app/schedule-entry/view/calendar/default",
			"schedule_entries": "http://localhost:8000/app/schedule-entry",
			"repair_orders": "http://localhost:8000/app/repair-order",
			"pending_authorization": "http://localhost:8000/app/repair-order?status=Pending+Authorization",
			"work_completed": "http://localhost:8000/app/repair-order?status=Work+Completed",
			"pnl_report": "http://localhost:8000/app/query-report/Profit%20and%20Loss%20Statement",
			"action_items": "http://localhost:8000/app/shop-action-item",
			"health_snapshots": "http://localhost:8000/app/shop-health-snapshot",
		},
		"data": {
			"date": "2026-11-17",
			"next_open_date": "2026-11-18",
			"server_now": "2026-11-17 10:00:00",
			"settings": {
				"operating_hours_start": "08:00:00",
				"operating_hours_end": "17:00:00",
				"break_start": "12:00:00",
				"break_end": "12:30:00",
				"default_slot_interval": 30,
			},
			"bays": [
				{
					"name": "BAY-1",
					"bay_name": "Bay 1",
					"equipment_tags": ["Lift"],
					"desk_url": "http://localhost:8000/app/service-bay/BAY-1",
				},
				{
					"name": "BAY-2",
					"bay_name": "Bay 2",
					"equipment_tags": ["Lift", "Alignment Rack"],
					"desk_url": "http://localhost:8000/app/service-bay/BAY-2",
				},
			],
			"technician_overview": {
				"date": "2026-11-17",
				"technicians": [
					{"employee": "EMP-1", "employee_name": "Tech One", "on_leave": False, "utilization_pct": 50.0},
					{"employee": "EMP-2", "employee_name": "Tech Two", "on_leave": False, "utilization_pct": 25.0},
				],
			},
			"next_open_technicians": [
				{"employee": "EMP-1", "employee_name": "Tech One", "on_leave": False},
				{"employee": "EMP-2", "employee_name": "Tech Two", "on_leave": False},
			],
			"schedule_entries": [],
			"active_repair_orders": [],
			"action_items": [],
			"health_snapshots": {},
		},
	}


class TestMonitoringTriageAndGauges(unittest.TestCase):
	def test_all_derived_rules_trigger_and_autoclear_on_state_change(self) -> None:
		snap = _base_snapshot()
		snap["data"]["schedule_entries"] = [
			{
				"name": "SE-0001",
				"entry_type": "Repair",
				"status": "In Progress",
				"scheduled_date": "2026-11-17",
				"scheduled_time": "08:30:00",
				"estimated_duration": 60,
				"repair_order": "RO-DEAD",
				"customer": "Cust D",
				"vehicle": "Model Y",
				"service_bay": "BAY-1",
				"assigned_technician": "EMP-1",
				"p80_duration_minutes": 90,
				"desk_url": "http://localhost:8000/app/schedule-entry/SE-0001",
			}
		]
		snap["data"]["active_repair_orders"] = [
			{
				"name": "RO-DRAFT",
				"customer": "Cust A",
				"vehicle": "Model 3",
				"status": "Draft Estimate",
				"approval_status": "Estimating",
				"creation": "2026-11-17 09:40:00",
				"modified": "2026-11-17 09:45:00",
				"total_services": 2,
				"parts_summary": {},
				"desk_url": "http://localhost:8000/app/repair-order/RO-DRAFT",
			},
			{
				"name": "RO-COLD",
				"customer": "Cust B",
				"vehicle": "Model S",
				"status": "Pending Authorization",
				"approval_status": "Pending Approval",
				"approval_sent_at": "2026-11-17 07:00:00",
				"grand_total": 850.0,
				"parts_summary": {},
				"desk_url": "http://localhost:8000/app/repair-order/RO-COLD",
			},
			{
				"name": "RO-PARTS",
				"customer": "Cust C",
				"vehicle": "Model X",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"authorized_at": "2026-11-17 08:00:00",
				"vehicle_location": "In Customer Lot",
				"unscheduled_approved_services": 1,
				"has_active_or_future_booking": True,
				"parts_summary": {
					"has_stock_deficit": True,
					"has_uncovered_deficit": True,
					"all_parts_ready": False,
					"deficit_items": [{"item_code": "CTRL-ARM-LH", "uncovered_qty": 1.0}],
				},
				"desk_url": "http://localhost:8000/app/repair-order/RO-PARTS",
			},
			{
				"name": "RO-READY",
				"customer": "Cust E",
				"vehicle": "Model 3",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"modified": "2026-11-17 09:10:00",
				"manual_parts_hold": 0,
				"unscheduled_approved_services": 1,
				"has_active_or_future_booking": False,
				"parts_summary": {
					"has_stock_deficit": False,
					"has_uncovered_deficit": False,
					"all_parts_ready": True,
					"deficit_items": [],
				},
				"desk_url": "http://localhost:8000/app/repair-order/RO-READY",
			},
			{
				"name": "RO-DEAD",
				"customer": "Cust D",
				"vehicle": "Model Y",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"vehicle_location": "In Bay / Being Serviced",
				"picked_up_at": "2026-11-17 08:30:00",
				"modified": "2026-11-17 09:05:00",
				"parts_summary": {"has_stock_deficit": True, "has_uncovered_deficit": False, "all_parts_ready": False},
				"desk_url": "http://localhost:8000/app/repair-order/RO-DEAD",
			},
			{
				"name": "RO-WIP",
				"customer": "Cust F",
				"vehicle": "Model 3",
				"status": "Work Completed",
				"approval_status": "Approved",
				"modified": "2026-11-17 09:30:00",
				"grand_total": 420.0,
				"outstanding_amount": 420.0,
				"vehicle_released": 0,
				"parts_summary": {"all_parts_ready": True},
				"desk_url": "http://localhost:8000/app/repair-order/RO-WIP",
			},
		]

		view1 = build_cockpit_view(snap)
		keys1 = {it["dedup_key"] for it in view1["action_queue"]}
		self.assertEqual(
			keys1,
			{
				"quote_needed:RO-DRAFT",
				"quote_cold:RO-COLD",
				"parts_deficit:RO-PARTS",
				"parts_ready:RO-READY",
				"unscheduled_ro:RO-READY",
				"dead_lift:RO-DEAD",
				"checkout_ready:RO-WIP",
			},
		)
		# Dead lift gauge + spillover gauge (RO-DEAD SE-0001 ran 08:30-09:30, server_now is 10:00)
		gauges1 = {g["key"]: g for g in view1["gauges"]}
		self.assertEqual(gauges1["dead_lift"]["value"], "1 blocked")
		self.assertEqual(gauges1["dead_lift"]["grade"], "Critical")
		self.assertEqual(gauges1["spillover_risk"]["value"], "1 at risk")
		self.assertEqual(gauges1["pnl_health"]["grade"], "Stale")

		# Now resolve each item in Frappe state and refresh
		snap2 = copy.deepcopy(snap)
		snap2["data"]["schedule_entries"][0]["status"] = "Completed"
		snap2["data"]["active_repair_orders"] = [
			{
				"name": "RO-DRAFT",
				"status": "Pending Authorization",
				"approval_status": "Pending Approval",
				"approval_sent_at": "2026-11-17 09:55:00",
				"parts_summary": {},
			},
			{
				"name": "RO-PARTS",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"has_active_or_future_booking": True,
				"parts_summary": {
					"has_stock_deficit": True,
					"has_uncovered_deficit": False,
					"all_parts_ready": False,
					"deficit_items": [{"item_code": "CTRL-ARM-LH", "uncovered_qty": 0.0, "open_po_qty": 1.0}],
				},
			},
			{
				"name": "RO-READY",
				"status": "Authorized - In Progress",
				"approval_status": "Approved",
				"has_active_or_future_booking": True,
				"unscheduled_approved_services": 0,
				"parts_summary": {"all_parts_ready": True},
			},
			{
				"name": "RO-DEAD",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"vehicle_location": "In Customer Lot",
				"parts_summary": {"has_stock_deficit": True, "has_uncovered_deficit": False},
			},
		]
		view2 = build_cockpit_view(snap2)
		self.assertEqual(view2["action_queue"], [])
		gauges2 = {g["key"]: g for g in view2["gauges"]}
		self.assertEqual(gauges2["dead_lift"]["value"], "0 blocked")

	def test_snooze_and_dismiss_sync_never_hides_workflow_deficits(self) -> None:
		snap = _base_snapshot()
		snap["data"]["active_repair_orders"] = [
			{
				"name": "RO-PARTS",
				"customer": "Cust A",
				"vehicle": "Model 3",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"parts_summary": {
					"has_stock_deficit": True,
					"has_uncovered_deficit": True,
					"deficit_items": [{"item_code": "PART-1", "uncovered_qty": 2}],
				},
			},
			{
				"name": "RO-SNOOZED",
				"customer": "Cust B",
				"vehicle": "Model Y",
				"status": "Draft Estimate",
				"approval_status": "Estimating",
				"parts_summary": {},
			},
		]
		snap["data"]["action_items"] = [
			{
				"name": "ACT-1",
				"title": "P&L margin anomaly",
				"category": "Analytics Threshold",
				"priority": "High",
				"status": "Open",
				"source": "Agent",
				"dedup_key": "pnl_margin:2026-11-17",
				"creation": "2026-11-17 09:00:00",
			},
			{
				"name": "ACT-2",
				"title": "Already dismissed irregularity",
				"category": "Analytics Threshold",
				"priority": "High",
				"status": "Dismissed",
				"source": "Agent",
				"dedup_key": "old_alert:1",
			},
			{
				"name": "ACT-SNOOZE",
				"title": "Snoozed quote",
				"status": "Open",
				"source": "Rule Snooze",
				"dedup_key": "quote_needed:RO-SNOOZED",
				"snoozed_until": "2026-11-17 11:00:00",
			},
			{
				# Even if someone forged a Dismissed record for parts_deficit, derived deficit MUST stay visible
				"name": "ACT-FORGED",
				"title": "Dismissed parts",
				"status": "Dismissed",
				"source": "Rule Snooze",
				"dedup_key": "parts_deficit:RO-PARTS",
			},
		]

		view = build_cockpit_view(snap)
		keys = [it["dedup_key"] for it in view["action_queue"]]
		self.assertIn("parts_deficit:RO-PARTS", keys)
		self.assertIn("pnl_margin:2026-11-17", keys)
		self.assertNotIn("quote_needed:RO-SNOOZED", keys)
		self.assertNotIn("old_alert:1", keys)
		self.assertEqual(view["action_counts"]["suppressed"], 1)

	def test_dual_resource_bottlenecks_spillover_and_supplemental_gauges(self) -> None:
		snap = _base_snapshot()
		# Alignment-Bound when Alignment Rack bay (BAY-2) is booked 4h out of 4h window
		snap["data"]["schedule_entries"] = [
			{
				"name": "SE-ALIGN",
				"status": "Scheduled",
				"scheduled_date": "2026-11-17",
				"scheduled_time": "10:00:00",
				"estimated_duration": 210,
				"repair_order": "RO-ALIGN",
				"service_bay": "BAY-2",
				"assigned_technician": "EMP-1",
				"p80_duration_minutes": 270,
			},
			{
				"name": "SE-NEXT",
				"status": "Scheduled",
				"scheduled_date": "2026-11-17",
				"scheduled_time": "14:00:00",
				"estimated_duration": 60,
				"repair_order": "RO-NEXT",
				"service_bay": "BAY-2",
				"assigned_technician": "EMP-2",
				"p80_duration_minutes": 60,
			},
			{
				"name": "SE-TMRW",
				"status": "Scheduled",
				"scheduled_date": "2026-11-18",
				"scheduled_time": "09:00:00",
				"estimated_duration": 60,
				"repair_order": "RO-TMRW",
				"service_bay": "BAY-1",
				"assigned_technician": "EMP-1",
			},
		]
		snap["data"]["active_repair_orders"] = [
			{
				"name": "RO-TMRW",
				"status": "Waiting on Parts",
				"approval_status": "Approved",
				"parts_summary": {"has_stock_deficit": True, "has_uncovered_deficit": True},
			}
		]
		snap["data"]["health_snapshots"] = {
			"pnl_health": {
				"metric_key": "pnl_health",
				"period_date": "2026-11-17",
				"score": 86,
				"grade": "Healthy",
				"verdict": "Strong ELR $155/hr and 42% parts margin",
				"delta": 4,
				"creation": "2026-11-17 08:30:00",
			},
			"employee_performance": {
				"metric_key": "employee_performance",
				"period_date": "2026-11-17",
				"score": 91,
				"grade": "Healthy",
				"verdict": "Tech efficiency 108% flat-rate",
				"delta": 2,
				"creation": "2026-11-17 08:30:00",
			},
		}

		view = build_cockpit_view(snap)
		self.assertEqual(view["capacity"]["bottleneck"], "Alignment-Bound")
		gauges = {g["key"]: g for g in view["gauges"]}
		self.assertEqual(gauges["spillover_risk"]["value"], "1 at risk")
		self.assertEqual(gauges["pnl_health"]["value"], "86/100")
		self.assertFalse(gauges["pnl_health"]["is_stale"])
		self.assertEqual(gauges["pnl_health"]["delta"], 4)
		# Tomorrow readiness deducts 20 for uncovered parts on SE-TMRW
		self.assertEqual(gauges["tomorrow_readiness"]["value"], "80/100")
		self.assertIn("employee_performance", gauges)
		self.assertEqual(gauges["employee_performance"]["title"], "Employee Performance")

		# Tech-Bound case when 1 tech vs 2 free bays
		snap_tech = _base_snapshot()
		snap_tech["data"]["technician_overview"]["technicians"] = [
			{"employee": "EMP-1", "employee_name": "Tech One", "on_leave": False, "utilization_pct": 100.0},
			{"employee": "EMP-2", "employee_name": "Tech Two", "on_leave": True, "utilization_pct": 0.0},
		]
		snap_tech["data"]["schedule_entries"] = [
			{
				"name": "SE-BUSY-TECH",
				"status": "In Progress",
				"scheduled_date": "2026-11-17",
				"scheduled_time": "10:00:00",
				"estimated_duration": 240,
				"service_bay": "BAY-1",
				"assigned_technician": "EMP-1",
			}
		]
		view_tech = build_cockpit_view(snap_tech)
		self.assertEqual(view_tech["capacity"]["bottleneck"], "Tech-Bound")


class TestCockpitHTTPAndCLI(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.staged_dir = Path(self._tmp.name)
		self.server = start_stage_server(
			self.staged_dir,
			port=0,
			cockpit_token="cockpit-secret-token-123",
		)
		self.port = self.server.server_port
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self._tmp.cleanup()

	def _req(self, method: str, path: str, *, body: dict | None = None, headers: dict | None = None):
		conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
		hdrs = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
		if headers:
			hdrs.update(headers)
		raw = None
		if body is not None:
			raw = json.dumps(body).encode("utf-8")
			hdrs["Content-Type"] = "application/json"
			hdrs["Content-Length"] = str(len(raw))
		try:
			conn.request(method, path, body=raw, headers=hdrs)
			resp = conn.getresponse()
			return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
		finally:
			conn.close()

	def test_cockpit_http_security_and_routes(self) -> None:
		# Missing / invalid token rejected
		st, _, _ = self._req("GET", "/cockpit")
		self.assertEqual(st, 403)
		st, _, _ = self._req("GET", "/api/cockpit/snapshot", headers={"X-Cockpit-Token": "wrong"})
		self.assertEqual(st, 403)

		# Valid token renders HTML
		st, _, html = self._req("GET", "/cockpit?token=cockpit-secret-token-123")
		self.assertEqual(st, 200)
		self.assertIn("ISM Shop Health Cockpit", html)

		# Bad date -> 400
		st, _, raw = self._req(
			"GET",
			"/api/cockpit/snapshot?date=not-a-date",
			headers={"X-Cockpit-Token": "cockpit-secret-token-123"},
		)
		self.assertEqual(st, 400)

		# Cross-origin POST -> 403
		st, _, _ = self._req(
			"POST",
			"/api/cockpit/action-sync",
			body={"dedup_key": "k1", "action": "snooze"},
			headers={
				"Origin": "http://evil.example.com",
				"X-Cockpit-Token": "cockpit-secret-token-123",
			},
		)
		self.assertEqual(st, 403)

		# Valid snapshot and action-sync with mocked client
		class _FakeClient:
			def __init__(self, *_a, **_kw) -> None:
				pass

			def __enter__(self):
				return self

			def __exit__(self, *_a) -> None:
				pass

			def get_cockpit_snapshot(self, date=None):
				return _base_snapshot()

			def sync_action_item_state(self, **kwargs):
				return {"success": True, "data": {"status": "Open", "dedup_key": kwargs["dedup_key"]}}

		with patch("tools.cockpit_routes.IsmAgentClient", _FakeClient):
			st, _, raw = self._req(
				"GET",
				"/api/cockpit/snapshot?date=2026-11-17",
				headers={"X-Cockpit-Token": "cockpit-secret-token-123"},
			)
			self.assertEqual(st, 200, raw)
			payload = json.loads(raw)
			self.assertTrue(payload["success"])
			self.assertEqual(len(payload["gauges"]), 7)

			st, _, raw = self._req(
				"POST",
				"/api/cockpit/action-sync",
				body={"dedup_key": "dead_lift:RO-1", "action": "snooze", "snooze_minutes": 60},
				headers={
					"Origin": f"http://127.0.0.1:{self.port}",
					"X-Cockpit-Token": "cockpit-secret-token-123",
				},
			)
			self.assertEqual(st, 200, raw)
			self.assertEqual(json.loads(raw)["data"]["dedup_key"], "dead_lift:RO-1")

	def test_cli_validation_and_stage_specs_for_monitoring_commands(self) -> None:
		self.assertIn("cockpit", READ_ONLY_COMMANDS)
		self.assertIn("pnl-digest", READ_ONLY_COMMANDS)
		self.assertIn("publish-score", WRITE_COMMAND_META)
		self.assertIn("sync-action", WRITE_COMMAND_META)

		parser = _build_parser(relax_required=False)
		ns_pub = parser.parse_args([
			"publish-score", "--metric", "pnl_health", "--score", "105", "--verdict", "Bad score"
		])
		errs = validate_write_command_args(ns_pub)
		self.assertTrue(any("--score" in e for e in errs))

		ns_ok = parser.parse_args([
			"publish-score", "--metric", "pnl_health", "--score", "88", "--verdict", "Healthy margin", "--period-date", "2026-11-17"
		])
		self.assertEqual(validate_write_command_args(ns_ok), [])

		# Check --stage field overrides don't collide across commands
		relaxed = _build_parser(relax_required=True)
		ns_sync = relaxed.parse_args(["sync-action", "--dedup-key", "pnl:1", "--action", "snooze"])
		spec_sync = build_stage_form_spec([ns_sync], relaxed)
		action_field = next(f for f in spec_sync["steps"][0]["fields"] if f["name"] == "action")
		self.assertEqual(action_field["choices"], ["snooze", "dismiss", "reopen"])

		ns_ca = relaxed.parse_args(["create-action", "--title", "Margin Drop", "--source", "Agent", "--dedup-key", "pnl:1"])
		spec_ca = build_stage_form_spec([ns_ca], relaxed)
		src_field = next(f for f in spec_ca["steps"][0]["fields"] if f["name"] == "source")
		self.assertEqual(src_field["choices"], ["Manual", "Agent", "Board AI", "Rule Snooze"])

		# Dry-run pre-dispatch summary works cleanly
		summary = build_pre_dispatch_summary([ns_ok])
		self.assertTrue(summary["ready"])
		self.assertTrue(summary["has_writes"])

		# cockpit --stage is rejected cleanly
		self.assertEqual(cli.main(["--stage", "cockpit"]), 1)

		# cockpit without --data-only prints localhost URL using ensure_server_running
		with patch("tools.stage_server.ensure_server_running", return_value={
			"port": 51234,
			"cockpit_token": "tok-xyz",
		}):
			rc = cli.main(["--json", "cockpit", "--date", "2026-11-17"])
			self.assertEqual(rc, 0)


if __name__ == "__main__":
	unittest.main()

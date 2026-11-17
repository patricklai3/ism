"""Tests for IsmConfig, FrappeTransport pure REST, IsmAgentClient, and CLI."""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.demo_shop_flow import run_demo
from scripts.verify_connection import verify
from tools import cli, transport as transport_mod
from tools.client import IsmAgentClient, IsmApiError
from tools.config import IsmConfig
from tools.diagnostics import run_diagnostics


class _FakeHttpResponse:
	def __init__(self, payload: dict, status: int = 200) -> None:
		self._raw = json.dumps(payload).encode("utf-8")
		self.status = status

	def read(self) -> bytes:
		return self._raw

	def __enter__(self) -> _FakeHttpResponse:
		return self

	def __exit__(self, *_args) -> None:
		return None


class TestIsmConfig(unittest.TestCase):
	def test_from_env_loads_dotenv_and_overrides(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			env_file = Path(tmp) / ".env"
			env_file.write_text(
				'FRAPPE_URL="http://shop.local:8000/"\n'
				"FRAPPE_API_KEY=key123\n"
				"FRAPPE_API_SECRET='sec456'\n"
				"ISM_TIMEOUT=45\n",
				encoding="utf-8",
			)
			with patch.dict(os.environ, {}, clear=True):
				cfg = IsmConfig.from_env(env_path=env_file, overrides={"FRAPPE_SITE": "site.local"})

		self.assertEqual(cfg.base_url, "http://shop.local:8000")
		self.assertEqual(cfg.auth_header, "token key123:sec456")
		self.assertEqual(cfg.site, "site.local")
		self.assertEqual(cfg.timeout, 45)
		self.assertEqual(cfg.validate(), [])
		self.assertNotIn("sec456", repr(cfg))

	def test_validate_reports_missing_credentials(self) -> None:
		cfg = IsmConfig(url="http://localhost:8000", api_key="", api_secret="")
		errs = cfg.validate()
		self.assertEqual(len(errs), 2)


class TestIsmAgentClientAndTransport(unittest.TestCase):
	def setUp(self) -> None:
		self.cfg = IsmConfig(url="http://localhost:8000", api_key="k1", api_secret="s1")
		self.client = IsmAgentClient(self.cfg)

	def test_create_quote_uses_single_direct_rest_call(self) -> None:
		rest_reply = {
			"message": {
				"success": True,
				"repair_order": "RO-2026-00010",
				"urls": {"desk": "http://localhost:8000/app/repair-order/RO-2026-00010"},
			}
		}
		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(rest_reply)) as mock_open:
			res = self.client.create_quote(
				customer_name="Jane Doe",
				vehicle_identifier="5YJ3E1EBXNF123456",
			)

		self.assertEqual(mock_open.call_count, 1)
		req_obj = mock_open.call_args[0][0]
		self.assertEqual(req_obj.full_url, "http://localhost:8000/api/method/ism.api.agent.create_quote")
		self.assertEqual(req_obj.get_method(), "POST")
		sent = json.loads(req_obj.data.decode("utf-8"))
		self.assertEqual(sent["customer_name"], "Jane Doe")
		self.assertEqual(res["repair_order"], "RO-2026-00010")

	def test_curated_and_extended_methods_route_through_agent_facade(self) -> None:
		routes = [
			(lambda: self.client.get_parts_stock("RO-1"), "ism.api.agent.check_parts_stock"),
			(
				lambda: self.client.import_supplier_catalog(
					items=[{"part_number": "P1", "part_name": "Pad"}],
					supplier="Acme",
					service_package="SP-1",
				),
				"ism.api.agent.import_supplier_catalog",
			),
			(
				lambda: self.client.get_component_alternates(
					service_package="SP-1",
					slot_number=1,
					oem_part_number="1044341-00-D",
				),
				"ism.api.agent.get_component_alternates",
			),
			(
				lambda: self.client.submit_procurement_orders("RO-1", [{"item_code": "P1", "qty": 1, "supplier": "S1"}]),
				"ism.api.agent.submit_procurement_orders",
			),
			(
				lambda: self.client.submit_procurement_orders(parts_data=[{"item_code": "P1", "qty": 1, "supplier": "S1"}]),
				"ism.api.agent.submit_procurement_orders",
			),
			(
				lambda: self.client.create_purchase_payment(purchase_order="PO-1", payment_method="Cash", paid_amount=100.0),
				"ism.api.agent.create_purchase_payment",
			),
			(
				lambda: self.client.create_purchase_invoice(purchase_order="PO-1", update_stock=True, bill_no="INV-1"),
				"ism.api.agent.create_purchase_invoice",
			),
			(
				lambda: self.client.fetch_tesla_procedure(
					"https://service.tesla.com/docs/x.html", repair_order="RO-1", vehicle="VEH-1"
				),
				"ism.api.agent.fetch_tesla_manual",
			),
			(
				lambda: self.client.import_tesla_procedure(
					repair_order="RO-1",
					url="https://service.tesla.com/docs/x.html",
					on_conflict="overwrite",
					force=True,
					side="LH",
					is_supplement=1,
					advisor_notes="Check bushing",
				),
				"ism.api.agent.import_tesla_procedure",
			),
			(lambda: self.client.decode_vehicle_vin("5YJ3E1EBXNF123456"), "ism.api.agent.decode_vehicle_vin"),
			(lambda: self.client.search_vehicles(query="Model 3", limit=10), "ism.api.agent.search_vehicles"),
			(lambda: self.client.get_vehicle_intelligence("VEH-1", history_limit=15), "ism.api.agent.get_vehicle_intelligence"),
			(
				lambda: self.client.link_vehicle_customer("VEH-1", "Jane Doe", is_primary=True, relationship="Owner"),
				"ism.api.agent.link_vehicle_customer",
			),
			(lambda: self.client.list_inspection_templates(include_inactive=True), "ism.api.agent.list_inspection_templates"),
			(
				lambda: self.client.get_inspection_template(template_name="Standard Vehicle Walkaround"),
				"ism.api.agent.get_inspection_template",
			),
			(
				lambda: self.client.apply_inspection_template("RO-1", template_name="Standard Vehicle Walkaround", replace_existing=True),
				"ism.api.agent.apply_inspection_template",
			),
			(lambda: self.client.get_check_in_inspection("RO-1"), "ism.api.agent.get_check_in_inspection"),
			(
				lambda: self.client.record_check_in_inspection(
					"RO-1",
					items=[{"idx": 1, "status": "Pass"}],
					odometer_in=42000,
					customer_concern="Rattle",
					auto_apply_template=True,
				),
				"ism.api.agent.record_check_in_inspection",
			),
			(lambda: self.client.get_daily_tech_overview(), "ism.api.agent.get_tech_overview"),
			(lambda: self.client.get_unscheduled_services("RO-1"), "ism.api.agent.get_unscheduled_services"),
			(lambda: self.client.receive_purchase_order("PO-1"), "ism.api.agent.receive_purchase_order"),
			(lambda: self.client.checkout_repair_order("RO-1", confirm=False), "ism.api.agent.checkout_repair_order"),
			(lambda: self.client.toggle_service_completion("RO-1", "abc123"), "ism.api.agent.toggle_service_completion"),
			(lambda: self.client.add_supplement("RO-1", "Clip", labor_hours=0.5), "ism.api.agent.add_supplement"),
			(lambda: self.client.duplicate_service_for_opposite_side("RO-1", "abc123"), "ism.api.agent.duplicate_service_for_opposite_side"),
			(lambda: self.client.get_deferred_services(vehicle="VEH-1"), "ism.api.agent.get_deferred_services"),
			(lambda: self.client.add_deferred_services("RO-1", ["DEF-1"]), "ism.api.agent.add_deferred_services"),
			(lambda: self.client.list_shop_action_items(status="Open"), "ism.api.agent.list_shop_action_items"),
			(lambda: self.client.create_shop_action_item("Missing bolt"), "ism.api.agent.create_shop_action_item"),
			(lambda: self.client.resolve_shop_action_item("SAI-1"), "ism.api.agent.resolve_shop_action_item"),
			(lambda: self.client.reschedule_appointment("SE-0001", scheduled_date="2026-11-15"), "ism.api.agent.reschedule_appointment"),
			(lambda: self.client.update_appointment_status("SE-0001", "Confirmed"), "ism.api.agent.update_appointment_status"),
			(lambda: self.client.create_ro_from_appointment("SE-0001", odometer_in=42000), "ism.api.agent.create_ro_from_appointment"),
			(lambda: self.client.estimate_duration(repair_order="RO-1"), "ism.api.agent.estimate_duration"),
		]
		for call_fn, expected_method in routes:
			rest_reply = {"message": {"success": True, "data": {}}}
			with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(rest_reply)) as mock_open:
				res = call_fn()
			self.assertEqual(mock_open.call_count, 1)
			req_obj = mock_open.call_args[0][0]
			self.assertEqual(req_obj.full_url, f"http://localhost:8000/api/method/{expected_method}")
			self.assertTrue(res["success"])

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open:
			self.client.submit_procurement_orders(parts_data=[{"item_code": "P1", "qty": 1, "supplier": "S1"}])
			payload_default = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertIs(payload_default["submit"], True)
		self.assertNotIn("repair_order", payload_default)

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open:
			self.client.submit_procurement_orders(parts_data=[{"item_code": "P1", "qty": 1, "supplier": "S1"}], submit=False)
			payload_draft = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertIs(payload_draft["submit"], False)

		with self.assertRaisesRegex(ValueError, "service_package is required"):
			self.client.import_supplier_catalog(items=[{"part_number": "P1", "part_name": "Pad"}], supplier="Acme")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open:
			res_row_pkg = self.client.import_supplier_catalog(
				items=[{"part_number": "P1", "part_name": "Pad", "service_package": "SP-1"}],
				supplier="Acme",
			)
		self.assertTrue(res_row_pkg["success"])
		self.assertEqual(mock_open.call_count, 1)

	def test_generic_rest_crud_routes_directly(self) -> None:
		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"data": [{"name": "RO-1"}]})) as mock_open:
			docs = self.client.list_documents("Repair Order", limit=5)
		self.assertEqual(docs, [{"name": "RO-1"}])
		self.assertIn("/api/resource/Repair%20Order?", mock_open.call_args[0][0].full_url)
		self.assertIn("limit_page_length=5", mock_open.call_args[0][0].full_url)

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"data": {"name": "RO-1"}})) as mock_open:
			doc = self.client.get_document("Repair Order", "RO-1")
		self.assertEqual(doc["name"], "RO-1")
		self.assertEqual(mock_open.call_args[0][0].full_url, "http://localhost:8000/api/resource/Repair%20Order/RO-1")
		self.assertEqual(mock_open.call_args[0][0].get_method(), "GET")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"data": {"name": "RO-2"}})) as mock_open:
			created = self.client.create_document("Repair Order", {"customer": "Jane"})
		self.assertEqual(created["name"], "RO-2")
		self.assertEqual(mock_open.call_args[0][0].get_method(), "POST")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"data": {"name": "RO-2", "notes": "ok"}})) as mock_open:
			updated = self.client.update_document("Repair Order", "RO-2", {"notes": "ok"})
		self.assertEqual(updated["notes"], "ok")
		self.assertEqual(mock_open.call_args[0][0].get_method(), "PUT")

	def test_authorization_lock_error_propagates_cleanly(self) -> None:
		err_payload = json.dumps({"_server_messages": [json.dumps({"message": "Customer authorization required."})]}).encode("utf-8")
		http_err = urllib.error.HTTPError(
			"http://localhost:8000/api/method/ism.api.agent.update_job_status",
			417,
			"Expectation Failed",
			{},
			io.BytesIO(err_payload),
		)
		with patch("urllib.request.urlopen", side_effect=http_err):
			with self.assertRaises(IsmApiError) as ctx:
				self.client.update_job_status("RO-1", "start")

		self.assertEqual(ctx.exception.status_code, 417)
		self.assertIn("Customer authorization required", str(ctx.exception))

	def test_remote_disconnected_retries_once(self) -> None:
		calls = 0

		def side_effect(_req, timeout=30):
			nonlocal calls
			calls += 1
			if calls == 1:
				raise http.client.RemoteDisconnected("Remote end closed connection")
			return _FakeHttpResponse({"message": {"success": True}})

		with patch("urllib.request.urlopen", side_effect=side_effect):
			res = self.client.get_shop_floor_status()
		self.assertEqual(calls, 2)
		self.assertTrue(res["success"])

	def test_http_client_keep_alive_pool_reuses_and_retries_once(self) -> None:
		created = []

		class _FakePoolResp:
			def __init__(self, payload: dict, status: int = 200) -> None:
				self._raw = json.dumps(payload).encode("utf-8")
				self.status = status
				self.will_close = False

			def read(self) -> bytes:
				return self._raw

			def getheader(self, name: str, default: str | None = None) -> str | None:
				return "keep-alive" if name.lower() == "connection" else default

		class _FakeConn:
			def __init__(self, host: str, port: int, timeout: float = 30) -> None:
				self.host = host
				self.port = port
				self.timeout = timeout
				self.sock = None
				self.closed = False
				self.reqs = 0
				created.append(self)

			def request(self, method: str, path: str, body=None, headers=None) -> None:
				self.reqs += 1

			def getresponse(self):
				return _FakePoolResp({"message": {"success": True, "conn_idx": len(created)}})

			def close(self) -> None:
				self.closed = True

		with patch("http.client.HTTPConnection", side_effect=_FakeConn):
			r1 = self.client.get_shop_floor_status()
			r2 = self.client.get_shop_floor_status()
			self.assertEqual(len(created), 1)
			self.assertEqual(created[0].reqs, 2)
			self.assertTrue(r1["success"])
			self.assertTrue(r2["success"])

			# Simulate a stale pooled connection that drops on next request -> retries on fresh conn
			created[0].getresponse = lambda: (_ for _ in ()).throw(http.client.RemoteDisconnected("drop"))  # type: ignore[method-assign]
			r3 = self.client.get_shop_floor_status()
			self.assertEqual(len(created), 2)
			self.assertTrue(created[0].closed)
			self.assertTrue(r3["success"])
		self.client._tx.close()

	def test_new_contract_payload_serialization(self) -> None:
		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.import_tesla_procedure(
				repair_order="RO-1",
				url="https://service.tesla.com/docs/x.html",
				on_conflict="overwrite",
				force=True,
				side="LH",
				is_supplement=1,
				advisor_notes="Wear noticed",
			)
		sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent["on_conflict"], "overwrite")
		self.assertTrue(sent["force"])
		self.assertEqual(sent["side"], "LH")
		self.assertEqual(sent["is_supplement"], 1)
		self.assertEqual(sent["advisor_notes"], "Wear noticed")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.record_check_in_inspection(
				repair_order="RO-1",
				items=[{"idx": 1, "status": "Attention", "notes": "Scuff"}],
				odometer_in=42100,
				customer_concern="Front rattle",
				template_name="Standard Vehicle Walkaround",
				auto_apply_template=True,
			)
		sent_insp = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_insp["odometer_in"], 42100)
		self.assertEqual(sent_insp["customer_concern"], "Front rattle")
		self.assertTrue(sent_insp["auto_apply_template"])
		self.assertEqual(sent_insp["items"][0]["status"], "Attention")

		# Alias method names & parameter parity checks
		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.check_parts_stock(repair_order="RO-1", items=[{"item_code": "P1", "qty": 2}])
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.check_parts_stock"))
		self.assertEqual(json.loads(mock_open.call_args[0][0].data.decode("utf-8"))["items"], [{"item_code": "P1", "qty": 2}])

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.fetch_tesla_manual("https://service.tesla.com/docs/x.html", repair_order="RO-1", vehicle="VEH-1")
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.fetch_tesla_manual"))

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.get_tech_overview(date="2026-11-13")
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.get_tech_overview"))

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.create_quote(customer="Jane", vehicle="5YJ3E1EBXNF123456", concern="Squeak")
		sent_q = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_q["customer"], "Jane")
		self.assertEqual(sent_q["vehicle"], "5YJ3E1EBXNF123456")
		self.assertEqual(sent_q["concern"], "Squeak")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.find_available_slots(target_date="2026-11-14")
		self.assertEqual(json.loads(mock_open.call_args[0][0].data.decode("utf-8"))["target_date"], "2026-11-14")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.send_quote_for_approval("RO-1", communication_medium="email", recipient="jane@example.com")
		self.assertEqual(json.loads(mock_open.call_args[0][0].data.decode("utf-8"))["recipient"], "jane@example.com")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
			self.client.toggle_service_completion("RO-1", service_row_ids=["r1", "r2"])
		self.assertEqual(json.loads(mock_open.call_args[0][0].data.decode("utf-8"))["service_row_ids"], ["r1", "r2"])


class TestCli(unittest.TestCase):
	def test_cli_subcommands_parse_and_legacy_subcommand_rejected(self) -> None:
		parser = cli._build_parser()
		for cmd in (
			["test"],
			["procure", "--parts-json", '[{"item_code":"P1","qty":1,"supplier":"S1"}]'],
			["procure", "--parts-json", '[{"item_code":"P1","qty":1,"supplier":"S1"}]', "--payment-method", "Credit Card", "--paid-amount", "169.50"],
			["pay-purchase", "--purchase-order", "PO-1", "--payment-method", "Debit / Interac"],
			["purchase-invoice", "--purchase-order", "PO-1", "--update-stock", "--bill-no", "B-101"],
			["purchase-invoice", "--supplier", "Sup1", "--parts-json", '[{"item_code":"P1","qty":1}]'],
			["receive-po", "--purchase-order", "PO-1", "--submit-po"],
			["receive-po", "--purchase-invoice", "ACC-PINV-1"],
			["checkout", "--repair-order", "RO-1", "--confirm", "--odometer-out", "42150"],
			["check-stock", "--repair-order", "RO-1"],
			["check-stock", "--items-json", '["P1"]'],
			["send-approval", "--repair-order", "RO-1", "--medium", "email", "--recipient", "jane@example.com"],
			["toggle-service", "--repair-order", "RO-1", "--service-rows-json", '["r1","r2"]'],
			["job-status", "--repair-order", "RO-1", "--action", "complete", "--odometer-out", "42150"],
			["job-status", "--repair-order", "RO-1", "--action", "park"],
			["amend-quote", "--repair-order", "RO-1", "--add-services-json", "[]"],
			["book", "--repair-order", "RO-1", "--date", "2026-11-13", "--time", "09:00:00"],
			["reschedule", "--schedule-entry", "SE-1", "--date", "2026-11-14", "--time", "10:00:00"],
			["appointment-status", "--schedule-entry", "SE-1", "--status", "No Show"],
			["create-ro-from-entry", "--schedule-entry", "SE-1"],
			["estimate", "--repair-order", "RO-1"],
			["toggle-service", "--repair-order", "RO-1", "--service-row", "abc123"],
			["add-supplement", "--repair-order", "RO-1", "--title", "Extra Clip", "--hours", "0.5"],
			["duplicate-side", "--repair-order", "RO-1", "--service-row", "abc123"],
			["deferred", "--vehicle", "VEH-1"],
			["add-deferred", "--repair-order", "RO-1", "--item", "Cabin Air Filter"],
			["action-items", "--status", "Open"],
			["create-action", "--title", "Damaged splash shield"],
			["resolve-action", "--action-item", "SAI-1", "--notes", "Replaced"],
			[
				"alternates",
				"--service-package",
				"SP-1",
				"--component-slot",
				"Lower Link",
				"--slot-number",
				"1",
				"--oem",
				"1044341-00-D",
				"--item-code",
				"ITEM-1",
			],
			["fetch-tesla", "https://service.tesla.com/docs/x.html", "--repair-order", "RO-1", "--vehicle", "VEH-1"],
			[
				"import-tesla",
				"--repair-order",
				"RO-1",
				"--url",
				"https://service.tesla.com/docs/x.html",
				"--on-conflict",
				"overwrite",
				"--force",
				"--side",
				"LH",
			],
			["decode-vin", "--vin", "5YJ3E1EBXNF123456"],
			["search-vehicles", "--query", "5YJ3", "--customer", "Jane", "--model", "Model 3", "--limit", "10"],
			["vehicle-info", "--vehicle", "VEH-1", "--history-limit", "5"],
			["link-vehicle-customer", "--vehicle", "VEH-1", "--customer", "Jane", "--primary", "--relationship", "Owner"],
			["inspection-templates"],
			["inspection-templates", "--template", "Standard Vehicle Walkaround", "--include-inactive"],
			["apply-inspection", "--repair-order", "RO-1", "--template", "Standard Vehicle Walkaround", "--replace"],
			["get-inspection", "--repair-order", "RO-1"],
			[
				"record-inspection",
				"--repair-order",
				"RO-1",
				"--items-json",
				"[]",
				"--odometer-in",
				"42150",
				"--concern",
				"Rattle",
				"--auto-apply",
			],
			["list-docs", "--doctype", "Repair Order", "--limit", "10"],
			["get-doc", "--doctype", "Repair Order", "--name", "RO-1"],
			["create-doc", "--doctype", "ToDo", "--data-json", "{}"],
			["update-doc", "--doctype", "ToDo", "--name", "TD-1", "--data-json", "{}"],
			["call-method", "--method", "ism.api.parts.get_component_alternates", "--args-json", "{}"],
		):
			parsed = parser.parse_args(cmd)
			self.assertEqual(parsed.command, cmd[0])

		with patch("sys.stderr", new_callable=io.StringIO):
			with self.assertRaises(SystemExit) as exit_ctx:
				parser.parse_args(["mc" + "p"])
		self.assertEqual(exit_ctx.exception.code, 2)

	def test_cli_main_executes_and_handles_api_error(self) -> None:
		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "summary": {"active_bays": 4}}})), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "status"])
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(out.getvalue())["summary"]["active_bays"], 4)

		http_err = urllib.error.HTTPError("http://localhost:8000/api/method/x", 500, "Error", {}, io.BytesIO(b'{"message":"boom"}'))
		with patch("urllib.request.urlopen", side_effect=http_err), patch("sys.stderr", new_callable=io.StringIO) as err:
			code = cli.main(["status"])
		self.assertEqual(code, 1)
		self.assertIn("boom", err.getvalue())

	def test_verify_connection_runs_4_step_rest_check(self) -> None:
		cfg = IsmConfig(url="http://localhost:8000", api_key="k1", api_secret="s1")

		def side_effect(req, timeout=30):
			if req.full_url.endswith("frappe.auth.get_logged_user"):
				return _FakeHttpResponse({"message": "advisor@example.com"})
			if req.full_url.endswith("ism.api.agent.get_shop_floor_status"):
				return _FakeHttpResponse({"message": {"summary": {"active_bays": 4, "active_technicians": 3}}})
			return _FakeHttpResponse({"message": {"slots": [{"start": "09:00"}]}})

		with patch("urllib.request.urlopen", side_effect=side_effect) as mock_open, patch("sys.stdout", new_callable=io.StringIO) as out:
			ok = verify(cfg, full_e2e=False)
		self.assertTrue(ok)
		self.assertEqual(mock_open.call_count, 3)
		self.assertIn("[2/4] Frappe REST API OK", out.getvalue())

	def test_cli_structured_ingest_parts_and_inspection_dispatch(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			csv_file = Path(tmp) / "catalog.csv"
			csv_file.write_text("Part#,Description,Rate,OEM#\nP100,Control Arm,$125.00,1044341-00-D\n", encoding="utf-8")
			with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"total_created": 1}}})) as mock_open, \
				patch("sys.stdout", new_callable=io.StringIO):
				code = cli.main(["--json", "ingest-parts", str(csv_file), "--supplier", "Tesla", "--service-package", "SP-1"])
		self.assertEqual(code, 0)
		sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertNotIn("raw_text", sent)
		self.assertEqual(sent["supplier"], "Tesla")
		self.assertEqual(sent["service_package"], "SP-1")
		self.assertEqual(sent["items"][0]["part_number"], "P100")
		self.assertEqual(sent["items"][0]["unit_rate"], 125.0)

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"templates": []}}})) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			self.assertEqual(cli.main(["--json", "inspection-templates", "--include-inactive"]), 0)
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.list_inspection_templates"))

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"name": "Standard"}}})) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			self.assertEqual(cli.main(["--json", "inspection-templates", "--template", "Standard"]), 0)
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.get_inspection_template"))

	def test_cli_at_file_json_and_batch_toggle_service(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			payload_file = Path(tmp) / "services.json"
			payload_file.write_text(json.dumps([{"service_title": "Front Link", "labor_hours": 0.9}]), encoding="utf-8")
			parsed_from_file = cli._parse_json_opt(f"@{payload_file}")
		self.assertEqual(parsed_from_file, [{"service_title": "Front Link", "labor_hours": 0.9}])

		with self.assertRaises(FileNotFoundError):
			cli._parse_json_opt("@/nonexistent/ism_payload.json")

		parser = cli._build_parser()
		args = parser.parse_args(["toggle-service", "--repair-order", "RO-1", "--service-row", "r1", "r2", "--service-row", "r3,r4"])
		self.assertEqual(cli._flatten_service_rows(args.service_row), ["r1", "r2", "r3", "r4"])

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main(["--json", "toggle-service", "--repair-order", "RO-1", "--service-row", "r1", "r2"])
		self.assertEqual(code, 0)
		sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent["service_row_ids"], ["r1", "r2"])
		self.assertNotIn("service_row_id", sent)

	def test_cli_tags_provisional_book_and_rest_shorthand(self) -> None:
		parser = cli._build_parser()
		s_args = parser.parse_args([
			"slots",
			"--duration",
			"30",
			"--tags",
			"Lift",
			"--min-time",
			"09:50:00",
			"--include-past",
		])
		self.assertEqual(cli._flatten_csv_items(s_args.tags), ["Lift"])
		self.assertEqual(s_args.min_time, "09:50:00")
		self.assertTrue(s_args.include_past)

		b_args = parser.parse_args([
			"book",
			"--date",
			"2026-11-13",
			"--time",
			"10:00:00",
			"--duration",
			"30",
			"--entry-type",
			"Diagnostic",
			"--provisional-customer",
			"Walk-In",
		])
		self.assertIsNone(b_args.repair_order)
		self.assertEqual(b_args.duration, 30)
		self.assertEqual(b_args.provisional_customer, "Walk-In")

		l_args = parser.parse_args([
			"list-docs",
			"--doctype",
			"Schedule Entry",
			"--fields",
			"name,status,service_bay",
			"--filter",
			"scheduled_date=2026-11-13",
			"--filter",
			"status!=Cancelled",
			"--filter",
			"service_bay=like:%Bay%",
		])
		self.assertEqual(cli._parse_fields_opt(l_args.fields_json), ["name", "status", "service_bay"])
		self.assertEqual(
			cli._parse_filters_opt(l_args.filters_json, l_args.filter),
			[
				["scheduled_date", "=", "2026-11-13"],
				["status", "!=", "Cancelled"],
				["service_bay", "like", "%Bay%"],
			],
		)

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main(["--json", "slots", "--duration", "30", "--tags", "Lift,Alignment Rack", "--min-time", "10:00:00"])
		self.assertEqual(code, 0)
		sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent["equipment_tags"], ["Lift", "Alignment Rack"])
		self.assertEqual(sent["min_start_time"], "10:00:00")

		with patch("tools.cli.run_diagnostics", return_value=True) as mock_diag:
			self.assertEqual(cli.main(["verify-connection"]), 0)
		mock_diag.assert_called_once()

	def test_cli_batch_stdin_and_file_execution(self) -> None:
		batch_payload = [
			["status", "--date", "2026-11-15"],
			"tech-overview --date 2026-11-15",
			{"command": "slots", "args": {"duration": 30, "tags": ["Lift"]}},
		]
		fake_resp = _FakeHttpResponse({"message": {"success": True, "data": {"ok": True}}})
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdin", io.StringIO(json.dumps(batch_payload))), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch"])
		self.assertEqual(code, 0)
		parsed = json.loads(out.getvalue())
		self.assertTrue(parsed["success"])
		self.assertEqual(parsed["total"], 3)
		self.assertEqual(parsed["succeeded"], 3)
		self.assertEqual(parsed["failed"], 0)
		self.assertEqual(mock_open.call_count, 3)

		bom_payload = [["status", "--date", "2026-11-17"], ["tech-overview", "--date", "2026-11-16"]]
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdin", io.StringIO("\ufeff" + json.dumps(bom_payload))), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch"])
		self.assertEqual(code, 0)
		bom_parsed = json.loads(out.getvalue())
		self.assertTrue(bom_parsed["success"])
		self.assertEqual(bom_parsed["succeeded"], 2)
		self.assertEqual(json.loads(mock_open.call_args_list[0][0][0].data.decode("utf-8"))["date"], "2026-11-17")
		self.assertEqual(json.loads(mock_open.call_args_list[1][0][0].data.decode("utf-8"))["date"], "2026-11-16")

		with tempfile.TemporaryDirectory() as tmp:
			batch_file = Path(tmp) / "cmds.json"
			batch_file.write_text(
				json.dumps([
					[
						"quote",
						"--customer",
						"Jane Doe",
						"--vehicle",
						"5YJ3E1EBXNF123456",
						"--odometer-in",
						"42000",
						"--company",
						"Test Shop",
						"--notes",
						"Brake noise",
					]
				]),
				encoding="utf-8",
			)
			with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
				patch("sys.stdout", new_callable=io.StringIO) as out:
				code = cli.main(["--json", "batch", "--file", f"@{batch_file}"])
		self.assertEqual(code, 0)
		sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent["company"], "Test Shop")
		self.assertEqual(sent["notes"], "Brake noise")

		parser = cli._build_parser()
		with patch("sys.stderr", new_callable=io.StringIO):
			with self.assertRaises(SystemExit):
				parser.parse_args(["slots", "--target-date", "2026-11-15"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["quote", "--customer", "Jane", "--vehicle", "VIN1", "--concern", "Noise"])

	def test_cli_batch_cmd_and_powershell_unquoted_json_fallback(self) -> None:
		fake_resp = _FakeHttpResponse({"message": {"success": True, "data": {"ok": True}}})
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"batch",
				"--cmd",
				"status --date 2026-11-17",
				"--cmd",
				"book --date 2026-11-17 --time 10:00 --bay 'Bay 1 - Main Lift' --notes 'Walk-in check'",
			])
		self.assertEqual(code, 0)
		cmd_res = json.loads(out.getvalue())
		self.assertTrue(cmd_res["success"])
		self.assertEqual(cmd_res["succeeded"], 2)
		self.assertEqual(json.loads(mock_open.call_args_list[0][0][0].data.decode("utf-8"))["date"], "2026-11-17")
		book_sent = json.loads(mock_open.call_args_list[1][0][0].data.decode("utf-8"))
		self.assertEqual(book_sent["scheduled_date"], "2026-11-17")
		self.assertEqual(book_sent["start_time"], "10:00")
		self.assertEqual(book_sent["service_bay"], "Bay 1 - Main Lift")
		self.assertEqual(book_sent["notes"], "Walk-in check")

		# PowerShell-stripped double quotes on batch positional array and nested JSON
		ps_batch = (
			"[[status,--date,2026-11-17],"
			"[book,--date,2026-11-17,--time,10:00,--bay,Bay 1 - Main Lift],"
			"[amend-quote,--repair-order,RO-2026-00011,--add-services-json,[{service_title:Tire Rotation,labor_hours:0.5}]]]"
		)
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch", ps_batch])
		self.assertEqual(code, 0)
		ps_res = json.loads(out.getvalue())
		self.assertTrue(ps_res["success"])
		self.assertEqual(ps_res["succeeded"], 3)
		self.assertEqual(json.loads(mock_open.call_args_list[1][0][0].data.decode("utf-8"))["start_time"], "10:00")
		amend_sent = json.loads(mock_open.call_args_list[2][0][0].data.decode("utf-8"))
		self.assertEqual(amend_sent["add_services"], [{"service_title": "Tire Rotation", "labor_hours": 0.5}])

		# PowerShell-stripped --services-json with colon inside value and --remove-indices-json
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main([
				"--json",
				"quote",
				"--customer",
				"Jane Doe",
				"--vehicle",
				"5YJ3E1EA1PF200101",
				"--odometer-in",
				"42000",
				"--services-json",
				"[{service_title:Brake Check: LH/RH,labor_hours:0.5}]",
			])
		self.assertEqual(code, 0)
		quote_sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(quote_sent["services"], [{"service_title": "Brake Check: LH/RH", "labor_hours": 0.5}])

		with patch("sys.stderr", new_callable=io.StringIO) as err:
			code = cli.main(["--json", "batch", "--cmd", "status", "[[status]]"])
		self.assertEqual(code, 1)
		self.assertIn("Cannot combine --cmd", err.getvalue())

	def test_cli_service_colon_hours_and_windows_backslash_batch_cmd(self) -> None:
		fake_resp = _FakeHttpResponse({"message": {"success": True, "data": {"name": "RO-1"}}})
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main([
				"--json",
				"quote",
				"--customer",
				"Jane Doe",
				"--vehicle",
				"5YJ3E1EA1PF200101",
				"--odometer-in",
				"42000",
				"--service",
				"Brake Fluid Flush::1.0",
				"--service",
				"Front Brake Inspection",
			])
		self.assertEqual(code, 0)
		quote_sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(
			quote_sent["services"],
			[{"service_title": "Brake Fluid Flush", "labor_hours": 1.0}, "Front Brake Inspection"],
		)

		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"batch",
				"--cmd",
				"amend-quote --repair-order RO-1 --add-service 'Wiper Blade Replacement::0.3'",
				"--cmd",
				"send-approval --repair-order RO-1 --medium link",
			])
		self.assertEqual(code, 0)
		batch_res = json.loads(out.getvalue())
		self.assertTrue(batch_res["success"])
		amend_sent = json.loads(mock_open.call_args_list[0][0][0].data.decode("utf-8"))
		self.assertEqual(
			amend_sent["add_services"],
			[{"service_title": "Wiper Blade Replacement", "labor_hours": 0.3}],
		)

		with tempfile.TemporaryDirectory() as tmp:
			payload_path = Path(tmp) / "add_services.json"
			payload_path.write_text(
				json.dumps([{"service_title": "Cabin Filter", "labor_hours": 0.4}]),
				encoding="utf-8",
			)
			win_path = str(payload_path).replace("/", "\\")
			with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
				patch("sys.stdout", new_callable=io.StringIO) as out:
				code = cli.main([
					"--json",
					"batch",
					"--cmd",
					f"amend-quote --repair-order RO-1 --add-services-json @{win_path}",
				])
		self.assertEqual(code, 0)
		win_res = json.loads(out.getvalue())
		self.assertTrue(win_res["success"])
		win_sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(win_sent["add_services"], [{"service_title": "Cabin Filter", "labor_hours": 0.4}])

	def test_cli_batch_error_handling_and_stop_on_error(self) -> None:
		err_http = urllib.error.HTTPError(
			"http://localhost:8000/api/method/x", 417, "Conflict", {}, io.BytesIO(b'{"message":"Bay conflict"}')
		)
		ok_resp = _FakeHttpResponse({"message": {"success": True, "data": {"ok": True}}})
		payload = [["book", "--date", "2026-11-15", "--time", "09:00"], ["status", "--date", "2026-11-15"]]

		with patch("urllib.request.urlopen", side_effect=[err_http, ok_resp]), \
			patch("sys.stdin", io.StringIO(json.dumps(payload))), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch"])
		self.assertEqual(code, 1)
		res = json.loads(out.getvalue())
		self.assertFalse(res["success"])
		self.assertEqual(res["succeeded"], 1)
		self.assertEqual(res["failed"], 1)
		self.assertEqual(len(res["results"]), 2)
		self.assertIn("Bay conflict", res["results"][0]["error"])

		with patch("urllib.request.urlopen", side_effect=[err_http, ok_resp]), \
			patch("sys.stdin", io.StringIO(json.dumps(payload))), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch", "--stop-on-error"])
		self.assertEqual(code, 1)
		res_stopped = json.loads(out.getvalue())
		self.assertEqual(len(res_stopped["results"]), 1)
		self.assertEqual(res_stopped["failed"], 1)

		with patch("sys.stdin", io.StringIO(json.dumps([["batch"]]))), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "batch"])
		self.assertEqual(code, 1)
		self.assertIn("not allowed inside batch", json.loads(out.getvalue())["results"][0]["error"])

	def test_cli_intake_slots_and_slots_bay_technician_filters(self) -> None:
		fake_resp = _FakeHttpResponse({"message": {"success": True, "data": {"repair_order": "RO-1", "slots": []}}})
		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main([
				"--json",
				"slots",
				"--date",
				"2026-11-17",
				"--duration",
				"60",
				"--bay",
				"Bay 1 - Main Lift",
				"--technician",
				"Marcus Vance",
			])
		self.assertEqual(code, 0)
		sent_slots = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_slots["service_bay"], "Bay 1 - Main Lift")
		self.assertEqual(sent_slots["technician"], "Marcus Vance")

		with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main([
				"--json",
				"intake-slots",
				"--customer",
				"Sarah Jenkins",
				"--vehicle",
				"5YJ3E1EA1PF200101",
				"--odometer-in",
				"42000",
				"--service",
				"HEPA Cabin Air Filter Replacement",
				"--date",
				"2026-11-17",
				"--bay",
				"Bay 1 - Main Lift",
				"--tech",
				"HR-EMP-00004",
				"--max-slots",
				"5",
			])
		self.assertEqual(code, 0)
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.intake_and_find_slots"))
		sent_is = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_is["customer_name"], "Sarah Jenkins")
		self.assertEqual(sent_is["vehicle_identifier"], "5YJ3E1EA1PF200101")
		self.assertEqual(sent_is["services"], ["HEPA Cabin Air Filter Replacement"])
		self.assertEqual(sent_is["service_bay"], "Bay 1 - Main Lift")
		self.assertEqual(sent_is["technician"], "HR-EMP-00004")
		self.assertEqual(sent_is["max_slots"], 5)

		conflict_resp = _FakeHttpResponse({
			"message": {
				"success": False,
				"message": "Bay 1 - Main Lift is already occupied.",
				"data": {"status": "conflict", "conflict_type": "bay_conflict", "alternative_slots": []},
			}
		})
		with patch("urllib.request.urlopen", return_value=conflict_resp), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "book", "--repair-order", "RO-1", "--date", "2026-11-15", "--time", "09:00"])
		self.assertEqual(code, 1)
		self.assertFalse(json.loads(out.getvalue())["success"])

	def test_cli_dry_run_and_required_field_gate(self) -> None:
		# --dry-run reports missing required fields cleanly without any HTTP call
		with patch("urllib.request.urlopen") as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"--dry-run",
				"quote",
				"--customer",
				"Rob",
				"--vehicle",
				"5YJ3E1EA1PF200101",
				"--service",
				"Lateral Link Diagnosis",
			])
		self.assertEqual(code, 0)
		mock_open.assert_not_called()
		dry_res = json.loads(out.getvalue())
		self.assertTrue(dry_res["dry_run"])
		self.assertFalse(dry_res["ready"])
		self.assertIn("quote:--odometer-in", dry_res["missing_required"])
		self.assertIn("[MISSING — REQUIRED]", dry_res["summary_card"])

		# Live write calls fail fast before any HTTP call when required fields are missing
		failing_cases = [
			(["quote", "--customer", "Rob", "--vehicle", "5YJ3E1EA1PF200101", "--service", "Lateral Link"], "--odometer-in"),
			(["quote", "--customer", "Rob", "--vehicle", "5YJ3E1EA1PF200101", "--odometer-in", "48250"], "at least one required"),
			(["intake-slots", "--customer", "Rob", "--vehicle", "5YJ3E1EA1PF200101", "--service", "Alignment"], "--odometer-in"),
			(["create-ro-from-entry", "--schedule-entry", "SE-00001"], "--odometer-in"),
			(["job-status", "--repair-order", "RO-1", "--action", "complete"], "--odometer-out"),
			(["checkout", "--repair-order", "RO-1", "--odometer-out", "48255", "--confirm"], "--payment-method"),
			(["checkout", "--repair-order", "RO-1", "--payment-method", "Debit / Interac", "--confirm"], "--odometer-out"),
			(["checkout", "--repair-order", "RO-1", "--payment-method", "Debit / Interac", "--odometer-out", "48255"], "--confirm"),
			(["status", "--date", "tomorrow"], "ISO YYYY-MM-DD"),
			(["slots", "--date", "2026-11-17", "--min-time", "9am"], "24-hour HH:MM or HH:MM:SS"),
			(["book", "--date", "tomorrow", "--time", "10:00", "--repair-order", "RO-1"], "ISO YYYY-MM-DD"),
			(["book", "--date", "2026-11-17", "--time", "9am", "--repair-order", "RO-1"], "24-hour HH:MM or HH:MM:SS"),
			(["reschedule", "--schedule-entry", "SE-1", "--date", "friday"], "ISO YYYY-MM-DD"),
			(["procure", "--parts-json", '[{"item_code":"P1","qty":1,"supplier":"S1"}]', "--paid-amount", "150"], "--payment-method"),
			(["procure", "--parts-json", '[{"item_code":"P1","qty":1,"supplier":"S1"}]', "--draft", "--payment-method", "Cash"], "--draft"),
			(["pay-purchase", "--purchase-order", "PO-1"], "--payment-method"),
			(["pay-purchase", "--payment-method", "Cash"], "--purchase-order or --purchase-invoice"),
			(["purchase-invoice"], "--supplier (or --purchase-order)"),
			(["receive-po"], "--purchase-order or --purchase-invoice"),
			(["pay-purchase", "--purchase-order", "PO-1", "--payment-method", "Cash", "--reference-date", "today"], "ISO YYYY-MM-DD"),
			(["purchase-invoice", "--purchase-order", "PO-1", "--bill-date", "yesterday"], "ISO YYYY-MM-DD"),
		]
		for argv_case, expected_text in failing_cases:
			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stderr", new_callable=io.StringIO) as err:
				code = cli.main(argv_case)
			self.assertEqual(code, 1, f"Expected failure for {argv_case}")
			mock_open.assert_not_called()
			self.assertIn(expected_text, err.getvalue())

		# --dry-run marks invalid NL date/time as [INVALID] and ready=False
		with patch("urllib.request.urlopen") as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"--dry-run",
				"book",
				"--date",
				"tomorrow",
				"--time",
				"9am",
				"--repair-order",
				"RO-1",
			])
		self.assertEqual(code, 0)
		mock_open.assert_not_called()
		invalid_dry = json.loads(out.getvalue())
		self.assertFalse(invalid_dry["ready"])
		self.assertIn("[INVALID: `tomorrow`]", invalid_dry["summary_card"])
		self.assertIn("[INVALID: `9am`]", invalid_dry["summary_card"])

		# Subcommand --dry-run placement and batch --dry-run both work offline
		with patch("urllib.request.urlopen") as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"batch",
				"--dry-run",
				"--cmd",
				"quote --customer Rob --vehicle 5YJ3E1EA1PF200101 --odometer-in 48250 --service 'Lateral Link Diagnosis'",
				"--cmd",
				"send-approval --repair-order RO-1 --medium link",
			])
		self.assertEqual(code, 0)
		mock_open.assert_not_called()
		self.assertIn("### ISM Pre-Dispatch Summary", out.getvalue())
		self.assertIn("READY", out.getvalue())

		# Standalone procure --dry-run is ready without --repair-order
		with patch("urllib.request.urlopen") as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"--dry-run",
				"procure",
				"--parts-json",
				'[{"item_code":"OEM-P1-NEW","qty":1,"supplier":"PartsBin"}]',
			])
		self.assertEqual(code, 0)
		mock_open.assert_not_called()
		standalone_dry = json.loads(out.getvalue())
		self.assertTrue(standalone_dry["ready"])
		self.assertEqual(standalone_dry["missing_required"], [])

	def test_config_validation_and_transport_security_guards(self) -> None:
		bad_cfg = IsmConfig(url="ftp://user:pw@evil.local\n", api_key="k\r1", api_secret="s1", site="bad\nsite")
		errs = bad_cfg.validate()
		self.assertTrue(any("newline" in e for e in errs))

		bad_scheme = IsmConfig(url="file:///etc/passwd", api_key="k", api_secret="s")
		self.assertTrue(any("http://" in e for e in bad_scheme.validate()))

		bad_userinfo = IsmConfig(url="https://user:secret@shop.local", api_key="k", api_secret="s")
		self.assertTrue(any("userinfo" in e for e in bad_userinfo.validate()))

		client = IsmAgentClient(IsmConfig(url="http://localhost:8000", api_key="k", api_secret="s", company="ISM Corp"))
		with self.assertRaises(IsmApiError) as unpatched_ctx:
			client._tx.request_json("file:///etc/hosts", "GET")
		self.assertEqual(unpatched_ctx.exception.status_code, 400)
		with patch("urllib.request.urlopen") as mock_open:
			for bad_url in ("file:///etc/hosts", "ftp://example.com/a", "http://user:pw@localhost:8000/api"):
				with self.assertRaises(IsmApiError) as ctx:
					client._tx.request_json(bad_url, "GET")
				self.assertEqual(ctx.exception.status_code, 400)
			mock_open.assert_not_called()

		for bad_method in ("frappe.ping/../../etc", "bad method", "a..b"):
			with self.assertRaises(IsmApiError) as ctx:
				client.call_method(bad_method)
			self.assertEqual(ctx.exception.status_code, 400)

		with patch(
			"urllib.request.urlopen",
			side_effect=urllib.error.URLError("timed out"),
		):
			with self.assertRaises(IsmApiError) as ctx:
				client._tx.request_json("http://localhost:8000/api/method/x?token=secret123", "GET")
		self.assertNotIn("secret123", str(ctx.exception))

		bad_json_resp = _FakeHttpResponse({})
		bad_json_resp._raw = b"<html>\xffbad-json"
		with patch("urllib.request.urlopen", return_value=bad_json_resp):
			with self.assertRaises(IsmApiError) as ctx:
				client._tx.request_json("http://localhost:8000/api/method/x", "GET")
		self.assertIn("Invalid JSON response", str(ctx.exception))

		list_err_msg = transport_mod._format_http_error(
			400,
			json.dumps({"_server_messages": [json.dumps({"message": "List message 1"})]}),
		)
		self.assertEqual(list_err_msg, "HTTP 400: List message 1")
		str_err_msg = transport_mod._format_http_error(
			400,
			json.dumps({"_server_messages": json.dumps([json.dumps({"message": "Str message 1"})])}),
		)
		self.assertEqual(str_err_msg, "HTTP 400: Str message 1")

		with client:
			with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})) as mock_open:
				client.submit_procurement_orders(parts_data=[])
				po_sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
				self.assertEqual(po_sent["company"], "ISM Corp")

				client.create_purchase_invoice(purchase_order="PO-1")
				pi_sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
				self.assertEqual(pi_sent["company"], "ISM Corp")

			for bad_tesla in ("https://service.tesla.com@evil.com/x.html", "http://service.tesla.com/x.html"):
				with self.assertRaises(ValueError):
					client.fetch_tesla_procedure(bad_tesla)
				with self.assertRaises(ValueError):
					client.fetch_tesla_manual(bad_tesla)

	def test_diagnostics_secret_redaction_and_null_data_resilience(self) -> None:
		cfg = IsmConfig(url="http://localhost:8000", api_key="SUPERKEY99", api_secret="TOPSECRET77")
		responses = [
			{"message": "advisor@example.com"},
			{"message": {"success": True, "data": None}},
			{"message": {"success": True, "data": None}},
			{"message": {"success": True, "data": None, "repair_order": "RO-NULL-1"}},
			{"message": {"success": True, "data": None}},
			{"message": {"success": True, "urls": None}},
		]
		with patch("urllib.request.urlopen", side_effect=[_FakeHttpResponse(r) for r in responses]), \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			ok = run_diagnostics(cfg, full_e2e=True)
		self.assertTrue(ok)
		stdout_text = out.getvalue()
		self.assertNotIn("SUPERKEY", stdout_text)
		self.assertNotIn("TOPSECRET", stdout_text)
		self.assertNotIn("TOPS...", stdout_text)
		self.assertIn("key=***, secret=***", stdout_text)

		mock_demo_client = MagicMock()
		mock_demo_client.get_shop_floor_status.return_value = {"summary_card": "Floor OK"}
		mock_demo_client.create_quote.return_value = {"data": None, "repair_order": "RO-DEMO-1"}
		mock_demo_client.find_available_slots.return_value = {"summary_card": "Slots OK"}
		with patch("sys.stdout", new_callable=io.StringIO):
			run_demo(mock_demo_client, live=True)
		mock_demo_client.find_available_slots.assert_called_once_with(
			repair_order="RO-DEMO-1", days_ahead=3, include_summary=True
		)

	def test_cli_batch_success_false_container_validation_and_edge_cases(self) -> None:
		# Batch treats success=False response as failure, stops on --stop-on-error, exits 1
		responses = [
			_FakeHttpResponse({"message": {"success": False, "error": "Slot conflict"}}),
			_FakeHttpResponse({"message": {"success": True}}),
		]
		with patch("urllib.request.urlopen", side_effect=responses) as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main([
				"--json",
				"batch",
				"--stop-on-error",
				"--cmd",
				"book --date 2026-11-15 --time 09:00 --repair-order RO-1",
				"--cmd",
				"status",
			])
		self.assertEqual(code, 1)
		self.assertEqual(mock_open.call_count, 1)
		batch_out = json.loads(out.getvalue())
		self.assertFalse(batch_out["success"])
		self.assertFalse(batch_out["results"][0]["ok"])
		self.assertEqual(batch_out["results"][0]["error"], "Slot conflict")

		# Container type validation rejects dict/list mismatches before HTTP calls
		bad_containers = [
			["quote", "--customer", "Rob", "--vehicle", "VIN1", "--odometer-in", "100", "--services-json", '{"bad":1}'],
			["check-stock", "--items-json", '{"bad":1}'],
			["create-doc", "--doctype", "ToDo", "--data-json", '["bad"]'],
			["call-method", "--method", "frappe.ping", "--args-json", '["bad"]'],
		]
		for argv_case in bad_containers:
			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stderr", new_callable=io.StringIO) as err:
				code = cli.main(argv_case)
			self.assertEqual(code, 1, argv_case)
			mock_open.assert_not_called()
			self.assertIn("Expected JSON", err.getvalue())

		# Missing import-tesla targets and whitespace-only values fail validation cleanly
		self.assertTrue(
			any("--url/--data-json/--title" in e for e in cli.validate_write_command_args(argparse.Namespace(command="import-tesla")))
		)
		self.assertTrue(
			any("--customer" in e for e in cli.validate_write_command_args(
				argparse.Namespace(command="quote", customer="   ", vehicle="V1", odometer_in=10, service=["   "])
			))
		)
		ns_bad_int = argparse.Namespace(command="create-ro-from-entry", schedule_entry="SE-1", odometer_in="not-int")
		errs = cli.validate_write_command_args(ns_bad_int)
		self.assertTrue(any("non-negative integer" in e for e in errs))

		# list-docs merges dict --filters-json with --filter and splits on earliest operator
		merged_filters = cli._parse_filters_opt('{"status": "Open"}', ["note=a!=b"])
		self.assertEqual(merged_filters, [["status", "=", "Open"], ["note", "=", "a!=b"]])

		# ingest-parts requires --service-package, handles UTF-8 BOM, and rejects empty item files cleanly
		with tempfile.TemporaryDirectory() as tmp:
			bom_csv = Path(tmp) / "bom.csv"
			bom_csv.write_bytes(b"\xef\xbb\xbfPart#,Description,Rate\nP1,Sensor,$10.00\n")
			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stdout", new_callable=io.StringIO) as out:
				self.assertEqual(cli.main(["--json", "--dry-run", "ingest-parts", str(bom_csv)]), 0)
			mock_open.assert_not_called()
			dry_res = json.loads(out.getvalue())
			self.assertFalse(dry_res["ready"])
			self.assertEqual(dry_res["missing_required"], ["ingest-parts:--service-package"])

			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stderr", new_callable=io.StringIO) as err:
				code = cli.main(["--json", "ingest-parts", str(bom_csv)])
			self.assertEqual(code, 1)
			mock_open.assert_not_called()
			self.assertIn("--service-package", err.getvalue())

			with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True}})), \
				patch("sys.stdout", new_callable=io.StringIO):
				self.assertEqual(cli.main(["--json", "ingest-parts", str(bom_csv), "--service-package", "SP-1"]), 0)

			empty_json = Path(tmp) / "empty.json"
			empty_json.write_text("[]", encoding="utf-8")
			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stderr", new_callable=io.StringIO) as err:
				code = cli.main(["--json", "ingest-parts", str(empty_json), "--service-package", "SP-1"])
			self.assertEqual(code, 1)
			mock_open.assert_not_called()
			self.assertIn("No valid parts", err.getvalue())

		# Direct script invocation works cleanly
		repo_dir = Path(__file__).resolve().parent.parent
		proc = subprocess.run(
			[sys.executable, str(repo_dir / "tools" / "cli.py"), "--json", "--dry-run", "status"],
			cwd=str(repo_dir),
			capture_output=True,
			text=True,
			check=False,
		)
		self.assertEqual(proc.returncode, 0, proc.stderr)
		self.assertTrue(json.loads(proc.stdout)["dry_run"])

	def test_cli_and_client_amend_deposit_and_split_checkout(self) -> None:
		client = IsmAgentClient(IsmConfig(url="http://127.0.0.1:8000", api_key="k", api_secret="s"))

		with patch.object(client, "_call_agent", return_value={"success": True}) as mock_call:
			client.amend_quote(
				"RO-100",
				update_services=[{"idx": 1, "labor_hours": 1.5, "discount_amount": 10.0}],
				discount_amount=25.0,
				shop_supplies_amount=15.0,
				environmental_fee_amount=5.0,
			)
		mock_call.assert_called_once_with(
			"amend_quote",
			include_summary=False,
			repair_order="RO-100",
			add_services=None,
			remove_service_indices=None,
			update_services=[{"idx": 1, "labor_hours": 1.5, "discount_amount": 10.0}],
			notes=None,
			discount_amount=25.0,
			shop_supplies_amount=15.0,
			environmental_fee_amount=5.0,
		)

		with patch.object(client, "_call_agent", return_value={"success": True}) as mock_call:
			client.record_ro_deposit("RO-100", amount=200.0, payment_method="Visa", reference_no="AUTH-42")
		mock_call.assert_called_once_with(
			"record_ro_deposit",
			include_summary=False,
			repair_order="RO-100",
			amount=200.0,
			payment_method="Visa",
			reference_no="AUTH-42",
		)

		split_tenders = [
			{"payment_method": "Visa", "amount": 150.0, "reference_no": "V-1"},
			{"payment_method": "Cash", "amount": 50.0},
		]
		with patch.object(client, "_call_agent", return_value={"success": True}) as mock_call:
			client.checkout_repair_order("RO-100", odometer_out=51200, payments=split_tenders, confirm=True)
		mock_call.assert_called_once_with(
			"checkout_repair_order",
			include_summary=False,
			repair_order="RO-100",
			payment_method=None,
			paid_amount=None,
			odometer_out=51200,
			payments=split_tenders,
			confirm=True,
		)

		captured: list[dict] = []

		def _fake_urlopen(req, **_kwargs):
			captured.append(json.loads(req.data.decode("utf-8")))
			return _FakeHttpResponse({"message": {"success": True, "data": {"repair_order": "RO-100"}}})

		batch_cmds = [
			{
				"command": "amend-quote",
				"args": {
					"repair_order": "RO-100",
					"update_services": [{"idx": 1, "labor_hours": 2.0}],
					"discount_amount": 15.0,
					"shop_supplies_amount": 12.0,
					"environmental_fee_amount": 4.5,
					"notes": "Updated scope",
				},
			},
			{
				"command": "deposit",
				"args": {
					"repair_order": "RO-100",
					"amount": 250.0,
					"payment_method": "Debit / Interac",
					"reference_no": "INT-99",
				},
			},
			{
				"command": "checkout",
				"args": {
					"repair_order": "RO-100",
					"payment_method": "Visa",
					"payments": split_tenders,
					"odometer_out": 51200,
					"confirm": True,
				},
			},
		]
		with patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
			patch("sys.stdin", io.StringIO(json.dumps(batch_cmds))), \
			patch("sys.stdout", new_callable=io.StringIO) as out, \
			patch("sys.stderr", new_callable=io.StringIO) as err:
			code = cli.main(["--json", "batch"])
		self.assertEqual(code, 0, f"stdout={out.getvalue()} stderr={err.getvalue()}")
		batch_res = json.loads(out.getvalue())
		self.assertTrue(batch_res["success"])
		self.assertEqual(len(captured), 3)
		self.assertEqual(captured[0]["update_services"], [{"idx": 1, "labor_hours": 2.0}])
		self.assertEqual(captured[0]["discount_amount"], 15.0)
		self.assertEqual(captured[0]["shop_supplies_amount"], 12.0)
		self.assertEqual(captured[0]["environmental_fee_amount"], 4.5)
		self.assertEqual(captured[1]["repair_order"], "RO-100")
		self.assertEqual(captured[1]["amount"], 250.0)
		self.assertEqual(captured[1]["reference_no"], "INT-99")
		self.assertEqual(captured[2]["payments"], split_tenders)
		self.assertTrue(captured[2]["confirm"])

		# Also verify parser + handler dispatch when notes / payment_method are omitted
		from tools.cli_commands import _COMMAND_HANDLERS, _parse_batch_subcommand
		from tools.cli_parsers import _build_parser

		parser = _build_parser()
		cmd_amend, ns_amend = _parse_batch_subcommand(
			{
				"command": "amend-quote",
				"args": {"repair_order": "RO-100", "update_services": [{"idx": 1, "labor_hours": 1.5}]},
			},
			parser,
		)
		self.assertEqual(cmd_amend, "amend-quote")
		with patch.object(client, "_call_agent", return_value={"success": True}) as mock_call:
			_COMMAND_HANDLERS[cmd_amend](ns_amend, client, False)
		mock_call.assert_called_once_with(
			"amend_quote",
			include_summary=False,
			repair_order="RO-100",
			add_services=None,
			remove_service_indices=None,
			update_services=[{"idx": 1, "labor_hours": 1.5}],
			notes=None,
			discount_amount=None,
			shop_supplies_amount=None,
			environmental_fee_amount=None,
		)

		cmd_chk, ns_chk = _parse_batch_subcommand(
			{
				"command": "checkout",
				"args": {"repair_order": "RO-100", "payments": split_tenders, "odometer_out": 51200, "confirm": True},
			},
			parser,
		)
		self.assertEqual(cmd_chk, "checkout")
		with patch.object(client, "_call_agent", return_value={"success": True}) as mock_call:
			_COMMAND_HANDLERS[cmd_chk](ns_chk, client, False)
		mock_call.assert_called_once_with(
			"checkout_repair_order",
			include_summary=False,
			repair_order="RO-100",
			payment_method=None,
			paid_amount=None,
			odometer_out=51200,
			payments=split_tenders,
			confirm=True,
		)

		# Verify --dry-run / validate_write_command_args for amend-quote (--update-services without --notes),
		# checkout (--payments without --payment-method), and deposit (positive vs non-positive amount)
		for dry_argv in (
			["--json", "--dry-run", "amend-quote", "--repair-order", "RO-100", "--update-services", json.dumps([{"idx": 1, "labor_hours": 1.5}])],
			["--json", "--dry-run", "amend-quote", "--repair-order", "RO-100", "--discount-amount", "0"],
			["--json", "--dry-run", "checkout", "--repair-order", "RO-100", "--payments", json.dumps(split_tenders), "--odometer-out", "51200", "--confirm"],
			["--json", "--dry-run", "deposit", "RO-100", "--amount", "150"],
		):
			with patch("urllib.request.urlopen") as mock_open, \
				patch("sys.stdout", new_callable=io.StringIO) as out:
				code = cli.main(dry_argv)
			self.assertEqual(code, 0, out.getvalue())
			mock_open.assert_not_called()
			dry_res = json.loads(out.getvalue())
			self.assertTrue(dry_res["ready"], dry_res)

		with patch("urllib.request.urlopen") as mock_open, \
			patch("sys.stdout", new_callable=io.StringIO) as out:
			code = cli.main(["--json", "--dry-run", "deposit", "RO-100", "--amount", "0"])
		self.assertEqual(code, 0)
		mock_open.assert_not_called()
		bad_dep = json.loads(out.getvalue())
		self.assertFalse(bad_dep["ready"])
		self.assertIn("deposit:--amount (must be > 0)", bad_dep["missing_required"])

		# Direct CLI invocation for deposit
		captured.clear()
		with patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
			patch("sys.stdout", new_callable=io.StringIO):
			code = cli.main(["--json", "deposit", "RO-100", "--amount", "100", "--reference-no", "REF-1"])
		self.assertEqual(code, 0)
		self.assertEqual(captured[0]["repair_order"], "RO-100")
		self.assertEqual(captured[0]["amount"], 100.0)
		self.assertEqual(captured[0]["payment_method"], "Debit / Interac")
		self.assertEqual(captured[0]["reference_no"], "REF-1")


if __name__ == "__main__":
	unittest.main()

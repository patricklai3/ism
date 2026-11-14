"""Tests for IsmConfig, FrappeTransport pure REST, IsmAgentClient, CLI, and Open WebUI parity."""

from __future__ import annotations

import ast
import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts.verify_connection import verify
from tools import cli
from tools.client import IsmAgentClient, IsmApiError
from tools.config import IsmConfig


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
			(lambda: self.client.create_ro_from_appointment("SE-0001"), "ism.api.agent.create_ro_from_appointment"),
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


class TestCliAndOpenWebUi(unittest.TestCase):
	def test_cli_subcommands_parse_and_legacy_subcommand_rejected(self) -> None:
		parser = cli._build_parser()
		for cmd in (
			["test"],
			["receive-po", "--purchase-order", "PO-1", "--submit-po"],
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

	def test_openwebui_parity(self) -> None:
		expected_methods = [
			"ism_create_quote",
			"ism_amend_quote",
			"ism_send_quote_for_approval",
			"ism_find_available_slots",
			"ism_book_appointment",
			"ism_get_shop_floor_status",
			"ism_update_job_status",
			"ism_import_supplier_catalog",
			"ism_import_tesla_procedure",
			"ism_check_parts_stock",
			"ism_submit_procurement_orders",
			"ism_fetch_tesla_manual",
			"ism_get_tech_overview",
			"ism_get_unscheduled_services",
			"ism_receive_purchase_order",
			"ism_checkout_repair_order",
			"ism_toggle_service_completion",
			"ism_add_supplement",
			"ism_duplicate_service_for_opposite_side",
			"ism_get_deferred_services",
			"ism_add_deferred_services",
			"ism_list_shop_action_items",
			"ism_create_shop_action_item",
			"ism_resolve_shop_action_item",
			"ism_reschedule_appointment",
			"ism_update_appointment_status",
			"ism_create_ro_from_appointment",
			"ism_estimate_duration",
			"ism_get_component_alternates",
			"ism_decode_vehicle_vin",
			"ism_search_vehicles",
			"ism_get_vehicle_intelligence",
			"ism_link_vehicle_customer",
			"ism_list_inspection_templates",
			"ism_get_inspection_template",
			"ism_apply_inspection_template",
			"ism_get_check_in_inspection",
			"ism_record_check_in_inspection",
		]
		ow_path = Path(__file__).resolve().parent.parent / "openwebui_tools.py"
		tree = ast.parse(ow_path.read_text(encoding="utf-8"))
		tools_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Tools")
		defined = {n.name for n in tools_cls.body if isinstance(n, ast.FunctionDef)}
		for name in expected_methods:
			self.assertIn(name, defined, f"openwebui_tools.Tools missing {name}")

		try:
			import openwebui_tools
		except ImportError:
			return

		ow_tools = openwebui_tools.Tools()
		for name in expected_methods:
			self.assertTrue(hasattr(ow_tools, name), f"openwebui_tools.Tools missing {name}")

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"alternates": []}}})) as mock_open:
			raw_out = ow_tools.ism_get_component_alternates(oem_part_number="1044341-00-D", slot_number=1)
		self.assertTrue(json.loads(raw_out)["success"])
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.get_component_alternates"))

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"vin": "5YJ3E1EBXNF123456"}}})) as mock_open:
			vin_out = ow_tools.ism_decode_vehicle_vin(vin="5YJ3E1EBXNF123456")
		self.assertTrue(json.loads(vin_out)["success"])
		self.assertTrue(mock_open.call_args[0][0].full_url.endswith("ism.api.agent.decode_vehicle_vin"))

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {"updated_count": 1}}})) as mock_open:
			insp_out = ow_tools.ism_record_check_in_inspection(
				repair_order="RO-1", items='[{"idx": 1, "status": "Pass"}]', odometer_in=42100
			)
		self.assertTrue(json.loads(insp_out)["success"])
		sent_ow = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_ow["items"], [{"idx": 1, "status": "Pass"}])

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open:
			ow_tools.ism_check_parts_stock(repair_order="RO-1", items='[{"item_code":"P1","qty":1}]')
		sent_stk = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
		self.assertEqual(sent_stk["repair_order"], "RO-1")
		self.assertEqual(sent_stk["items"], [{"item_code": "P1", "qty": 1}])

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse({"message": {"success": True, "data": {}}})) as mock_open:
			ow_tools.ism_toggle_service_completion(repair_order="RO-1", service_row_ids='["r1","r2"]')
		self.assertEqual(json.loads(mock_open.call_args[0][0].data.decode("utf-8"))["service_row_ids"], ["r1", "r2"])

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


if __name__ == "__main__":
	unittest.main()

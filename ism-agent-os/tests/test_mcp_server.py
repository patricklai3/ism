"""Tests for IsmConfig, IsmAgentClient, and IsmMcpServer JSON-RPC protocol."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools.client import IsmAgentClient, IsmApiError
from tools.config import IsmConfig
from tools.ism_mcp_server import IsmMcpServer
from tools.tool_registry import get_mcp_tools


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
				"ISM_MCP_TIMEOUT=45\n",
				encoding="utf-8",
			)
			with patch.dict(os.environ, {}, clear=True):
				cfg = IsmConfig.from_env(env_path=env_file, overrides={"FRAPPE_SITE": "site.local"})

		self.assertEqual(cfg.base_url, "http://shop.local:8000")
		self.assertEqual(cfg.auth_header, "token key123:sec456")
		self.assertEqual(cfg.site, "site.local")
		self.assertEqual(cfg.timeout, 45)
		self.assertEqual(cfg.validate(), [])

	def test_validate_reports_missing_credentials(self) -> None:
		cfg = IsmConfig(url="http://localhost:8000", api_key="", api_secret="", transport="invalid")
		errs = cfg.validate()
		self.assertEqual(len(errs), 3)


class TestIsmMcpServer(unittest.TestCase):
	def setUp(self) -> None:
		self.cfg = IsmConfig(url="http://localhost:8000", api_key="k1", api_secret="s1")
		self.client = IsmAgentClient(self.cfg)
		self.server = IsmMcpServer(self.client)

	def test_initialize_ping_and_notification(self) -> None:
		init = self.server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
		self.assertEqual(init["result"]["serverInfo"]["name"], "ism-agent-os")
		self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")

		notif = self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
		self.assertIsNone(notif)

		ping = self.server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})
		self.assertEqual(ping["result"], {})

	def test_tools_list_exposes_curated_and_generic_tools(self) -> None:
		res = self.server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
		tools = res["result"]["tools"]
		names = {t["name"] for t in tools}
		self.assertEqual(len(tools), len(get_mcp_tools()))
		self.assertIn("ism_create_quote", names)
		self.assertIn("ism_book_appointment", names)
		self.assertIn("ism_update_job_status", names)
		self.assertIn("frappe_list_documents", names)

	def test_tools_call_routes_through_frappe_mcp_and_formats_card(self) -> None:
		mcp_reply = {
			"jsonrpc": "2.0",
			"id": 1,
			"result": {
				"structuredContent": {
					"result": {
						"success": True,
						"repair_order": "RO-2026-00010",
						"summary_card": "### Repair Order Quote #RO-2026-00010\nGrand Total: $158.20",
						"urls": {"desk": "http://localhost:8000/app/repair-order/RO-2026-00010"},
					}
				},
				"isError": False,
			},
		}

		with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(mcp_reply)) as mock_open:
			res = self.server.handle_message({
				"jsonrpc": "2.0",
				"id": 4,
				"method": "tools/call",
				"params": {
					"name": "ism_create_quote",
					"arguments": {
						"customer_name": "Jane Doe",
						"vehicle_identifier": "5YJ3E1EBXNF123456",
						"extra_ignored_arg": "should-be-filtered",
					},
				},
			})

		req_obj = mock_open.call_args[0][0]
		sent = json.loads(req_obj.data.decode("utf-8"))
		self.assertEqual(req_obj.full_url, "http://localhost:8000/api/v2/mcp")
		self.assertEqual(sent["params"]["arguments"]["method"], "ism.api.agent.create_quote")
		self.assertNotIn("extra_ignored_arg", sent["params"]["arguments"]["args"])
		self.assertFalse(res["result"]["isError"])
		self.assertIn("Repair Order Quote #RO-2026-00010", res["result"]["content"][0]["text"])

	def test_automatic_rest_fallback_when_mcp_endpoint_missing(self) -> None:
		rest_reply = {
			"message": {
				"success": True,
				"summary_card": "### Shop Floor Status\nActive Bays: 4",
				"summary": {"active_bays": 4},
			}
		}

		def side_effect(req, timeout=30):
			if req.full_url.endswith("/api/v2/mcp"):
				raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"Not Found"))
			return _FakeHttpResponse(rest_reply)

		with patch("urllib.request.urlopen", side_effect=side_effect) as mock_open:
			res = self.server.handle_message({
				"jsonrpc": "2.0",
				"id": 5,
				"method": "tools/call",
				"params": {"name": "ism_get_shop_floor_status", "arguments": {}},
			})

		self.assertEqual(mock_open.call_count, 2)
		self.assertFalse(res["result"]["isError"])
		self.assertEqual(res["result"]["structuredContent"]["summary"]["active_bays"], 4)

	def test_missing_required_arg_returns_tool_error(self) -> None:
		res = self.server.handle_message({
			"jsonrpc": "2.0",
			"id": 6,
			"method": "tools/call",
			"params": {"name": "ism_update_job_status", "arguments": {"repair_order": "RO-2026-00010"}},
		})
		self.assertTrue(res["result"]["isError"])
		self.assertIn("action", res["result"]["content"][0]["text"])

	def test_authorization_lock_error_propagates_cleanly(self) -> None:
		err_payload = json.dumps({"_server_messages": [json.dumps({"message": "Customer authorization required."})]}).encode("utf-8")
		http_err = urllib.error.HTTPError("http://localhost:8000/api/v2/mcp", 417, "Expectation Failed", {}, io.BytesIO(err_payload))
		with patch("urllib.request.urlopen", side_effect=http_err):
			res = self.server.handle_message({
				"jsonrpc": "2.0",
				"id": 7,
				"method": "tools/call",
				"params": {"name": "ism_update_job_status", "arguments": {"repair_order": "RO-1", "action": "start"}},
			})

		self.assertTrue(res["result"]["isError"])
		self.assertIn("Customer authorization required", res["result"]["content"][0]["text"])


if __name__ == "__main__":
	unittest.main()

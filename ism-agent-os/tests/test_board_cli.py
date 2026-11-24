"""Tests for Shop Board OS client methods, CLI parsers, validation, and --dry-run gating."""

from __future__ import annotations

import argparse
import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tools import cli
from tools.cli_parsers import _build_parser
from tools.cli_validation import (
	DIRECT_HOUSEKEEPING_COMMANDS,
	READ_ONLY_COMMANDS,
	WRITE_COMMAND_META,
	build_pre_dispatch_summary,
	validate_write_command_args,
)
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


class TestBoardClientMethods(unittest.TestCase):
	def setUp(self) -> None:
		self.cfg = IsmConfig(url="http://localhost:8000", api_key="k1", api_secret="s1")
		self.client = IsmAgentClient(self.cfg)

	def test_removed_legacy_board_methods_absent(self) -> None:
		self.assertFalse(hasattr(self.client, "resolve_board_card"))
		self.assertFalse(hasattr(self.client, "undo_board_receipt"))

	def test_board_client_methods_route_and_strip_none(self) -> None:
		cases = [
			(
				lambda: self.client.get_action_cards_context(
					action_items=["ACT-2026-00012", "ACT-2026-00013"],
					repair_order="RO-2026-00011",
					status="Open",
					include_events_limit=15,
				),
				"ism.api.board.get_action_cards_context",
				{
					"action_items": ["ACT-2026-00012", "ACT-2026-00013"],
					"repair_order": "RO-2026-00011",
					"status": "Open",
					"include_events_limit": 15,
				},
			),
			(
				lambda: self.client.list_board_threads(
					status="NEEDS_ACTION",
					assigned_only=True,
					query="RO-2026",
					limit=25,
				),
				"ism.api.board.list_board_threads",
				{
					"status": "NEEDS_ACTION",
					"assigned_only": True,
					"query": "RO-2026",
					"limit": 25,
				},
			),
			(
				lambda: self.client.get_board_thread(
					repair_order="RO-2026-00011",
					limit=50,
					before_event="BE-0010",
				),
				"ism.api.board.get_board_thread",
				{
					"repair_order": "RO-2026-00011",
					"limit": 50,
					"before_event": "BE-0010",
				},
			),
			(
				lambda: self.client.post_board_capture(
					thread_key="RO:RO-2026-00011",
					text="Parts ordered, ETA tomorrow 10am",
					intent_tag="Parts",
					metadata={"source": "cli"},
					idempotency_key="idem-1",
				),
				"ism.api.board.post_board_capture",
				{
					"thread_key": "RO:RO-2026-00011",
					"text": "Parts ordered, ETA tomorrow 10am",
					"intent_tag": "Parts",
					"metadata": {"source": "cli", "skip_ai": True},
					"idempotency_key": "idem-1",
				},
			),
			(
				lambda: self.client.post_board_capture(
					repair_order="RO-2026-00011",
					text="Front left upper control arm cracked",
					skip_ai=False,
				),
				"ism.api.board.post_board_capture",
				{
					"repair_order": "RO-2026-00011",
					"text": "Front left upper control arm cracked",
				},
			),
		]

		for call_fn, expected_method, expected_payload in cases:
			with self.subTest(method=expected_method, payload=expected_payload):
				with patch(
					"urllib.request.urlopen",
					return_value=_FakeHttpResponse({"message": {"success": True, "method": expected_method}}),
				) as mock_open:
					res = call_fn()
				self.assertEqual(mock_open.call_count, 1)
				req_obj = mock_open.call_args[0][0]
				self.assertEqual(req_obj.full_url, f"http://localhost:8000/api/method/{expected_method}")
				self.assertEqual(req_obj.get_method(), "POST")
				sent = json.loads(req_obj.data.decode("utf-8"))
				self.assertEqual(sent, expected_payload)
				self.assertTrue(res["success"])

	def test_board_api_error_propagates_cleanly(self) -> None:
		err_payload = json.dumps({
			"_server_messages": [json.dumps({"message": "Captures are locked for closed Repair Orders."})]
		}).encode("utf-8")
		http_err = urllib.error.HTTPError(
			"http://localhost:8000/api/method/ism.api.board.post_board_capture",
			417,
			"Expectation Failed",
			{},
			io.BytesIO(err_payload),
		)
		with patch("urllib.request.urlopen", side_effect=http_err):
			with self.assertRaises(IsmApiError) as ctx:
				self.client.post_board_capture(repair_order="RO-2026-00011", text="Late update")

		self.assertEqual(ctx.exception.status_code, 417)
		self.assertIn("Captures are locked for closed Repair Orders.", str(ctx.exception))


class TestBoardCliParsingAndGating(unittest.TestCase):
	def setUp(self) -> None:
		self.env_vars = {
			"FRAPPE_URL": "http://localhost:8000",
			"FRAPPE_API_KEY": "k1",
			"FRAPPE_API_SECRET": "s1",
		}

	def test_read_only_housekeeping_and_write_metadata_classification(self) -> None:
		for ro_cmd in ("action-cards", "board-threads", "board-thread"):
			self.assertIn(ro_cmd, READ_ONLY_COMMANDS)
			self.assertNotIn(ro_cmd, WRITE_COMMAND_META)

		self.assertIn("resolve-action", DIRECT_HOUSEKEEPING_COMMANDS)
		self.assertNotIn("board-post", READ_ONLY_COMMANDS)
		self.assertIn("board-post", WRITE_COMMAND_META)

		parser = _build_parser()
		subcommands = parser._subparsers._group_actions[0].choices
		for removed_cmd in ("board-resolve", "board-undo"):
			self.assertNotIn(removed_cmd, subcommands)
			self.assertNotIn(removed_cmd, WRITE_COMMAND_META)
			self.assertNotIn(removed_cmd, READ_ONLY_COMMANDS)

	def test_action_cards_and_board_parsers(self) -> None:
		parser = _build_parser()

		ns_ac = parser.parse_args([
			"action-cards",
			"--action-item",
			"ACT-2026-00012",
			"ACT-2026-00013",
			"--repair-order",
			"RO-2026-00011",
			"--status",
			"Open",
			"--limit",
			"10",
			"--events-limit",
			"12",
		])
		self.assertEqual(ns_ac.action_item, ["ACT-2026-00012", "ACT-2026-00013"])
		self.assertEqual(ns_ac.repair_order, "RO-2026-00011")
		self.assertEqual(ns_ac.status, "Open")
		self.assertEqual(ns_ac.limit, 10)
		self.assertEqual(ns_ac.events_limit, 12)

		ns_bts = parser.parse_args(["board-threads"])
		self.assertEqual(ns_bts.status, "OPEN")
		with redirect_stderr(io.StringIO()):
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-threads", "--thread-type", "REPAIR_ORDER"])

		ns_ca = parser.parse_args(["create-action", "--title", "Needs quote", "--source", "Board AI"])
		self.assertEqual(ns_ca.source, "Board AI")

	def test_thread_key_and_repair_order_mutual_exclusion_and_required(self) -> None:
		parser = _build_parser()
		with redirect_stderr(io.StringIO()):
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-thread"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-thread", "--thread-key", "RO:RO-1", "--repair-order", "RO-1"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-post", "--text", "hello"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-post", "--thread-key", "RO:RO-1", "--repair-order", "RO-1", "--text", "hello"])

		ns_tk = parser.parse_args(["board-thread", "--thread-key", "RO:RO-2026-00011", "--limit", "20"])
		self.assertEqual(ns_tk.thread_key, "RO:RO-2026-00011")
		self.assertIsNone(ns_tk.repair_order)
		self.assertEqual(ns_tk.limit, 20)

		ns_ro = parser.parse_args(["board-post", "--repair-order", "RO-2026-00011", "--text", "Pad wear 2mm", "--intent", "Supplement"])
		self.assertEqual(ns_ro.repair_order, "RO-2026-00011")
		self.assertIsNone(ns_ro.thread_key)
		self.assertEqual(ns_ro.text, "Pad wear 2mm")
		self.assertEqual(ns_ro.intent, "Supplement")
		self.assertFalse(ns_ro.run_ai)

		ns_ai = parser.parse_args(["board-post", "--repair-order", "RO-2026-00011", "--text", "Need supplement", "--run-ai"])
		self.assertTrue(ns_ai.run_ai)

		# Direct Namespace validation also rejects mutually exclusive targets (e.g. batch dict).
		both_ns = argparse.Namespace(
			command="board-post",
			thread_key="RO:RO-1",
			repair_order="RO-1",
			text="hello",
		)
		errs = validate_write_command_args(both_ns)
		self.assertIn("--thread-key/--repair-order (mutually exclusive)", errs)

	def test_cli_dry_run_gating_for_board_writes(self) -> None:
		with patch.dict("os.environ", self.env_vars, clear=False), patch("urllib.request.urlopen") as mock_open:
			buf = io.StringIO()
			with redirect_stdout(buf):
				rc = cli.main(["--json", "--dry-run", "board-post", "--thread-key", "RO:RO-2026-00011", "--text", "Parts ETA 2pm"])
			self.assertEqual(rc, 0)
			out = json.loads(buf.getvalue())
			self.assertTrue(out["dry_run"])
			self.assertTrue(out["has_writes"])
			self.assertEqual(out["missing_required"], [])
		self.assertEqual(mock_open.call_count, 0)

		summary = build_pre_dispatch_summary([
			argparse.Namespace(command="board-post", thread_key=None, repair_order=None, text=None),
		])
		self.assertFalse(summary["ready"])
		self.assertIn("board-post:--text", summary["missing_required"])
		self.assertIn("board-post:--thread-key/--repair-order (one required)", summary["missing_required"])

	def test_cli_main_executes_board_commands_with_skip_ai_and_propagates_errors(self) -> None:
		with patch.dict("os.environ", self.env_vars, clear=False):
			cases = [
				(
					["--json", "action-cards", "--action-item", "ACT-2026-00012", "ACT-2026-00013", "--events-limit", "10"],
					"ism.api.board.get_action_cards_context",
					{
						"action_items": ["ACT-2026-00012", "ACT-2026-00013"],
						"status": "Open",
						"include_events_limit": 10,
						"limit": 20,
					},
				),
				(
					["--json", "board-threads", "--status", "NEEDS_ACTION", "--assigned-only"],
					"ism.api.board.list_board_threads",
					{"status": "NEEDS_ACTION", "assigned_only": True, "limit": 50},
				),
				(
					["--json", "board-thread", "--repair-order", "RO-2026-00011"],
					"ism.api.board.get_board_thread",
					{"repair_order": "RO-2026-00011", "limit": 100},
				),
				(
					["--json", "board-post", "--thread-key", "RO:RO-2026-00011", "--text", "Done"],
					"ism.api.board.post_board_capture",
					{"thread_key": "RO:RO-2026-00011", "text": "Done", "metadata": {"skip_ai": True}},
				),
				(
					["--json", "board-post", "--repair-order", "RO-2026-00011", "--text", "Check strut", "--run-ai"],
					"ism.api.board.post_board_capture",
					{"repair_order": "RO-2026-00011", "text": "Check strut"},
				),
			]
			for argv, expected_method, expected_payload in cases:
				with self.subTest(argv=argv):
					with patch(
						"urllib.request.urlopen",
						return_value=_FakeHttpResponse({"message": {"success": True, "endpoint": expected_method}}),
					) as mock_open:
						buf = io.StringIO()
						with redirect_stdout(buf):
							rc = cli.main(argv)
					self.assertEqual(rc, 0)
					self.assertEqual(mock_open.call_count, 1)
					req_obj = mock_open.call_args[0][0]
					self.assertEqual(req_obj.full_url, f"http://localhost:8000/api/method/{expected_method}")
					self.assertEqual(json.loads(req_obj.data.decode("utf-8")), expected_payload)

			http_err = urllib.error.HTTPError(
				"http://localhost:8000/api/method/ism.api.board.post_board_capture",
				417,
				"Expectation Failed",
				{},
				io.BytesIO(json.dumps({"exception": "Repair Order is closed."}).encode("utf-8")),
			)
			with patch("urllib.request.urlopen", side_effect=http_err):
				out_buf = io.StringIO()
				err_buf = io.StringIO()
				with redirect_stdout(out_buf), redirect_stderr(err_buf):
					rc = cli.main(["--json", "board-post", "--repair-order", "RO-2026-00011", "--text", "Hi"])
			self.assertEqual(rc, 1)
			err_json = json.loads(out_buf.getvalue())
			self.assertFalse(err_json["success"])
			self.assertIn("Repair Order is closed.", err_json["error"])


if __name__ == "__main__":
	unittest.main()

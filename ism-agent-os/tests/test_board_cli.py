"""Tests for Shop Board OS client methods, CLI parsers, validation, and --stage/--dry-run gating."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
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

	def test_all_five_board_methods_route_and_strip_none(self) -> None:
		cases = [
			(
				lambda: self.client.list_board_threads(
					thread_type="REPAIR_ORDER",
					status="BLOCKED_DECISION",
					assigned_only=True,
					query="RO-2026",
					limit=25,
				),
				"ism.api.board.list_board_threads",
				{
					"thread_type": "REPAIR_ORDER",
					"status": "BLOCKED_DECISION",
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
					thread_key="SHOP:INTAKE",
					text="Model 3 front brake noise",
					intent_tag="DIAGNOSIS",
					metadata={"source": "cli"},
					idempotency_key="idem-1",
				),
				"ism.api.board.post_board_capture",
				{
					"thread_key": "SHOP:INTAKE",
					"text": "Model 3 front brake noise",
					"intent_tag": "DIAGNOSIS",
					"metadata": {"source": "cli"},
					"idempotency_key": "idem-1",
				},
			),
			(
				lambda: self.client.resolve_board_card(
					event_name="BE-00042",
					decision="approve",
					selected_option="opt_1",
					edited_message="Updated SMS",
					form_patch={"notes": "ok"},
					notes="Manager approved",
					idempotency_key="idem-2",
				),
				"ism.api.board.resolve_board_card",
				{
					"event_name": "BE-00042",
					"decision": "approve",
					"selected_option": "opt_1",
					"edited_message": "Updated SMS",
					"form_patch": {"notes": "ok"},
					"notes": "Manager approved",
					"idempotency_key": "idem-2",
				},
			),
			(
				lambda: self.client.undo_board_receipt(
					event_name="BE-00043",
					reason="Wrong side",
					idempotency_key="idem-3",
				),
				"ism.api.board.undo_board_receipt",
				{
					"event_name": "BE-00043",
					"reason": "Wrong side",
					"idempotency_key": "idem-3",
				},
			),
		]

		for call_fn, expected_method, expected_payload in cases:
			with self.subTest(method=expected_method):
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
			"_server_messages": [json.dumps({"message": "Only managers can resolve APPROVAL_GATE cards."})]
		}).encode("utf-8")
		http_err = urllib.error.HTTPError(
			"http://localhost:8000/api/method/ism.api.board.resolve_board_card",
			403,
			"Forbidden",
			{},
			io.BytesIO(err_payload),
		)
		with patch("urllib.request.urlopen", side_effect=http_err):
			with self.assertRaises(IsmApiError) as ctx:
				self.client.resolve_board_card(event_name="BE-00042", decision="approve")

		self.assertEqual(ctx.exception.status_code, 403)
		self.assertIn("Only managers can resolve APPROVAL_GATE cards.", str(ctx.exception))


class TestBoardCliParsingAndGating(unittest.TestCase):
	def setUp(self) -> None:
		self.env_vars = {
			"FRAPPE_URL": "http://localhost:8000",
			"FRAPPE_API_KEY": "k1",
			"FRAPPE_API_SECRET": "s1",
		}

	def test_read_only_and_write_metadata_classification(self) -> None:
		self.assertIn("board-threads", READ_ONLY_COMMANDS)
		self.assertIn("board-thread", READ_ONLY_COMMANDS)
		for write_cmd in ("board-post", "board-resolve", "board-undo"):
			self.assertNotIn(write_cmd, READ_ONLY_COMMANDS)
			self.assertIn(write_cmd, WRITE_COMMAND_META)

	def test_thread_key_and_repair_order_mutual_exclusion_and_required(self) -> None:
		parser = _build_parser(relax_required=False)
		with redirect_stderr(io.StringIO()):
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-thread"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-thread", "--thread-key", "RO:RO-1", "--repair-order", "RO-1"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-post", "--text", "hello"])
			with self.assertRaises(SystemExit):
				parser.parse_args(["board-post", "--thread-key", "RO:RO-1", "--repair-order", "RO-1", "--text", "hello"])

		ns_tk = parser.parse_args(["board-thread", "--thread-key", "SHOP:INTAKE", "--limit", "20"])
		self.assertEqual(ns_tk.thread_key, "SHOP:INTAKE")
		self.assertIsNone(ns_tk.repair_order)
		self.assertEqual(ns_tk.limit, 20)

		ns_ro = parser.parse_args(["board-post", "--repair-order", "RO-2026-00011", "--text", "Pad wear 2mm", "--intent", "INSPECTION"])
		self.assertEqual(ns_ro.repair_order, "RO-2026-00011")
		self.assertIsNone(ns_ro.thread_key)
		self.assertEqual(ns_ro.text, "Pad wear 2mm")
		self.assertEqual(ns_ro.intent, "INSPECTION")

		# Direct Namespace validation also rejects mutually exclusive targets (e.g. batch dict or stage).
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
			for argv in (
				["--json", "--dry-run", "board-post", "--thread-key", "SHOP:DISPATCH", "--text", "Bay 2 clear"],
				["--json", "--dry-run", "board-resolve", "--event", "BE-001", "--decision", "clarify", "--option", "FL"],
				["--json", "--dry-run", "board-undo", "--event", "BE-002", "--reason", "Duplicate"],
			):
				buf = io.StringIO()
				with redirect_stdout(buf):
					rc = cli.main(argv)
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

	def test_cli_stage_gating_for_board_writes_without_network(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			staged_dir = Path(tmp) / "staged"
			with (
				patch.dict("os.environ", {**self.env_vars, "ISM_STAGE_DIR": str(staged_dir)}, clear=False),
				patch("tools.cli.ensure_server_running", return_value={"port": 54321, "pid": 999}),
				patch("urllib.request.urlopen") as mock_open,
			):
				buf = io.StringIO()
				with redirect_stdout(buf):
					rc = cli.main(["--stage", "board-post", "--text", "Needs parts check"])
				self.assertEqual(rc, 0)
				out = json.loads(buf.getvalue())
				self.assertTrue(out["staged"])
				self.assertFalse(out["ready"])
				self.assertIn("board-post:--thread-key/--repair-order (one required)", out["missing_required"])
				self.assertEqual(mock_open.call_count, 0)

		spec = build_stage_form_spec([
			argparse.Namespace(
				command="board-resolve",
				event="BE-009",
				decision="approve",
				option=None,
				edited_message="Your Model 3 is ready.",
				notes=None,
				idempotency_key=None,
			)
		])
		self.assertTrue(spec["ready"])
		self.assertTrue(spec["has_writes"])
		self.assertEqual(spec["steps"][0]["target"], "ism.api.board.resolve_board_card -> Shop Board Event")

	def test_cli_main_executes_all_board_commands_and_propagates_errors(self) -> None:
		with patch.dict("os.environ", self.env_vars, clear=False):
			for argv, expected_method in (
				(["--json", "board-threads", "--thread-type", "REPAIR_ORDER", "--assigned-only"], "ism.api.board.list_board_threads"),
				(["--json", "board-thread", "--repair-order", "RO-2026-00011"], "ism.api.board.get_board_thread"),
				(["--json", "board-post", "--thread-key", "RO:RO-2026-00011", "--text", "Done"], "ism.api.board.post_board_capture"),
				(["--json", "board-resolve", "--event", "BE-001", "--decision", "reject", "--notes", "Wait"], "ism.api.board.resolve_board_card"),
				(["--json", "board-undo", "--event", "BE-002", "--reason", "Undo"], "ism.api.board.undo_board_receipt"),
			):
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
					self.assertEqual(
						mock_open.call_args[0][0].full_url,
						f"http://localhost:8000/api/method/{expected_method}",
					)

			http_err = urllib.error.HTTPError(
				"http://localhost:8000/api/method/ism.api.board.undo_board_receipt",
				417,
				"Expectation Failed",
				{},
				io.BytesIO(json.dumps({"exception": "Undo window expired."}).encode("utf-8")),
			)
			with patch("urllib.request.urlopen", side_effect=http_err):
				out_buf = io.StringIO()
				err_buf = io.StringIO()
				with redirect_stdout(out_buf), redirect_stderr(err_buf):
					rc = cli.main(["--json", "board-undo", "--event", "BE-002"])
			self.assertEqual(rc, 1)
			err_json = json.loads(out_buf.getvalue())
			self.assertFalse(err_json["success"])
			self.assertIn("Undo window expired.", err_json["error"])


if __name__ == "__main__":
	unittest.main()

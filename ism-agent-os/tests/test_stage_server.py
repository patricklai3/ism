"""Unit tests for the localhost staging daemon, security guards, and idempotent submit lifecycle."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from tools.cli_parsers import _build_parser
from tools.cli_validation import build_stage_form_spec
from tools.client import IsmApiError
from tools.stage_server import (
	StageHTTPServer,
	StageRequestHandler,
	ensure_server_running,
	start_stage_server,
)
from tools.stage_store import (
	create_stage_record,
	load_stage_record,
	read_latest_receipt,
	write_server_info,
)


def _make_stage(
	staged_dir: str,
	cmd_argvs: list[list[str]],
	*,
	mode: str = "single",
	ttl_seconds: int = 3600,
) -> dict[str, Any]:
	parser = _build_parser(relax_required=True)
	parsed = [parser.parse_args(argv) for argv in cmd_argvs]
	spec = build_stage_form_spec(parsed, parser)
	return create_stage_record(
		spec,
		mode=mode,
		ttl_seconds=ttl_seconds,
		staged_dir=staged_dir,
	)


class _FakeClient:
	def __init__(self, handler_fn) -> None:
		self._handler_fn = handler_fn

	def __enter__(self) -> _FakeClient:
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		return None


class TestStageServer(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp_ctx = tempfile.TemporaryDirectory()
		self.staged_dir = self.tmp_ctx.name
		self.server: StageHTTPServer = start_stage_server(self.staged_dir, port=0, idle_timeout=60)
		self.port = self.server.server_port
		self.thread = threading.Thread(
			target=self.server.serve_forever,
			kwargs={"poll_interval": 0.05},
			daemon=True,
		)
		self.thread.start()

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=2)
		self.tmp_ctx.cleanup()

	def _request(
		self,
		method: str,
		path: str,
		*,
		body: Optional[dict[str, Any] | str] = None,
		headers: Optional[dict[str, str]] = None,
	) -> tuple[int, dict[str, str], str]:
		conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
		try:
			hdrs = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
			if headers:
				hdrs.update(headers)
			raw_body: Optional[bytes] = None
			if body is not None:
				if isinstance(body, str):
					raw_body = body.encode("utf-8")
				else:
					raw_body = json.dumps(body).encode("utf-8")
				hdrs.setdefault("Content-Type", "application/json")
			conn.request(method, path, body=raw_body, headers=hdrs)
			resp = conn.getresponse()
			text = resp.read().decode("utf-8", errors="replace")
			resp_headers = {k.lower(): v for k, v in resp.getheaders()}
			return (resp.status, resp_headers, text)
		finally:
			conn.close()

	def test_security_host_origin_nonce_and_ttl_guards(self) -> None:
		rec = _make_stage(
			self.staged_dir,
			[["quote", "--customer", "Rob", "--vehicle", "5YJ3E1EA1PF200101", "--service", "Brake::1.0"]],
		)
		sid = rec["stage_id"]
		nonce = rec["nonce"]

		# /health reports matching staged_dir
		st, _, raw = self._request("GET", "/health")
		self.assertEqual(st, 200)
		self.assertEqual(Path(json.loads(raw)["staged_dir"]).resolve(), Path(self.staged_dir).resolve())

		# Bad Host header -> 403
		st, _, _ = self._request("GET", "/health", headers={"Host": "evil.example.com"})
		self.assertEqual(st, 403)

		# Wrong token on GET / and GET /api/stage/<id> -> 403
		st, _, _ = self._request("GET", f"/?id={sid}&token=badtoken")
		self.assertEqual(st, 403)
		st, _, _ = self._request("GET", f"/api/stage/{sid}", headers={"X-Stage-Token": "badtoken"})
		self.assertEqual(st, 403)

		# Valid GET / returns HTML without leaking nonce
		st, hdrs, html_body = self._request("GET", f"/?id={sid}&token={nonce}")
		self.assertEqual(st, 200)
		self.assertIn("text/html", hdrs.get("content-type", ""))
		self.assertIn("ISM Staged Action Review", html_body)
		self.assertNotIn(nonce, html_body)

		# Valid GET /api/stage/<id> omits nonce from payload
		st, _, api_raw = self._request("GET", f"/api/stage/{sid}", headers={"X-Stage-Token": nonce})
		self.assertEqual(st, 200)
		api_json = json.loads(api_raw)
		self.assertNotIn("nonce", api_json)
		self.assertEqual(api_json["stage_id"], sid)
		self.assertEqual(api_json["status"], "pending")

		# Cross-origin Origin and Sec-Fetch-Site: cross-site rejected on POST -> 403
		st, _, _ = self._request(
			"POST",
			f"/api/stage/{sid}/submit",
			body={"odometer_in": 48250},
			headers={"X-Stage-Token": nonce, "Origin": "https://attacker.example"},
		)
		self.assertEqual(st, 403)
		st, _, _ = self._request(
			"POST",
			f"/api/stage/{sid}/submit",
			body={"odometer_in": 48250},
			headers={"X-Stage-Token": nonce, "Sec-Fetch-Site": "cross-site"},
		)
		self.assertEqual(st, 403)
		st, _, _ = self._request(
			"POST",
			f"/api/stage/{sid}/submit",
			body={"odometer_in": 48250},
			headers={"X-Stage-Token": "wrong-nonce", "Origin": f"http://127.0.0.1:{self.port}"},
		)
		self.assertEqual(st, 403)

		# Expired TTL returns 410 and transitions record to expired
		expired_rec = _make_stage(
			self.staged_dir,
			[["quote", "--customer", "Rob", "--vehicle", "VIN1", "--odometer-in", "100", "--notes", "Test"]],
			ttl_seconds=1,
		)
		time.sleep(1.1)
		st, _, exp_raw = self._request(
			"POST",
			f"/api/stage/{expired_rec['stage_id']}/submit",
			body={},
			headers={"X-Stage-Token": expired_rec["nonce"], "Origin": f"http://127.0.0.1:{self.port}"},
		)
		self.assertEqual(st, 410)
		self.assertEqual(json.loads(exp_raw)["status"], "expired")

	def test_validation_keeps_pending_then_submit_writes_receipt(self) -> None:
		rec = _make_stage(
			self.staged_dir,
			[["quote", "--customer", "Rob", "--vehicle", "5YJ3E1EA1PF200101", "--service", "Lateral Link::1.0"]],
		)
		sid = rec["stage_id"]
		nonce = rec["nonce"]
		same_origin = {"X-Stage-Token": nonce, "Origin": f"http://127.0.0.1:{self.port}"}

		captured: list[dict[str, Any]] = []

		def _fake_quote_handler(a, _c, _s):
			captured.append({"customer": a.customer, "vehicle": a.vehicle, "odometer_in": a.odometer_in})
			return {
				"success": True,
				"data": {"name": "RO-2026-00101", "grand_total": 169.5, "status": "Draft Estimate"},
				"urls": {
					"desk": "http://localhost:8000/app/repair-order/RO-2026-00101",
					"print": "http://localhost:8000/printview?name=RO-2026-00101",
				},
			}

		with patch("tools.stage_server.IsmAgentClient", return_value=_FakeClient(None)), \
			patch.dict("tools.stage_server._COMMAND_HANDLERS", {"quote": _fake_quote_handler}):
			# Missing odometer_in -> 400 and stays pending
			st, _, raw_400 = self._request(
				"POST", f"/api/stage/{sid}/submit", body={"steps": [{"values": {}}]}, headers=same_origin
			)
			self.assertEqual(st, 400)
			self.assertEqual(json.loads(raw_400)["status"], "pending")
			self.assertEqual(load_stage_record(sid, staged_dir=self.staged_dir)["status"], "pending")
			self.assertEqual(captured, [])

			# Negative odometer_in -> 400 and stays pending
			st, _, raw_neg = self._request(
				"POST",
				f"/api/stage/{sid}/submit",
				body={"steps": [{"values": {"odometer_in": -5}}]},
				headers=same_origin,
			)
			self.assertEqual(st, 400)
			self.assertEqual(load_stage_record(sid, staged_dir=self.staged_dir)["status"], "pending")
			self.assertEqual(captured, [])

			# Valid submit -> 200, writes latest.json, status=submitted
			st, _, raw_200 = self._request(
				"POST",
				f"/api/stage/{sid}/submit",
				body={"steps": [{"values": {"odometer_in": "48250"}}]},
				headers=same_origin,
			)
			self.assertEqual(st, 200, raw_200)
			ok_json = json.loads(raw_200)
			self.assertTrue(ok_json["success"])
			self.assertEqual(ok_json["status"], "submitted")
			self.assertEqual(ok_json["receipt"]["ids"]["repair_order"], "RO-2026-00101")
			self.assertEqual(captured[0]["odometer_in"], 48250)

		latest = read_latest_receipt(staged_dir=self.staged_dir)
		self.assertEqual(latest["stage_id"], sid)
		self.assertEqual(latest["data"]["primary_id"], "RO-2026-00101")
		self.assertEqual(load_stage_record(sid, staged_dir=self.staged_dir)["status"], "submitted")

	def test_double_submit_and_concurrent_submit_lock_return_409(self) -> None:
		rec = _make_stage(
			self.staged_dir,
			[["quote", "--customer", "Rob", "--vehicle", "VIN1", "--odometer-in", "48250", "--notes", "Check"]],
		)
		sid = rec["stage_id"]
		same_origin = {"X-Stage-Token": rec["nonce"], "Origin": f"http://127.0.0.1:{self.port}"}

		call_count = 0
		count_lock = threading.Lock()
		entered = threading.Event()

		def _slow_quote(_a, _c, _s):
			nonlocal call_count
			with count_lock:
				call_count += 1
			entered.set()
			time.sleep(0.18)
			return {"success": True, "data": {"name": "RO-2026-00222"}}

		barrier = threading.Barrier(2)
		statuses: list[int] = []
		statuses_lock = threading.Lock()

		def _worker() -> None:
			barrier.wait()
			st, _, _ = self._request(
				"POST", f"/api/stage/{sid}/submit", body={}, headers=same_origin
			)
			with statuses_lock:
				statuses.append(st)

		with patch("tools.stage_server.IsmAgentClient", return_value=_FakeClient(None)), \
			patch.dict("tools.stage_server._COMMAND_HANDLERS", {"quote": _slow_quote}):
			t1 = threading.Thread(target=_worker)
			t2 = threading.Thread(target=_worker)
			t1.start()
			t2.start()
			t1.join(timeout=5)
			t2.join(timeout=5)

			# Sequential third submit also gets 409
			st3, _, raw3 = self._request(
				"POST", f"/api/stage/{sid}/submit", body={}, headers=same_origin
			)

		self.assertEqual(sorted(statuses), [200, 409])
		self.assertEqual(st3, 409)
		self.assertEqual(json.loads(raw3)["status"], "submitted")
		self.assertEqual(call_count, 1)

	def test_chained_batch_and_partial_conflict_idempotent_retry(self) -> None:
		rec = _make_stage(
			self.staged_dir,
			[
				["quote", "--customer", "Rob", "--vehicle", "VIN1", "--odometer-in", "48250", "--service", "Brake::1.0"],
				["book", "--repair-order", "$step0.data.name", "--date", "2026-11-20", "--time", "09:00"],
				["send-approval", "--repair-order", "$step0.data.name", "--medium", "link"],
			],
			mode="batch",
		)
		sid = rec["stage_id"]
		same_origin = {"X-Stage-Token": rec["nonce"], "Origin": f"http://127.0.0.1:{self.port}"}

		counts = {"quote": 0, "book": 0, "send-approval": 0}
		booked_args: list[dict[str, Any]] = []
		approval_args: list[dict[str, Any]] = []

		def _fake_quote(_a, _c, _s):
			counts["quote"] += 1
			return {
				"success": True,
				"data": {"name": "RO-2026-00555"},
				"urls": {"desk": "http://localhost:8000/app/repair-order/RO-2026-00555"},
			}

		def _fake_book(a, _c, _s):
			counts["book"] += 1
			booked_args.append({"repair_order": a.repair_order, "date": a.date, "time": a.time, "bay": a.bay})
			if a.time == "09:00":
				return {
					"success": False,
					"error": "Bay 1 is occupied at 09:00",
					"data": {
						"status": "conflict",
						"conflict": True,
						"alternative_slots": [
							{"date": "2026-11-20", "start_time": "11:00:00", "service_bay": "Bay 2"}
						],
					},
				}
			return {
				"success": True,
				"data": {"name": "SE-00088", "scheduled_date": a.date, "scheduled_time": a.time},
				"urls": {"calendar": "http://localhost:8000/app/schedule-entry/view/calendar"},
			}

		def _fake_send(a, _c, _s):
			counts["send-approval"] += 1
			approval_args.append({"repair_order": a.repair_order, "medium": a.medium})
			return {
				"success": True,
				"data": {"name": a.repair_order},
				"urls": {"approval": "http://localhost:8000/quote-approval?token=xyz"},
			}

		handlers_patch = {
			"quote": _fake_quote,
			"book": _fake_book,
			"send-approval": _fake_send,
		}
		with patch("tools.stage_server.IsmAgentClient", return_value=_FakeClient(None)), \
			patch.dict("tools.stage_server._COMMAND_HANDLERS", handlers_patch):
			# First submit: Step 0 succeeds, Step 1 hits conflict -> 409, status reverts to pending
			st1, _, raw1 = self._request("POST", f"/api/stage/{sid}/submit", body={}, headers=same_origin)
			self.assertEqual(st1, 409, raw1)
			r1 = json.loads(raw1)
			self.assertTrue(r1["conflict"])
			self.assertEqual(r1["status"], "pending")
			self.assertEqual(r1["failed_step"], 1)
			self.assertEqual(r1["alternatives"][0]["start_time"], "11:00:00")
			self.assertTrue(r1["commands"][0]["completed"])
			self.assertEqual(counts, {"quote": 1, "book": 1, "send-approval": 0})

			# Retry with alternative slot: Step 0 MUST NOT re-execute!
			st2, _, raw2 = self._request(
				"POST",
				f"/api/stage/{sid}/submit",
				body={
					"steps": [
						{"values": {}},
						{"values": {"date": "2026-11-20", "time": "11:00:00", "bay": "Bay 2"}},
						{"values": {}},
					]
				},
				headers=same_origin,
			)
			self.assertEqual(st2, 200, raw2)
			r2 = json.loads(raw2)
			self.assertTrue(r2["success"])
			self.assertEqual(counts, {"quote": 1, "book": 2, "send-approval": 1})
			self.assertEqual(booked_args[1]["repair_order"], "RO-2026-00555")
			self.assertEqual(approval_args[0]["repair_order"], "RO-2026-00555")
			self.assertEqual(r2["receipt"]["ids"]["repair_order"], "RO-2026-00555")
			self.assertEqual(r2["receipt"]["ids"]["schedule_entry"], "SE-00088")

	def test_417_ambiguous_bay_conflict_passthrough(self) -> None:
		rec = _make_stage(
			self.staged_dir,
			[["book", "--repair-order", "RO-1", "--date", "2026-11-20", "--time", "10:00", "--bay", "Bay"]],
		)
		sid = rec["stage_id"]
		same_origin = {"X-Stage-Token": rec["nonce"], "Origin": f"http://127.0.0.1:{self.port}"}

		def _fake_book(a, _c, _s):
			if a.bay == "Bay":
				raise IsmApiError(
					"Ambiguous Service Bay 'Bay'. Matching bays: Bay 1 [Lift] [Available], Bay 2 [Busy].",
					status_code=417,
				)
			return {"success": True, "data": {"name": "SE-00099"}}

		with patch("tools.stage_server.IsmAgentClient", return_value=_FakeClient(None)), \
			patch.dict("tools.stage_server._COMMAND_HANDLERS", {"book": _fake_book}):
			st1, _, raw1 = self._request("POST", f"/api/stage/{sid}/submit", body={}, headers=same_origin)
			self.assertEqual(st1, 417)
			r1 = json.loads(raw1)
			self.assertTrue(r1["conflict"])
			self.assertEqual(r1["available_slots"][0]["bay"], "Bay 1")

			st2, _, raw2 = self._request(
				"POST",
				f"/api/stage/{sid}/submit",
				body={"steps": [{"values": {"bay": "Bay 1"}}]},
				headers=same_origin,
			)
			self.assertEqual(st2, 200, raw2)
			self.assertEqual(json.loads(raw2)["receipt"]["ids"]["schedule_entry"], "SE-00099")

	def test_ensure_server_running_reuses_healthy_daemon_via_server_json(self) -> None:
		write_server_info(
			self.staged_dir,
			{"pid": os.getpid(), "host": "127.0.0.1", "port": self.port, "staged_dir": self.staged_dir},
		)
		with patch("tools.stage_server.subprocess.Popen", side_effect=AssertionError("Should not spawn")) as mock_popen:
			info = ensure_server_running(self.staged_dir)
		mock_popen.assert_not_called()
		self.assertEqual(info["port"], self.port)
		self.assertTrue(info["ok"])


if __name__ == "__main__":
	unittest.main()

"""Zero-dependency 127.0.0.1 HTTP daemon for staged ISM write confirmation and execution."""

from __future__ import annotations

import argparse
import copy
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

if not __package__:
	_BOOTSTRAP_ROOT = str(Path(__file__).resolve().parent.parent)
	if _BOOTSTRAP_ROOT not in sys.path:
		sys.path.insert(0, _BOOTSTRAP_ROOT)
	__package__ = "tools"

from .cli_commands import (
	_COMMAND_HANDLERS,
	_dict_args_to_argv,
	_parse_batch_subcommand,
	resolve_at_last_value,
	resolve_step_tokens,
	validate_batch_step_references,
)
from .cli_parsers import _build_parser
from .cli_parsing import _loads_json_with_ps_fallback
from .cli_validation import (
	READ_ONLY_COMMANDS,
	_contains_step_ref,
	_has_value,
	validate_temporal_args,
	validate_write_command_args,
)
from .client import IsmAgentClient, IsmApiError
from .cockpit_routes import handle_cockpit_get, handle_cockpit_post, load_cockpit_html
from .config import IsmConfig
from .stage_store import (
	load_stage_record,
	read_server_info,
	record_receipt,
	remove_server_info,
	resolve_staged_dir,
	update_stage_record,
	validate_stage_id,
	write_server_info,
)

DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
_MAX_POST_BODY_BYTES = 1_048_576
_AGENT_OS_ROOT = Path(__file__).resolve().parent.parent
_UI_HTML_PATH = Path(__file__).resolve().with_name("stage_ui.html")
_VALID_HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost)(?::\d+)?$", re.IGNORECASE)
_AVAILABLE_BAY_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9 _-]*?)(?:\s*\[[^\]]+\])*\s*\[Available\]")


def _load_ui_html() -> str:
	if _UI_HTML_PATH.is_file():
		return _UI_HTML_PATH.read_text(encoding="utf-8")
	return "<!doctype html><html><body><h1>ISM Staged Action UI Missing</h1></body></html>"


def _coerce_submitted_value(raw: Any, field_spec: dict[str, Any]) -> Any:
	ftype = field_spec.get("type", "text")
	is_multi = bool(field_spec.get("multi"))

	if is_multi:
		if raw is None:
			return None
		if isinstance(raw, (list, tuple)):
			items = [str(x).strip() for x in raw if str(x).strip()]
			return items or None
		text = str(raw)
		items = [line.strip() for line in text.splitlines() if line.strip()]
		return items or None

	if ftype == "bool":
		if isinstance(raw, bool):
			return raw
		if isinstance(raw, str):
			low = raw.strip().lower()
			if low in ("1", "true", "yes", "on"):
				return True
			if low in ("0", "false", "no", "off", ""):
				return False
		return bool(raw)

	if raw is None:
		return None
	if isinstance(raw, str):
		stripped = raw.strip()
		if not stripped:
			return None
		if ftype == "int":
			if _contains_step_ref(stripped) or "@last" in stripped:
				return stripped
			try:
				return int(stripped)
			except ValueError:
				return stripped
		if ftype == "float":
			if _contains_step_ref(stripped) or "@last" in stripped:
				return stripped
			try:
				return float(stripped)
			except ValueError:
				return stripped
		if ftype == "json":
			return stripped
		return stripped

	if ftype == "json" and isinstance(raw, (dict, list)):
		return json.dumps(raw)
	if ftype == "int" and not isinstance(raw, bool):
		try:
			return int(raw)
		except (TypeError, ValueError):
			return raw
	if ftype == "float" and not isinstance(raw, bool):
		try:
			return float(raw)
		except (TypeError, ValueError):
			return raw
	return raw


def _normalize_slot_item(slot: Any) -> Optional[dict[str, Any]]:
	if not isinstance(slot, dict):
		return None
	date_val = slot.get("date") or slot.get("scheduled_date")
	time_val = slot.get("start_time") or slot.get("time") or slot.get("scheduled_time")
	bay_val = slot.get("service_bay") or slot.get("bay")
	tech_val = slot.get("technician") or slot.get("assigned_technician") or slot.get("tech")
	parts: list[str] = []
	if date_val:
		parts.append(str(date_val))
	if time_val:
		parts.append(str(time_val))
	if bay_val:
		parts.append(f"Bay: {bay_val}")
	if tech_val:
		parts.append(f"Tech: {tech_val}")
	if not parts:
		return None
	return {
		"date": date_val,
		"start_time": time_val,
		"time": time_val,
		"service_bay": bay_val,
		"bay": bay_val,
		"technician": tech_val,
		"tech": tech_val,
		"label": slot.get("label") or " · ".join(parts),
	}


def _extract_conflict_info(
	res: Any = None,
	err_msg: str = "",
	details_raw: Any = None,
) -> tuple[bool, list[dict[str, Any]]]:
	"""Extract (is_conflict, normalized_alternatives) from Frappe envelope or error details."""
	candidates: list[Any] = []
	is_conflict = False

	def _inspect_dict(obj: Any) -> None:
		nonlocal is_conflict
		if not isinstance(obj, dict):
			return
		if obj.get("conflict") is True or obj.get("status") == "conflict":
			is_conflict = True
		for key in ("alternative_slots", "alternatives", "available_slots"):
			seq = obj.get(key)
			if isinstance(seq, list):
				candidates.extend(seq)
		if isinstance(obj.get("data"), dict):
			_inspect_dict(obj["data"])
		if isinstance(obj.get("message"), dict):
			_inspect_dict(obj["message"])

	_inspect_dict(res)
	if isinstance(details_raw, dict):
		_inspect_dict(details_raw)
	elif isinstance(details_raw, str) and details_raw.strip().startswith("{"):
		try:
			_inspect_dict(json.loads(details_raw))
		except json.JSONDecodeError:
			pass

	normalized: list[dict[str, Any]] = []
	seen: set[tuple[Any, Any, Any, Any]] = set()
	for raw_slot in candidates:
		norm = _normalize_slot_item(raw_slot)
		if norm:
			key = (norm["date"], norm["time"], norm["bay"], norm["tech"])
			if key not in seen:
				seen.add(key)
				normalized.append(norm)

	# Handle 417 ambiguous-bay candidate messages: "... Matching bays: Bay 1 [Lift] [Available], ..."
	if err_msg and "Ambiguous Service Bay" in err_msg:
		is_conflict = True
		for match in _AVAILABLE_BAY_RE.finditer(err_msg):
			bay_name = match.group(1).strip(" ,.")
			if bay_name:
				key = (None, None, bay_name, None)
				if key not in seen:
					seen.add(key)
					normalized.append({
						"date": None,
						"start_time": None,
						"time": None,
						"service_bay": bay_name,
						"bay": bay_name,
						"technician": None,
						"tech": None,
						"label": f"Bay: {bay_name} [Available]",
					})

	if normalized:
		is_conflict = True
	return (is_conflict, normalized)


def _refresh_step_field_flags(
	step: dict[str, Any],
	missing: list[str],
	*,
	step_error: Optional[str] = None,
) -> None:
	values = step.get("values") or {}
	for fspec in step.get("fields") or []:
		fname = fspec["name"]
		flag = fspec.get("flag") or f"--{fname.replace('_', '-')}"
		val = values.get(fname)
		is_missing_field = any(
			m in (flag, fname) or m.startswith((f"{flag} ", f"{fname} "))
			for m in missing
		)
		has_val = _has_value(val) or val == 0
		fspec["value"] = val
		fspec["invalid"] = bool(is_missing_field and has_val)
		fspec["missing"] = bool(is_missing_field and not has_val)
		fspec["is_step_ref"] = _contains_step_ref(val)
	for grp in step.get("any_of") or []:
		choices = grp.get("fields") or []
		grp["satisfied"] = any(_has_value(values.get(f)) for f in choices)
	step["ready"] = len(missing) == 0 and step_error is None
	step["missing_required"] = list(missing)


class StageHTTPServer(ThreadingHTTPServer):
	"""Threaded localhost HTTP server managing staged action forms and locks."""

	daemon_threads = True
	allow_reuse_address = os.name != "nt"

	def __init__(
		self,
		server_address: tuple[str, int],
		RequestHandlerClass: type[BaseHTTPRequestHandler],
		*,
		staged_dir: Optional[str | Path] = None,
		idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
		cockpit_token: Optional[str] = None,
		env_file: Optional[str] = None,
	) -> None:
		self.staged_dir = resolve_staged_dir(staged_dir)
		self.idle_timeout = float(idle_timeout)
		self.last_activity = time.monotonic()
		self._locks_guard = threading.Lock()
		self._stage_locks: dict[str, threading.Lock] = {}
		self._active_executions = 0
		self.ui_html = _load_ui_html()
		self.cockpit_html = load_cockpit_html()
		self.cockpit_token = str(cockpit_token or secrets.token_urlsafe(24))
		self.env_file = env_file
		super().__init__(server_address, RequestHandlerClass)

	def touch(self) -> None:
		self.last_activity = time.monotonic()

	def get_stage_lock(self, stage_id: str) -> threading.Lock:
		with self._locks_guard:
			lock = self._stage_locks.get(stage_id)
			if lock is None:
				lock = threading.Lock()
				self._stage_locks[stage_id] = lock
			return lock

	def begin_execution(self) -> None:
		with self._locks_guard:
			self._active_executions += 1

	def end_execution(self) -> None:
		with self._locks_guard:
			self._active_executions = max(0, self._active_executions - 1)

	@property
	def active_executions(self) -> int:
		with self._locks_guard:
			return self._active_executions


class StageRequestHandler(BaseHTTPRequestHandler):
	"""HTTP request handler enforcing localhost origin, Host, nonce, and lock guards."""

	protocol_version = "HTTP/1.1"
	server: StageHTTPServer

	def log_message(self, format: str, *args: Any) -> None:
		if os.environ.get("ISM_STAGE_DEBUG", "").strip() == "1":
			super().log_message(format, *args)

	def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
		body = json.dumps(payload, default=str).encode("utf-8")
		self.send_response(status_code)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _send_html(self, status_code: int, html_text: str) -> None:
		body = html_text.encode("utf-8")
		self.send_response(status_code)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Frame-Options", "DENY")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header(
			"Content-Security-Policy",
			"default-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
		)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _check_host(self) -> bool:
		host_hdr = (self.headers.get("Host") or "").strip()
		if not host_hdr or not _VALID_HOST_RE.fullmatch(host_hdr):
			self._send_json(403, {"success": False, "error": "Forbidden Host header."})
			return False
		parsed = urlsplit(f"http://{host_hdr}")
		if parsed.port is not None and parsed.port != self.server.server_port:
			self._send_json(403, {"success": False, "error": "Host port mismatch."})
			return False
		return True

	def _check_post_origin(self) -> bool:
		sec_fetch = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
		if sec_fetch and sec_fetch not in ("same-origin", "none"):
			self._send_json(403, {"success": False, "error": "Cross-site POST rejected."})
			return False
		origin = (self.headers.get("Origin") or "").strip()
		if origin:
			if origin.lower() == "null":
				self._send_json(403, {"success": False, "error": "Null Origin rejected."})
				return False
			parsed = urlsplit(origin)
			effective_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
			if (
				parsed.scheme.lower() != "http"
				or (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost")
				or effective_port != self.server.server_port
				or parsed.username is not None
				or parsed.password is not None
			):
				self._send_json(403, {"success": False, "error": "Cross-origin POST rejected."})
				return False
		return True

	@staticmethod
	def _verify_nonce(record: dict[str, Any], provided_token: Optional[str]) -> bool:
		expected = str(record.get("nonce") or "")
		if not expected or not provided_token:
			return False
		return hmac.compare_digest(provided_token.encode("utf-8"), expected.encode("utf-8"))

	@staticmethod
	def _sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
		sanitized = {k: v for k, v in record.items() if k != "nonce"}
		steps: list[dict[str, Any]] = []
		for step in sanitized.get("commands") or []:
			if isinstance(step, dict):
				clean_step = {k: v for k, v in step.items() if k != "completed_result"}
				clean_step["completed"] = step.get("completed_result") is not None
				steps.append(clean_step)
		sanitized["commands"] = steps
		return sanitized

	def do_GET(self) -> None:
		self.server.touch()
		if not self._check_host():
			return

		parsed = urlsplit(self.path)
		query = parse_qs(parsed.query)

		if parsed.path == "/health":
			self._send_json(200, {
				"ok": True,
				"cockpit": True,
				"pid": os.getpid(),
				"port": self.server.server_port,
				"staged_dir": str(self.server.staged_dir),
			})
			return

		if handle_cockpit_get(self, parsed, query):
			return

		if parsed.path == "/":
			raw_id = (query.get("id") or [""])[0].strip()
			token = (query.get("token") or [""])[0].strip()
			try:
				stage_id = validate_stage_id(raw_id)
			except ValueError:
				self._send_json(400, {"success": False, "error": "Invalid or missing stage id."})
				return
			record = load_stage_record(stage_id, staged_dir=self.server.staged_dir)
			if record is None:
				self._send_json(404, {"success": False, "error": f"Staged action '{stage_id}' not found."})
				return
			if not self._verify_nonce(record, token):
				self._send_json(403, {"success": False, "error": "Invalid stage token."})
				return
			self._send_html(200, self.server.ui_html)
			return

		if parsed.path.startswith("/api/stage/") and not parsed.path.endswith("/submit"):
			raw_id = parsed.path[len("/api/stage/") :].strip("/")
			try:
				stage_id = validate_stage_id(raw_id)
			except ValueError:
				self._send_json(400, {"success": False, "error": "Invalid stage id."})
				return
			token = (self.headers.get("X-Stage-Token") or (query.get("token") or [""])[0]).strip()
			lock = self.server.get_stage_lock(stage_id)
			with lock:
				record = load_stage_record(stage_id, staged_dir=self.server.staged_dir)
				if record is None:
					self._send_json(404, {"success": False, "error": f"Staged action '{stage_id}' not found."})
					return
				if not self._verify_nonce(record, token):
					self._send_json(403, {"success": False, "error": "Invalid stage token."})
					return
				if record.get("status") == "pending" and time.time() > float(record.get("expires_at") or 0):
					record = update_stage_record(
						stage_id, {"status": "expired"}, staged_dir=self.server.staged_dir
					)
				sanitized = self._sanitize_record(record)
			self._send_json(200, {"success": True, **sanitized})
			return

		self._send_json(404, {"success": False, "error": "Not found."})

	def do_POST(self) -> None:
		self.server.touch()
		if not self._check_host():
			return
		if not self._check_post_origin():
			return

		parsed = urlsplit(self.path)
		query = parse_qs(parsed.query)
		if handle_cockpit_post(self, parsed, query):
			return

		if not (parsed.path.startswith("/api/stage/") and parsed.path.endswith("/submit")):
			self._send_json(404, {"success": False, "error": "Not found."})
			return

		raw_id = parsed.path[len("/api/stage/") : -len("/submit")].strip("/")
		try:
			stage_id = validate_stage_id(raw_id)
		except ValueError:
			self._send_json(400, {"success": False, "error": "Invalid stage id."})
			return

		raw_len = self.headers.get("Content-Length")
		try:
			content_len = int(raw_len or "0")
		except ValueError:
			self._send_json(400, {"success": False, "error": "Invalid Content-Length."})
			return
		if content_len < 0 or content_len > _MAX_POST_BODY_BYTES:
			self._send_json(413, {"success": False, "error": "Request body too large."})
			return

		raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"
		try:
			body = json.loads(raw_body.decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError):
			self._send_json(400, {"success": False, "error": "Malformed JSON request body."})
			return
		if not isinstance(body, dict):
			self._send_json(400, {"success": False, "error": "Request body must be a JSON object."})
			return

		query = parse_qs(parsed.query)
		token = (
			self.headers.get("X-Stage-Token")
			or body.get("token")
			or body.get("nonce")
			or (query.get("token") or [""])[0]
		)
		token = str(token or "").strip()

		lock = self.server.get_stage_lock(stage_id)
		with lock:
			record = load_stage_record(stage_id, staged_dir=self.server.staged_dir)
			if record is None:
				self._send_json(404, {"success": False, "error": f"Staged action '{stage_id}' not found."})
				return
			if not self._verify_nonce(record, token):
				self._send_json(403, {"success": False, "error": "Invalid stage token."})
				return

			status = record.get("status")
			if status in ("executing", "submitted"):
				self._send_json(409, {
					"success": False,
					"status": status,
					"error": f"Staged action '{stage_id}' was already {status}.",
					"receipt": record.get("receipt"),
				})
				return

			if status == "expired" or time.time() > float(record.get("expires_at") or 0):
				update_stage_record(stage_id, {"status": "expired"}, staged_dir=self.server.staged_dir)
				self._send_json(410, {
					"success": False,
					"status": "expired",
					"error": "Staged action has expired.",
				})
				return

			commands = copy.deepcopy(record.get("commands") or [])
			mode = str(record.get("mode") or ("batch" if len(commands) > 1 else "single"))
			self._merge_submitted_values(commands, body, mode=mode)
			all_missing, step_errors = self._preflight_validate_steps(commands, mode=mode)
			if all_missing or step_errors:
				err_msg = (
					f"Missing or invalid required field(s): {', '.join(all_missing)}"
					if all_missing
					else str(step_errors[0]["error"])
				)
				update_stage_record(
					stage_id,
					{"status": "pending", "commands": commands, "last_error": err_msg},
					staged_dir=self.server.staged_dir,
				)
				self._send_json(400, {
					"success": False,
					"status": "pending",
					"error": err_msg,
					"missing_required": all_missing,
					"step_errors": step_errors,
					"commands": self._sanitize_record({"commands": commands})["commands"],
				})
				return

			update_stage_record(
				stage_id,
				{"status": "executing", "commands": commands, "last_error": None},
				staged_dir=self.server.staged_dir,
			)

		self.server.begin_execution()
		try:
			http_status, resp_payload = self._execute_staged_commands(
				stage_id=stage_id,
				record=record,
				commands=commands,
				mode=mode,
				lock=lock,
			)
		finally:
			self.server.end_execution()

		self._send_json(http_status, resp_payload)

	@staticmethod
	def _merge_submitted_values(commands: list[dict[str, Any]], body: dict[str, Any], *, mode: str) -> None:
		submitted_steps: Optional[list[Any]] = None
		if isinstance(body.get("steps"), list):
			submitted_steps = body["steps"]
		elif mode == "single" or len(commands) == 1:
			if isinstance(body.get("values"), dict):
				submitted_steps = [body["values"]]
			else:
				direct = {k: v for k, v in body.items() if k not in ("token", "nonce")}
				if direct:
					submitted_steps = [direct]

		if not submitted_steps:
			return

		for idx, step in enumerate(commands):
			if step.get("completed_result") is not None:
				continue
			if idx >= len(submitted_steps):
				break
			raw_step = submitted_steps[idx]
			if not isinstance(raw_step, dict):
				continue
			raw_vals = raw_step.get("values") if isinstance(raw_step.get("values"), dict) else raw_step
			values = step.setdefault("values", {})
			field_map = {f["name"]: f for f in (step.get("fields") or []) if isinstance(f, dict)}
			for fname, raw_val in raw_vals.items():
				if fname in field_map:
					coerced = _coerce_submitted_value(raw_val, field_map[fname])
					values[fname] = coerced
					field_map[fname]["value"] = coerced

	def _preflight_validate_steps(
		self,
		commands: list[dict[str, Any]],
		*,
		mode: str,
	) -> tuple[list[str], list[dict[str, Any]]]:
		parser = _build_parser(relax_required=True)
		all_missing: list[str] = []
		step_errors: list[dict[str, Any]] = []

		try:
			validate_batch_step_references(
				[s.get("values") or {} for s in commands],
				is_batch=(mode == "batch"),
			)
		except ValueError as exc:
			step_errors.append({"index": 0, "command": commands[0].get("command") if commands else None, "error": str(exc)})
			return (all_missing, step_errors)

		for idx, step in enumerate(commands):
			if step.get("completed_result") is not None:
				_refresh_step_field_flags(step, [])
				continue

			cmd = step["command"]
			values = copy.deepcopy(step.get("values") or {})
			try:
				values = resolve_at_last_value(values, staged_dir=self.server.staged_dir)
			except ValueError as exc:
				_refresh_step_field_flags(step, [], step_error=str(exc))
				step_errors.append({"index": idx, "command": cmd, "error": str(exc)})
				continue

			json_err: Optional[str] = None
			for fspec in step.get("fields") or []:
				if fspec.get("type") != "json":
					continue
				raw_json_val = values.get(fspec["name"])
				if isinstance(raw_json_val, str) and raw_json_val.strip():
					if _contains_step_ref(raw_json_val) or raw_json_val.strip().startswith("@"):
						continue
					try:
						_loads_json_with_ps_fallback(raw_json_val)
					except json.JSONDecodeError as exc:
						json_err = f"{fspec.get('flag', fspec['name'])}: invalid JSON ({exc.msg})"
						break

			if json_err:
				_refresh_step_field_flags(step, [json_err], step_error=json_err)
				all_missing.append(f"{cmd}:{json_err}")
				step_errors.append({"index": idx, "command": cmd, "error": json_err})
				continue

			argv = _dict_args_to_argv(cmd, values)
			try:
				_, ns = _parse_batch_subcommand(argv, parser, suppress_stderr=True)
			except ValueError as exc:
				_refresh_step_field_flags(step, [], step_error=str(exc))
				step_errors.append({"index": idx, "command": cmd, "error": str(exc)})
				continue

			allow_refs = mode == "batch" and idx > 0
			missing = validate_write_command_args(ns, allow_step_refs=allow_refs)
			if cmd in READ_ONLY_COMMANDS:
				missing.extend(validate_temporal_args(ns, allow_step_refs=allow_refs))
			_refresh_step_field_flags(step, missing)
			if missing:
				all_missing.extend(f"{cmd}:{m}" for m in missing)
				step_errors.append({
					"index": idx,
					"command": cmd,
					"error": f"Missing or invalid required field(s) for '{cmd}': {', '.join(missing)}.",
				})

		return (all_missing, step_errors)

	def _execute_staged_commands(
		self,
		*,
		stage_id: str,
		record: dict[str, Any],
		commands: list[dict[str, Any]],
		mode: str,
		lock: threading.Lock,
	) -> tuple[int, dict[str, Any]]:
		parser_strict = _build_parser(relax_required=False)
		config = IsmConfig.from_env(env_path=record.get("env_file"))
		cmd_names = [str(s["command"]) for s in commands]

		failed_step: Optional[int] = None
		err_msg = ""
		err_status = 500
		is_conflict = False
		alternatives: list[dict[str, Any]] = []
		err_result: Any = None
		results: list[dict[str, Any]] = []

		try:
			with IsmAgentClient(config) as client:
				for idx, step in enumerate(commands):
					if step.get("completed_result") is not None:
						results.append({
							"index": idx,
							"command": step["command"],
							"ok": True,
							"result": step["completed_result"],
						})
						continue

					cmd = step["command"]
					resolved_vals = resolve_at_last_value(
						copy.deepcopy(step.get("values") or {}),
						staged_dir=self.server.staged_dir,
					)
					if mode == "batch":
						resolved_vals = resolve_step_tokens(resolved_vals, idx, results)

					argv = _dict_args_to_argv(cmd, resolved_vals)
					_, ns = _parse_batch_subcommand(argv, parser_strict, suppress_stderr=True)
					missing = validate_write_command_args(ns, allow_step_refs=False)
					if cmd in READ_ONLY_COMMANDS:
						missing.extend(validate_temporal_args(ns, allow_step_refs=False))
					if missing:
						raise ValueError(
							f"Missing or invalid required field(s) for '{cmd}': {', '.join(missing)}."
						)

					handler = _COMMAND_HANDLERS.get(cmd)
					if not handler:
						raise ValueError(f"Unknown staged command '{cmd}'.")

					handler_client = None if cmd in ("price-part", "stage-receipt") else client
					res = handler(ns, handler_client, False)
					is_ok = not (isinstance(res, dict) and res.get("success") is False)
					if not is_ok:
						failed_step = idx
						err_result = res
						err_msg = str(
							res.get("error")
							or res.get("message")
							or f"Command '{cmd}' failed."
						)
						is_conflict, alternatives = _extract_conflict_info(res=res, err_msg=err_msg)
						err_status = 409 if is_conflict else 400
						break

					step["completed_result"] = res
					results.append({
						"index": idx,
						"command": cmd,
						"ok": True,
						"result": res,
					})
		except IsmApiError as exc:
			failed_step = len(results)
			err_msg = str(exc)
			is_conflict, alternatives = _extract_conflict_info(err_msg=err_msg, details_raw=exc.details)
			if exc.status_code == 409:
				is_conflict = True
			err_status = exc.status_code if 400 <= exc.status_code < 600 else 502
			err_result = exc.details
		except (ValueError, json.JSONDecodeError, OSError) as exc:
			failed_step = len(results)
			err_msg = str(exc)
			err_status = 400

		if failed_step is not None:
			with lock:
				update_stage_record(
					stage_id,
					{"status": "pending", "commands": commands, "last_error": err_msg},
					staged_dir=self.server.staged_dir,
				)
			return (
				err_status,
				{
					"success": False,
					"status": "pending",
					"conflict": is_conflict,
					"error": err_msg,
					"alternatives": alternatives,
					"available_slots": alternatives,
					"failed_step": failed_step,
					"result": err_result,
					"commands": self._sanitize_record({"commands": commands})["commands"],
				},
			)

		if mode == "batch":
			execution_result: dict[str, Any] = {
				"success": True,
				"total": len(commands),
				"succeeded": len(results),
				"failed": 0,
				"results": results,
			}
		else:
			execution_result = results[0]["result"] if results else {"success": True}

		with lock:
			update_stage_record(
				stage_id,
				{"commands": commands},
				staged_dir=self.server.staged_dir,
			)
			receipt = record_receipt(
				stage_id,
				execution_result,
				commands=cmd_names,
				mode=mode,
				staged_dir=self.server.staged_dir,
			)

		return (
			200,
			{
				"success": True,
				"status": "submitted",
				"stage_id": stage_id,
				"receipt": receipt,
				"result": execution_result,
			},
		)


def start_stage_server(
	staged_dir: Optional[str | Path] = None,
	*,
	port: int = 0,
	idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
	cockpit_token: Optional[str] = None,
	env_file: Optional[str] = None,
) -> StageHTTPServer:
	"""Create a bound StageHTTPServer on 127.0.0.1."""
	resolved_dir = resolve_staged_dir(staged_dir)
	resolved_dir.mkdir(parents=True, exist_ok=True)
	return StageHTTPServer(
		("127.0.0.1", int(port)),
		StageRequestHandler,
		staged_dir=resolved_dir,
		idle_timeout=float(idle_timeout),
		cockpit_token=cockpit_token,
		env_file=env_file,
	)


def ping_stage_server(staged_dir: Optional[str | Path] = None, timeout: float = 0.5) -> Optional[dict[str, Any]]:
	"""Ping an existing localhost staging daemon directly, bypassing proxy env vars."""
	resolved_dir = resolve_staged_dir(staged_dir)
	info = read_server_info(resolved_dir)
	if not info or not info.get("port"):
		return None
	try:
		port = int(info["port"])
	except (TypeError, ValueError):
		return None
	if port <= 0 or port > 65535:
		return None

	conn: Optional[http.client.HTTPConnection] = None
	try:
		conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
		conn.request("GET", "/health", headers={"Host": f"127.0.0.1:{port}", "Connection": "close"})
		resp = conn.getresponse()
		raw = resp.read().decode("utf-8", errors="replace")
		if resp.status != 200:
			return None
		payload = json.loads(raw)
		if not isinstance(payload, dict) or not payload.get("ok"):
			return None
		payload_dir = payload.get("staged_dir")
		if payload_dir and Path(payload_dir).resolve() != resolved_dir.resolve():
			return None
		return {**info, **payload, "port": port}
	except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError):
		return None
	finally:
		if conn is not None:
			try:
				conn.close()
			except OSError:
				pass


def ensure_server_running(
	staged_dir: Optional[str | Path] = None,
	*,
	idle_timeout: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
	startup_timeout: float = 4.0,
	require_cockpit: bool = False,
	env_file: Optional[str] = None,
) -> dict[str, Any]:
	"""Return a running localhost stage daemon info dict, spawning one detached if needed."""
	resolved_dir = resolve_staged_dir(staged_dir)
	resolved_dir.mkdir(parents=True, exist_ok=True)
	existing = ping_stage_server(resolved_dir)
	if existing is not None:
		has_cockpit = bool(existing.get("cockpit") and existing.get("cockpit_token"))
		if not require_cockpit or has_cockpit:
			return existing
		old_pid = existing.get("pid")
		if old_pid and int(old_pid) != os.getpid():
			try:
				os.kill(int(old_pid), signal.SIGTERM)
			except OSError:
				pass
			time.sleep(0.2)

	stage_module = f"{__package__}.stage_server" if __package__ else "tools.stage_server"
	cmd = [
		sys.executable,
		"-m",
		stage_module,
		"--staged-dir",
		str(resolved_dir),
		"--idle-timeout",
		str(int(idle_timeout)),
	]
	if env_file:
		cmd.extend(["--env-file", str(env_file)])
	popen_kwargs: dict[str, Any] = {
		"cwd": str(_AGENT_OS_ROOT),
		"stdin": subprocess.DEVNULL,
		"stdout": subprocess.DEVNULL,
		"stderr": subprocess.DEVNULL,
		"close_fds": True,
	}
	if os.name == "nt":
		popen_kwargs["creationflags"] = (
			getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
			| getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
			| getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
		)
	else:
		popen_kwargs["start_new_session"] = True

	proc = subprocess.Popen(cmd, **popen_kwargs)
	deadline = time.monotonic() + float(startup_timeout)
	while time.monotonic() < deadline:
		health = ping_stage_server(resolved_dir, timeout=0.3)
		if health is not None:
			return health
		if proc.poll() is not None:
			raise RuntimeError(
				f"Local staging server exited prematurely with code {proc.returncode}."
			)
		time.sleep(0.025)

	raise RuntimeError(f"Timed out waiting for local staging server in {resolved_dir}.")


def main(argv: Optional[list[str]] = None) -> int:
	"""Run the localhost staging daemon until idle timeout or termination signal."""
	ap = argparse.ArgumentParser(description="ISM local staged-action HTTP daemon")
	ap.add_argument("--staged-dir", help="Path to .ism_staged directory")
	ap.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (0 for ephemeral)")
	ap.add_argument(
		"--idle-timeout",
		type=float,
		default=float(DEFAULT_IDLE_TIMEOUT_SECONDS),
		help="Idle shutdown timeout in seconds (default 1800)",
	)
	ap.add_argument("--env-file", help="Optional .env path for cockpit Frappe REST calls")
	opts = ap.parse_args(argv)

	server = start_stage_server(
		opts.staged_dir,
		port=opts.port,
		idle_timeout=opts.idle_timeout,
		env_file=opts.env_file,
	)
	info = {
		"pid": os.getpid(),
		"host": "127.0.0.1",
		"port": server.server_port,
		"staged_dir": str(server.staged_dir),
		"cockpit_token": server.cockpit_token,
		"env_file": opts.env_file,
		"started_at": time.time(),
	}
	write_server_info(server.staged_dir, info)

	stop_event = threading.Event()

	def _request_shutdown(_signum: Any = None, _frame: Any = None) -> None:
		stop_event.set()
		threading.Thread(target=server.shutdown, daemon=True).start()

	for sig_name in ("SIGINT", "SIGTERM"):
		sig = getattr(signal, sig_name, None)
		if sig is not None:
			try:
				signal.signal(sig, _request_shutdown)
			except (ValueError, OSError):
				pass

	def _idle_watchdog() -> None:
		while not stop_event.wait(1.0):
			idle_for = time.monotonic() - server.last_activity
			if idle_for >= server.idle_timeout and server.active_executions == 0:
				_request_shutdown()
				break

	threading.Thread(target=_idle_watchdog, name="stage-idle-watchdog", daemon=True).start()

	try:
		server.serve_forever(poll_interval=0.5)
	finally:
		stop_event.set()
		server.server_close()
		remove_server_info(server.staged_dir, expected_pid=os.getpid())
	return 0


if __name__ == "__main__":
	sys.exit(main())

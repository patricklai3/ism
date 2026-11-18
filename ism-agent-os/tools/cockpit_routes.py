"""HTTP route handlers for the local ISM Monitoring Cockpit."""

from __future__ import annotations

import hmac
import json
from datetime import date
from pathlib import Path
from typing import Any, Optional
from urllib.parse import SplitResult

from .client import IsmAgentClient, IsmApiError
from .config import IsmConfig
from .monitoring import build_cockpit_view

_COCKPIT_HTML_PATH = Path(__file__).resolve().with_name("cockpit_ui.html")
_MAX_POST_BODY_BYTES = 1_048_576


def load_cockpit_html() -> str:
	"""Load the bundled single-screen cockpit HTML template."""
	return _COCKPIT_HTML_PATH.read_text(encoding="utf-8")


def _extract_token(
	handler: Any,
	query: dict[str, list[str]],
	body: Optional[dict[str, Any]] = None,
) -> str:
	hdr = (handler.headers.get("X-Cockpit-Token") or "").strip()
	if hdr:
		return hdr
	if isinstance(body, dict) and body.get("token"):
		return str(body.get("token") or "").strip()
	return str((query.get("token") or [""])[0] or "").strip()


def _verify_cockpit_token(
	handler: Any,
	query: dict[str, list[str]],
	body: Optional[dict[str, Any]] = None,
) -> bool:
	expected = str(getattr(handler.server, "cockpit_token", "") or "")
	provided = _extract_token(handler, query, body)
	if not expected or not provided:
		handler._send_json(403, {"success": False, "error": "Invalid or missing cockpit token."})
		return False
	if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
		handler._send_json(403, {"success": False, "error": "Invalid or missing cockpit token."})
		return False
	return True


def _load_client_config(handler: Any) -> IsmConfig:
	env_file = getattr(handler.server, "env_file", None)
	return IsmConfig.from_env(env_path=env_file)


def handle_cockpit_get(handler: Any, parsed: SplitResult, query: dict[str, list[str]]) -> bool:
	"""Handle GET /cockpit and GET /api/cockpit/snapshot. Returns True if route matched."""
	if parsed.path == "/cockpit":
		if not _verify_cockpit_token(handler, query):
			return True
		html_text = getattr(handler.server, "cockpit_html", None) or load_cockpit_html()
		handler._send_html(200, html_text)
		return True

	if parsed.path == "/api/cockpit/snapshot":
		if not _verify_cockpit_token(handler, query):
			return True

		raw_date = (query.get("date") or [""])[0].strip() or None
		if raw_date is not None:
			try:
				date.fromisoformat(raw_date)
			except ValueError:
				handler._send_json(400, {"success": False, "error": f"Invalid date '{raw_date}'. Expected YYYY-MM-DD."})
				return True

		try:
			config = _load_client_config(handler)
			with IsmAgentClient(config) as client:
				snap = client.get_cockpit_snapshot(date=raw_date)
		except (IsmApiError, OSError, ValueError) as exc:
			handler._send_json(502, {"success": False, "error": str(exc)})
			return True

		if not isinstance(snap, dict) or snap.get("success") is False:
			err_msg = (snap.get("error") or snap.get("message")) if isinstance(snap, dict) else "Invalid snapshot response."
			handler._send_json(502, {"success": False, "error": str(err_msg)})
			return True

		view = build_cockpit_view(snap)
		handler._send_json(200, {"success": True, **view})
		return True

	return False


def handle_cockpit_post(handler: Any, parsed: SplitResult, query: dict[str, list[str]]) -> bool:
	"""Handle POST /api/cockpit/action-sync. Returns True if route matched."""
	if parsed.path != "/api/cockpit/action-sync":
		return False

	raw_len = handler.headers.get("Content-Length")
	try:
		content_len = int(raw_len or "0")
	except ValueError:
		handler._send_json(400, {"success": False, "error": "Invalid Content-Length."})
		return True
	if content_len < 0 or content_len > _MAX_POST_BODY_BYTES:
		handler._send_json(413, {"success": False, "error": "Request body too large."})
		return True

	raw_body = handler.rfile.read(content_len) if content_len > 0 else b"{}"
	try:
		body = json.loads(raw_body.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		handler._send_json(400, {"success": False, "error": "Malformed JSON request body."})
		return True
	if not isinstance(body, dict):
		handler._send_json(400, {"success": False, "error": "Request body must be a JSON object."})
		return True

	if not _verify_cockpit_token(handler, query, body):
		return True

	dedup_key = str(body.get("dedup_key") or "").strip()
	action = str(body.get("action") or "").strip().lower()
	if not dedup_key:
		handler._send_json(400, {"success": False, "error": "dedup_key is required."})
		return True
	if action not in ("snooze", "dismiss", "reopen"):
		handler._send_json(400, {"success": False, "error": "action must be snooze, dismiss, or reopen."})
		return True

	snooze_raw = body.get("snooze_minutes")
	snooze_minutes = None
	if snooze_raw is not None and snooze_raw != "":
		if isinstance(snooze_raw, bool):
			handler._send_json(400, {"success": False, "error": "snooze_minutes must be an integer."})
			return True
		try:
			snooze_minutes = int(snooze_raw)
		except (TypeError, ValueError):
			handler._send_json(400, {"success": False, "error": "snooze_minutes must be an integer."})
			return True
		if snooze_minutes < 1 or snooze_minutes > 43200:
			handler._send_json(400, {"success": False, "error": "snooze_minutes must be between 1 and 43200."})
			return True

	title = str(body.get("title")).strip() if body.get("title") is not None else None
	notes = str(body.get("notes")).strip() if body.get("notes") is not None else None
	ref_dt = str(body.get("reference_doctype")).strip() if body.get("reference_doctype") is not None else None
	ref_nm = str(body.get("reference_name")).strip() if body.get("reference_name") is not None else None

	try:
		config = _load_client_config(handler)
		with IsmAgentClient(config) as client:
			res = client.sync_action_item_state(
				dedup_key=dedup_key,
				action=action,
				snooze_minutes=snooze_minutes,
				title=title or None,
				notes=notes or None,
				reference_doctype=ref_dt or None,
				reference_name=ref_nm or None,
			)
	except (IsmApiError, OSError, ValueError) as exc:
		handler._send_json(502, {"success": False, "error": str(exc)})
		return True

	handler._send_json(200, res if isinstance(res, dict) else {"success": True, "data": res})
	return True

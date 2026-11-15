#!/usr/bin/env python3
"""Command-line interface for ISM Agent OS operations over direct Frappe REST."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import shlex
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

if __package__:
	from .client import IsmAgentClient, IsmApiError
	from .config import IsmConfig
	from .diagnostics import run_diagnostics
	try:
		from .ingestion.supplier_catalog import SupplierCatalogSanitizer
		from .ingestion.tesla_manual_parser import TeslaManualParser
	except ImportError:  # Uninstalled source checkout running `python -m tools.cli`
		_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
		if _REPO_ROOT not in sys.path:
			sys.path.insert(0, _REPO_ROOT)
		from ingestion.supplier_catalog import SupplierCatalogSanitizer
		from ingestion.tesla_manual_parser import TeslaManualParser
else:  # Direct script execution (`python tools/cli.py`)
	_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
	if _REPO_ROOT not in sys.path:
		sys.path.insert(0, _REPO_ROOT)
	from ingestion.supplier_catalog import SupplierCatalogSanitizer
	from ingestion.tesla_manual_parser import TeslaManualParser
	from tools.client import IsmAgentClient, IsmApiError
	from tools.config import IsmConfig
	from tools.diagnostics import run_diagnostics


READ_ONLY_COMMANDS = frozenset({
	"test",
	"verify-connection",
	"status",
	"tech-overview",
	"slots",
	"estimate",
	"unscheduled",
	"check-stock",
	"alternates",
	"fetch-tesla",
	"decode-vin",
	"search-vehicles",
	"vehicle-info",
	"inspection-templates",
	"get-inspection",
	"deferred",
	"action-items",
	"list-docs",
	"get-doc",
})

WRITE_COMMAND_META: dict[str, dict[str, Any]] = {
	"quote": {
		"target": "ism.api.agent.create_quote -> Repair Order (Draft Estimate)",
		"fields": ("customer", "vehicle", "odometer_in", "service", "services_json", "parts_json", "notes", "phone", "email", "company"),
	},
	"intake-slots": {
		"target": "ism.api.agent.intake_and_find_slots -> Repair Order (Draft Estimate) + Slots",
		"fields": ("customer", "vehicle", "odometer_in", "service", "services_json", "parts_json", "notes", "phone", "email", "date", "days", "duration", "tags", "bay", "technician"),
	},
	"create-ro-from-entry": {
		"target": "ism.api.agent.create_ro_from_appointment -> Repair Order (Checked In)",
		"fields": ("schedule_entry", "odometer_in"),
	},
	"amend-quote": {
		"target": "ism.api.agent.amend_quote -> Repair Order",
		"fields": ("repair_order", "add_service", "add_services_json", "remove_indices_json", "notes"),
	},
	"send-approval": {
		"target": "ism.api.agent.send_quote_for_approval -> Approval Dispatch",
		"fields": ("repair_order", "medium", "phone", "recipient"),
	},
	"book": {
		"target": "ism.api.agent.book_appointment -> Schedule Entry",
		"fields": ("repair_order", "date", "time", "bay", "tech", "entry_type", "duration", "provisional_customer", "provisional_vehicle", "notes"),
	},
	"reschedule": {
		"target": "ism.api.agent.reschedule_appointment -> Schedule Entry",
		"fields": ("schedule_entry", "date", "time", "duration", "bay", "tech", "notes"),
	},
	"appointment-status": {
		"target": "ism.api.agent.update_appointment_status -> Schedule Entry",
		"fields": ("schedule_entry", "status", "notes"),
	},
	"job-status": {
		"target": "ism.api.agent.update_job_status -> Repair Order",
		"fields": ("repair_order", "action", "odometer_out", "notes"),
	},
	"checkout": {
		"target": "ism.api.agent.checkout_repair_order -> Sales Invoice + Payment + Release",
		"fields": ("repair_order", "payment_method", "odometer_out", "paid_amount", "confirm"),
	},
	"toggle-service": {
		"target": "ism.api.agent.toggle_service_completion -> Repair Order Service",
		"fields": ("repair_order", "service_row", "service_rows_json", "reopen"),
	},
	"add-supplement": {
		"target": "ism.api.agent.add_supplement -> Repair Order Supplement",
		"fields": ("repair_order", "title", "category", "hours", "rate", "parts_json", "notes"),
	},
	"duplicate-side": {
		"target": "ism.api.agent.duplicate_service_for_opposite_side -> Repair Order Service",
		"fields": ("repair_order", "service_row"),
	},
	"add-deferred": {
		"target": "ism.api.agent.add_deferred_services -> Repair Order Service",
		"fields": ("repair_order", "item", "items_json"),
	},
	"create-action": {
		"target": "ism.api.agent.create_shop_action_item -> Shop Action Item",
		"fields": ("title", "category", "priority", "ref_doctype", "ref_name", "item_code", "details"),
	},
	"resolve-action": {
		"target": "ism.api.agent.resolve_shop_action_item -> Shop Action Item",
		"fields": ("action_item", "notes"),
	},
	"apply-inspection": {
		"target": "ism.api.agent.apply_inspection_template -> Repair Order Inspection",
		"fields": ("repair_order", "template", "replace"),
	},
	"record-inspection": {
		"target": "ism.api.agent.record_check_in_inspection -> Repair Order Inspection",
		"fields": ("repair_order", "odometer_in", "concern", "template", "auto_apply", "items_json"),
	},
	"procure": {
		"target": "ism.api.agent.submit_procurement_orders -> Purchase Order / RFQ",
		"fields": ("repair_order", "parts_json", "submit"),
	},
	"receive-po": {
		"target": "ism.api.agent.receive_purchase_order -> Purchase Receipt",
		"fields": ("purchase_order", "submit_po"),
	},
	"ingest-parts": {
		"target": "SupplierCatalogSanitizer + import_supplier_catalog -> Item",
		"fields": ("file", "supplier", "source", "condition", "service_package"),
	},
	"import-tesla": {
		"target": "ism.api.agent.import_tesla_procedure -> Service Package / Repair Order",
		"fields": ("url", "data_json", "title", "code", "hours", "model", "parts_json", "repair_order", "on_conflict", "force", "side", "supplement", "notes"),
	},
	"ingest-tesla": {
		"target": "TeslaManualParser.ingest_from_url -> Service Package / Repair Order",
		"fields": ("url", "model", "repair_order", "on_conflict", "force", "side"),
	},
	"link-vehicle-customer": {
		"target": "ism.api.agent.link_vehicle_customer -> Repair Vehicle",
		"fields": ("vehicle", "customer", "primary", "relationship"),
	},
	"create-doc": {
		"target": "REST POST /api/resource/<DocType>",
		"fields": ("doctype", "data_json"),
	},
	"update-doc": {
		"target": "REST PUT /api/resource/<DocType>/<Name>",
		"fields": ("doctype", "name", "data_json"),
	},
	"call-method": {
		"target": "REST POST /api/method/<method>",
		"fields": ("method", "args_json"),
	},
}


def _has_value(val: Any) -> bool:
	if val is None or val is False:
		return False
	if isinstance(val, str):
		return bool(val.strip())
	if isinstance(val, (list, tuple, dict, set)):
		return len(val) > 0
	return True


def _check_non_negative_int_arg(args: argparse.Namespace, attr: str, flag_name: str, missing: list[str]) -> None:
	val = getattr(args, attr, None)
	if val is None:
		missing.append(flag_name)
	elif isinstance(val, bool) or int(val) < 0:
		missing.append(f"{flag_name} (must be >= 0)")


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_RE = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")


def _is_valid_iso_date(val: Any) -> bool:
	if not isinstance(val, str):
		return False
	text = val.strip()
	if not _ISO_DATE_RE.match(text):
		return False
	try:
		date.fromisoformat(text)
		return True
	except ValueError:
		return False


def _is_valid_iso_time(val: Any) -> bool:
	if not isinstance(val, str):
		return False
	text = val.strip()
	if not _ISO_TIME_RE.match(text):
		return False
	parts = text.split(":")
	hour = int(parts[0])
	minute = int(parts[1])
	second = int(parts[2]) if len(parts) == 3 else 0
	return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def validate_temporal_args(args: argparse.Namespace) -> list[str]:
	"""Return invalid date/time flag messages for any CLI command."""
	errors: list[str] = []
	date_val = getattr(args, "date", None)
	if _has_value(date_val) and not _is_valid_iso_date(date_val):
		errors.append("--date (must be ISO YYYY-MM-DD)")

	time_val = getattr(args, "time", None)
	if _has_value(time_val) and not _is_valid_iso_time(time_val):
		errors.append("--time (must be 24-hour HH:MM or HH:MM:SS)")

	min_time_val = getattr(args, "min_time", None)
	if _has_value(min_time_val) and not _is_valid_iso_time(min_time_val):
		errors.append("--min-time (must be 24-hour HH:MM or HH:MM:SS)")
	return errors


def validate_write_command_args(args: argparse.Namespace) -> list[str]:
	"""Return a list of missing or invalid required flags for write CLI commands."""
	cmd = getattr(args, "command", None)
	if not cmd or cmd in READ_ONLY_COMMANDS or cmd == "batch":
		return []

	missing: list[str] = []
	if cmd in ("quote", "intake-slots"):
		if not _has_value(getattr(args, "customer", None)):
			missing.append("--customer")
		if not _has_value(getattr(args, "vehicle", None)):
			missing.append("--vehicle")
		_check_non_negative_int_arg(args, "odometer_in", "--odometer-in", missing)
		if not any(
			_has_value(getattr(args, f, None))
			for f in ("service", "services_json", "notes")
		):
			missing.append("--service/--services-json/--notes (at least one required)")
	elif cmd == "create-ro-from-entry":
		if not _has_value(getattr(args, "schedule_entry", None)):
			missing.append("--schedule-entry")
		_check_non_negative_int_arg(args, "odometer_in", "--odometer-in", missing)
	elif cmd == "amend-quote":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		if not any(
			_has_value(getattr(args, f, None))
			for f in ("add_service", "add_services_json", "remove_indices_json", "notes")
		):
			missing.append("--add-service/--add-services-json/--remove-indices-json/--notes (at least one required)")
	elif cmd == "send-approval":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		if not _has_value(getattr(args, "medium", None)):
			missing.append("--medium")
	elif cmd == "book":
		if not _has_value(getattr(args, "date", None)):
			missing.append("--date")
		if not _has_value(getattr(args, "time", None)):
			missing.append("--time")
	elif cmd == "reschedule":
		if not _has_value(getattr(args, "schedule_entry", None)):
			missing.append("--schedule-entry")
		if not any(
			_has_value(getattr(args, f, None))
			for f in ("date", "time", "duration", "bay", "tech", "notes")
		):
			missing.append("--date/--time/--duration/--bay/--tech/--notes (at least one change required)")
	elif cmd == "appointment-status":
		if not _has_value(getattr(args, "schedule_entry", None)):
			missing.append("--schedule-entry")
		if not _has_value(getattr(args, "status", None)):
			missing.append("--status")
	elif cmd == "job-status":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		action = getattr(args, "action", None)
		if not _has_value(action):
			missing.append("--action")
		elif action == "complete":
			_check_non_negative_int_arg(args, "odometer_out", "--odometer-out", missing)
	elif cmd == "checkout":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		if not _has_value(getattr(args, "payment_method", None)):
			missing.append("--payment-method")
		_check_non_negative_int_arg(args, "odometer_out", "--odometer-out", missing)
		if not bool(getattr(args, "confirm", False)):
			missing.append("--confirm")
	elif cmd == "toggle-service":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		if not _has_value(getattr(args, "service_row", None)) and not _has_value(
			getattr(args, "service_rows_json", None)
		):
			missing.append("--service-row or --service-rows-json")
	elif cmd == "add-deferred":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		if not _has_value(getattr(args, "item", None)) and not _has_value(getattr(args, "items_json", None)):
			missing.append("--item or --items-json")
	elif cmd == "record-inspection":
		if not _has_value(getattr(args, "repair_order", None)):
			missing.append("--repair-order")
		odo = getattr(args, "odometer_in", None)
		if odo is not None and (isinstance(odo, bool) or int(odo) < 0):
			missing.append("--odometer-in (must be >= 0)")
	missing.extend(validate_temporal_args(args))
	return missing


def build_pre_dispatch_summary(parsed_items: list[argparse.Namespace]) -> dict[str, Any]:
	"""Build a structured ISM Pre-Dispatch Summary envelope without executing HTTP writes."""
	item_summaries: list[dict[str, Any]] = []
	all_missing: list[str] = []
	has_writes = False
	md_lines: list[str] = ["### ISM Pre-Dispatch Summary"]

	for idx, sub_args in enumerate(parsed_items, start=1):
		cmd = getattr(sub_args, "command", "unknown")
		meta = WRITE_COMMAND_META.get(cmd)
		is_write = cmd not in READ_ONLY_COMMANDS
		if is_write:
			has_writes = True
		missing = validate_write_command_args(sub_args)
		if cmd in READ_ONLY_COMMANDS:
			missing.extend(validate_temporal_args(sub_args))
		all_missing.extend(f"{cmd}:{m}" for m in missing)

		fields_list = meta["fields"] if meta else tuple(
			k for k in vars(sub_args).keys() if k not in ("command", "env_file", "json", "summary", "dry_run")
		)
		payload: dict[str, Any] = {}
		field_lines: list[str] = []
		for field in fields_list:
			val = getattr(sub_args, field, None)
			flag = f"--{field.replace('_', '-')}"
			is_missing_field = any(m == flag or m.startswith(f"{flag} ") for m in missing)
			if is_missing_field:
				if _has_value(val) or val == 0:
					payload[field] = val
					field_lines.append(f"  - `{field}`: [INVALID: `{val}`]")
				else:
					payload[field] = None
					field_lines.append(f"  - `{field}`: [MISSING — REQUIRED]")
			elif _has_value(val) or val == 0:
				payload[field] = val
				field_lines.append(f"  - `{field}`: `{val}`")

		for m in missing:
			if not any(m == f"--{f.replace('_', '-')}" or m.startswith(f"--{f.replace('_', '-')} ") for f in fields_list):
				field_lines.append(f"  - `{m}`: [MISSING — REQUIRED]")

		target = meta["target"] if meta else f"Read-only command ({cmd})"
		status = "READY" if not missing else "BLOCKED — MISSING REQUIRED FIELDS"
		prefix = f"#### Step {idx}: " if len(parsed_items) > 1 else ""
		if prefix:
			md_lines.append(f"{prefix}`{cmd}`")
		else:
			md_lines.append(f"- **Command**: `{cmd}`")
		md_lines.append(f"- **Target**: {target}")
		md_lines.append(f"- **Status**: `{status}`")
		if field_lines:
			md_lines.append("- **Payload Fields**:")
			md_lines.extend(field_lines)
		md_lines.append(f"- **Missing Required Fields**: `{', '.join(missing) if missing else 'None'}`")

		item_summaries.append({
			"command": cmd,
			"is_write": is_write,
			"target": target,
			"ready": len(missing) == 0,
			"missing_required": missing,
			"payload": payload,
		})

	ready = len(all_missing) == 0
	if has_writes:
		next_step = (
			"Await explicit operator confirmation before dispatching live write command(s)."
			if ready
			else "Request the missing required field(s) above from the operator before dispatching."
		)
	else:
		next_step = "Read-only command(s) require no confirmation."
	md_lines.append(f"- **Confirm Next Step**: {next_step}")

	return {
		"success": True,
		"dry_run": True,
		"ready": ready,
		"has_writes": has_writes,
		"missing_required": all_missing,
		"commands": item_summaries,
		"summary_card": "\n".join(md_lines),
	}


def main(argv: Optional[list[str]] = None) -> int:
	"""Parse CLI arguments and execute the requested ISM Agent OS command."""
	parser = _build_parser()
	args = parser.parse_args(argv)
	dry_run = bool(getattr(args, "dry_run", False))

	if dry_run:
		try:
			if args.command == "batch":
				items = _load_batch_items(args)
				parsed_items: list[argparse.Namespace] = []
				for item in items:
					argv_item = _batch_item_to_argv(item)
					sub_args = parser.parse_args(argv_item)
					if sub_args.command in ("batch", "test", "verify-connection"):
						raise ValueError(f"Command '{sub_args.command}' is not allowed inside batch.")
					parsed_items.append(sub_args)
				res = build_pre_dispatch_summary(parsed_items)
			else:
				res = build_pre_dispatch_summary([args])
			_print_result(res, raw_json=args.json)
			return 0
		except (SystemExit, json.JSONDecodeError, OSError, ValueError) as exc:
			print(f"Error: {exc}", file=sys.stderr)
			return 1

	config = IsmConfig.from_env(env_path=args.env_file)

	if args.command in ("test", "verify-connection"):
		return 0 if run_diagnostics(config) else 1

	client = IsmAgentClient(config)
	show_summary = bool(args.summary and not args.json)
	try:
		if args.command == "batch":
			res = _cmd_batch(args, client, show_summary, parser)
			_print_result(res, raw_json=args.json)
			return 0 if res.get("success") else 1
		missing = validate_write_command_args(args)
		if args.command in READ_ONLY_COMMANDS:
			missing.extend(validate_temporal_args(args))
		if missing:
			raise ValueError(
				f"Missing or invalid required field(s) for '{args.command}': {', '.join(missing)}. "
				"Run with --dry-run to inspect and present the ISM Pre-Dispatch Summary before dispatching."
			)
		handler = _COMMAND_HANDLERS.get(args.command)
		if not handler:
			return 1
		res = handler(args, client, show_summary)
		_print_result(res, raw_json=args.json)
		if isinstance(res, dict) and res.get("success") is False:
			return 1
		return 0
	except (IsmApiError, json.JSONDecodeError, OSError, ValueError) as exc:
		print(f"Error: {exc}", file=sys.stderr)
		return 1


_JSON_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
_JSON_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _parse_unquoted_json_scalar(token: str) -> Any:
	"""Convert an unquoted scalar token from PowerShell-stripped JSON into a Python value."""
	if token == "true":
		return True
	if token == "false":
		return False
	if token == "null":
		return None
	if _JSON_INT_RE.fullmatch(token):
		return int(token)
	if _JSON_NUM_RE.fullmatch(token):
		return float(token)
	return token


def _parse_single_quoted(text: str, idx: int) -> tuple[str, int]:
	"""Parse a single-quoted string token starting at idx."""
	n = len(text)
	idx += 1
	chars: list[str] = []
	while idx < n:
		ch = text[idx]
		if ch == "\\" and idx + 1 < n:
			chars.append(text[idx + 1])
			idx += 2
			continue
		if ch == "'":
			return "".join(chars), idx + 1
		chars.append(ch)
		idx += 1
	raise ValueError("Unterminated single-quoted string")


def _skip_ws(text: str, idx: int) -> int:
	n = len(text)
	while idx < n and text[idx].isspace():
		idx += 1
	return idx


def _parse_unquoted_json_value(text: str, idx: int, stop_chars: tuple[str, ...] = ()) -> tuple[Any, int]:
	"""Recursively parse PowerShell-stripped JSON containers and unquoted scalars."""
	idx = _skip_ws(text, idx)
	n = len(text)
	if idx >= n:
		raise ValueError("Unexpected end of input")

	ch = text[idx]
	if ch == "{":
		idx = _skip_ws(text, idx + 1)
		obj: dict[str, Any] = {}
		if idx < n and text[idx] == "}":
			return obj, idx + 1
		while idx < n:
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == "'":
				key, idx = _parse_single_quoted(text, idx)
				idx = _skip_ws(text, idx)
				if idx >= n or text[idx] != ":":
					raise ValueError("Expected ':' after object key")
			else:
				colon = text.find(":", idx)
				if colon == -1:
					raise ValueError("Expected ':' in object entry")
				key = text[idx:colon].strip()
				if not key:
					raise ValueError("Empty object key")
				idx = colon
			idx += 1
			val, idx = _parse_unquoted_json_value(text, idx, stop_chars=(",", "}"))
			obj[key] = val
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == ",":
				idx += 1
				continue
			if idx < n and text[idx] == "}":
				return obj, idx + 1
			raise ValueError("Expected ',' or '}' in object")
		raise ValueError("Unterminated object")

	if ch == "[":
		idx = _skip_ws(text, idx + 1)
		arr: list[Any] = []
		if idx < n and text[idx] == "]":
			return arr, idx + 1
		while idx < n:
			val, idx = _parse_unquoted_json_value(text, idx, stop_chars=(",", "]"))
			arr.append(val)
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == ",":
				idx += 1
				continue
			if idx < n and text[idx] == "]":
				return arr, idx + 1
			raise ValueError("Expected ',' or ']' in array")
		raise ValueError("Unterminated array")

	if ch == "'":
		return _parse_single_quoted(text, idx)

	start = idx
	while idx < n and text[idx] not in stop_chars:
		idx += 1
	token = text[start:idx].strip()
	if not token:
		raise ValueError("Empty scalar token")
	return _parse_unquoted_json_scalar(token), idx


def _repair_unquoted_json(text: str) -> Any:
	"""Parse JSON whose double quotes were stripped by Windows PowerShell argv passing."""
	value, idx = _parse_unquoted_json_value(text, 0)
	idx = _skip_ws(text, idx)
	if idx != len(text):
		raise ValueError("Unexpected trailing tokens")
	return value


def _loads_json_with_ps_fallback(text: str) -> Any:
	"""Parse JSON, falling back to unquoted PowerShell-stripped container repair when safe."""
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		stripped = text.lstrip()
		if '"' not in text and stripped.startswith(("[", "{")):
			try:
				return _repair_unquoted_json(stripped)
			except ValueError:
				pass
		raise


def _parse_json_opt(raw: Optional[str], default: Any = None) -> Any:
	if raw is None or not str(raw).strip():
		return default
	text = str(raw).strip()
	if text.startswith("@"):
		file_arg = text[1:].strip()
		if not file_arg:
			raise ValueError("JSON file path missing after '@'.")
		json_path = Path(file_arg).expanduser()
		if not json_path.is_file():
			raise FileNotFoundError(f"JSON file not found: {json_path}")
		text = json_path.read_text(encoding="utf-8-sig")
	try:
		return _loads_json_with_ps_fallback(text)
	except json.JSONDecodeError as exc:
		raise json.JSONDecodeError(
			f"{exc.msg} (hint: on Windows PowerShell, pass @path/to/payload.json to avoid shell quoting issues)",
			exc.doc,
			exc.pos,
		) from exc


def _parse_service_specs(raw_items: Optional[list[str]]) -> list[Any]:
	"""Parse repeatable --service / --add-service values, supporting 'Title::hours' syntax."""
	parsed: list[Any] = []
	for raw in raw_items or []:
		if "::" in raw:
			title, hours_str = raw.rsplit("::", 1)
			title = title.strip()
			hours_str = hours_str.strip()
			if title and hours_str:
				try:
					hours = float(hours_str)
				except ValueError:
					hours = -1.0
				if math.isfinite(hours) and hours > 0:
					parsed.append({"service_title": title, "labor_hours": hours})
					continue
		parsed.append(raw)
	return parsed


def _cmd_quote(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	json_services = _parse_json_opt(a.services_json) or []
	flag_services = _parse_service_specs(a.service)
	services = [*json_services, *flag_services] or None
	return c.create_quote(
		customer_name=a.customer,
		vehicle_identifier=a.vehicle,
		services=services,
		parts=_parse_json_opt(a.parts_json),
		notes=a.notes,
		customer_phone=a.phone,
		customer_email=a.email,
		company=a.company,
		odometer_in=a.odometer_in,
		include_summary=s,
	)


def _cmd_intake_slots(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	json_services = _parse_json_opt(a.services_json) or []
	flag_services = _parse_service_specs(a.service)
	services = [*json_services, *flag_services] or None
	return c.intake_and_find_slots(
		customer_name=a.customer,
		vehicle_identifier=a.vehicle,
		services=services,
		parts=_parse_json_opt(a.parts_json),
		notes=a.notes,
		customer_phone=a.phone,
		customer_email=a.email,
		company=a.company,
		odometer_in=a.odometer_in,
		start_date=a.date,
		days_ahead=a.days,
		duration_minutes=a.duration,
		equipment_tags=_flatten_csv_items(a.tags),
		service_bay=a.bay,
		technician=a.technician,
		max_slots=a.max_slots,
		include_past=bool(a.include_past),
		min_start_time=a.min_time,
		include_resources=bool(a.include_resources),
		include_summary=s,
	)


def _cmd_amend_quote(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	add_json = _parse_json_opt(a.add_services_json) or []
	add_flags = _parse_service_specs(a.add_service)
	add_services = [*add_json, *add_flags] or None
	return c.amend_quote(
		repair_order=a.repair_order,
		add_services=add_services,
		remove_service_indices=_parse_json_opt(a.remove_indices_json),
		notes=a.notes,
		include_summary=s,
	)


def _cmd_ingest_parts(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	raw = Path(a.file).read_text(encoding="utf-8")
	sanitizer = SupplierCatalogSanitizer()
	if a.file.lower().endswith(".json") or raw.lstrip().startswith(("[", "{")):
		items = sanitizer.normalize_items(
			json.loads(raw), default_source=a.source, default_condition=a.condition
		)
	else:
		items = sanitizer.parse_rows(
			raw, default_source=a.source, default_condition=a.condition
		)
	return c.import_supplier_catalog(
		items=items,
		supplier=a.supplier,
		default_source=a.source,
		default_condition=a.condition,
		service_package=a.service_package,
		include_summary=s,
	)


def _cmd_inspection_templates(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	if a.template:
		return c.get_inspection_template(template_name=a.template, include_summary=s)
	return c.list_inspection_templates(include_inactive=bool(a.include_inactive), include_summary=s)


def _flatten_csv_items(raw_items: Optional[list[str]]) -> Optional[list[str]]:
	if not raw_items:
		return None
	items: list[str] = []
	for item in raw_items:
		items.extend(part.strip() for part in str(item).split(",") if part.strip())
	return items or None


def _parse_fields_opt(raw: Optional[str]) -> Optional[list[str]]:
	if raw is None or not str(raw).strip():
		return None
	text = str(raw).strip()
	if text.startswith("[") or text.startswith("@"):
		parsed = _parse_json_opt(text)
		return list(parsed) if parsed is not None else None
	return [col.strip() for col in text.split(",") if col.strip()] or None


def _parse_filters_opt(raw_json: Optional[str], exprs: Optional[list[str]]) -> Any:
	base = _parse_json_opt(raw_json)
	if not exprs:
		return base
	clauses: list[list[str]] = list(base) if isinstance(base, list) else []
	for expr in exprs:
		matched = False
		for op in ("!=", ">=", "<=", "=", ">", "<"):
			if op in expr:
				key, rhs = expr.split(op, 1)
				if op == "=" and ":" in rhs:
					explicit_op, val = rhs.split(":", 1)
					clauses.append([key.strip(), explicit_op.strip(), val.strip()])
				else:
					clauses.append([key.strip(), op, rhs.strip()])
				matched = True
				break
		if not matched:
			raise ValueError(f"Invalid --filter syntax '{expr}'. Expected key=val, key!=val, or key=op:val.")
	return clauses


def _flatten_service_rows(raw_rows: list[str]) -> list[str]:
	rows: list[str] = []
	for item in raw_rows:
		rows.extend(part.strip() for part in str(item).split(",") if part.strip())
	return rows


def _cmd_toggle_service(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	rows = _flatten_service_rows(list(a.service_row or []))
	json_rows = _parse_json_opt(a.service_rows_json)
	if json_rows is not None:
		if isinstance(json_rows, list):
			rows.extend(str(r).strip() for r in json_rows if str(r).strip())
		elif str(json_rows).strip():
			rows.append(str(json_rows).strip())
	if not rows:
		raise ValueError("Provide --service-row or --service-rows-json.")
	return c.toggle_service_completion(
		repair_order=a.repair_order,
		service_row_id=rows[0] if len(rows) == 1 else rows,
		service_row_ids=rows if len(rows) > 1 else None,
		is_completed=not bool(a.reopen),
		include_summary=s,
	)


def _load_batch_items(args: argparse.Namespace) -> list[Any]:
	"""Load batch command specs from --cmd, --file, positional input, or stdin."""
	cmds = getattr(args, "cmds", None)
	if cmds:
		if args.file is not None or args.input is not None:
			raise ValueError("Cannot combine --cmd with --file or positional batch input.")
		return list(cmds)

	source = args.file if args.file is not None else args.input
	if source is None or str(source).strip() == "-":
		raw = sys.stdin.read().lstrip("\ufeff")
	else:
		text = str(source).strip()
		if text.startswith("@"):
			file_arg = text[1:].strip()
			if not file_arg:
				raise ValueError("Batch JSON file path missing after '@'.")
			path = Path(file_arg).expanduser()
			if not path.is_file():
				raise FileNotFoundError(f"Batch JSON file not found: {path}")
			raw = path.read_text(encoding="utf-8-sig")
		elif text.lstrip().startswith("["):
			raw = text
		else:
			path = Path(text).expanduser()
			if not path.is_file():
				raise FileNotFoundError(f"Batch JSON file not found: {path}")
			raw = path.read_text(encoding="utf-8-sig")

	if not raw or not raw.strip():
		raise ValueError("Batch input is empty; provide a JSON array of commands or --cmd flags.")
	payload = _loads_json_with_ps_fallback(raw)
	if not isinstance(payload, list):
		raise ValueError("Batch payload must be a JSON array of commands.")
	return payload


def _dict_args_to_argv(cmd: str, kwargs: dict[str, Any]) -> list[str]:
	"""Convert a command name and flag dictionary into CLI argv tokens."""
	argv = [cmd]
	for key, val in kwargs.items():
		if val is None or val is False:
			continue
		flag = f"--{str(key).lstrip('-').replace('_', '-')}"
		if val is True:
			argv.append(flag)
		elif isinstance(val, (list, tuple)):
			if flag.endswith("-json"):
				argv.extend([flag, json.dumps(val)])
			else:
				for elem in val:
					argv.extend([flag, str(elem)])
		elif isinstance(val, dict):
			argv.extend([flag, json.dumps(val)])
		else:
			argv.extend([flag, str(val)])
	return argv


def _batch_item_to_argv(item: Any) -> list[str]:
	"""Normalize a list, string, or dict batch entry into CLI argv tokens."""
	if isinstance(item, list):
		argv = [json.dumps(x) if isinstance(x, (dict, list)) else str(x) for x in item]
	elif isinstance(item, str):
		escaped = re.sub(r'\\(?![\\"\'])', r'\\\\', item)
		argv = shlex.split(escaped)
	elif isinstance(item, dict):
		cmd = item.get("command") or item.get("cmd")
		if not cmd or not str(cmd).strip():
			raise ValueError("Batch object entry must include 'command'.")
		cmd_str = str(cmd).strip()
		raw_args = item.get("args")
		if isinstance(raw_args, list):
			argv = [cmd_str, *[str(x) for x in raw_args]]
		elif isinstance(raw_args, dict):
			argv = _dict_args_to_argv(cmd_str, raw_args)
		elif raw_args is None:
			extra = {k: v for k, v in item.items() if k not in ("command", "cmd")}
			argv = _dict_args_to_argv(cmd_str, extra)
		else:
			raise ValueError("'args' in batch object entry must be a list or dict.")
	else:
		raise ValueError(f"Unsupported batch entry type: {type(item).__name__}")

	if not argv:
		raise ValueError("Batch entry cannot be empty.")
	return argv


def _cmd_batch(
	a: argparse.Namespace,
	c: IsmAgentClient,
	s: bool,
	parser: argparse.ArgumentParser,
) -> dict[str, Any]:
	"""Execute a JSON array of CLI commands in-process reusing a single client."""
	items = _load_batch_items(a)
	results: list[dict[str, Any]] = []
	for idx, item in enumerate(items):
		cmd_label: Optional[str] = None
		try:
			argv_item = _batch_item_to_argv(item)
			cmd_label = argv_item[0] if argv_item else None
			err_buf = io.StringIO()
			with contextlib.redirect_stderr(err_buf):
				sub_args = parser.parse_args(argv_item)
			cmd_label = sub_args.command
			if sub_args.command in ("batch", "test", "verify-connection"):
				raise ValueError(f"Command '{sub_args.command}' is not allowed inside batch.")
			missing = validate_write_command_args(sub_args)
			if sub_args.command in READ_ONLY_COMMANDS:
				missing.extend(validate_temporal_args(sub_args))
			if missing:
				raise ValueError(
					f"Missing or invalid required field(s) for '{sub_args.command}': {', '.join(missing)}."
				)
			handler = _COMMAND_HANDLERS.get(sub_args.command)
			if not handler:
				raise ValueError(f"Unknown batch command '{sub_args.command}'.")
			res = handler(sub_args, c, s)
			results.append({"index": idx, "command": sub_args.command, "ok": True, "result": res})
		except SystemExit as exc:
			msg = err_buf.getvalue().strip() if "err_buf" in locals() else ""
			results.append({
				"index": idx,
				"command": cmd_label,
				"ok": False,
				"error": msg or f"Invalid arguments (exit code {exc.code})",
			})
			if a.stop_on_error:
				break
		except (IsmApiError, json.JSONDecodeError, OSError, ValueError) as exc:
			results.append({"index": idx, "command": cmd_label, "ok": False, "error": str(exc)})
			if a.stop_on_error:
				break

	succeeded = sum(1 for r in results if r["ok"])
	failed = len(results) - succeeded
	return {
		"success": failed == 0 and len(results) == len(items),
		"total": len(items),
		"succeeded": succeeded,
		"failed": failed,
		"results": results,
	}


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, IsmAgentClient, bool], Any]] = {
	"status": lambda a, c, s: c.get_shop_floor_status(date=a.date, include_summary=s),
	"tech-overview": lambda a, c, s: c.get_daily_tech_overview(date=a.date, include_summary=s),
	"slots": lambda a, c, s: c.find_available_slots(
		repair_order=a.repair_order,
		duration_minutes=a.duration,
		start_date=a.date,
		days_ahead=a.days,
		equipment_tags=_flatten_csv_items(a.tags),
		service_bay=a.bay,
		technician=a.technician,
		include_resources=bool(a.include_resources),
		max_slots=a.max_slots,
		include_past=bool(a.include_past),
		min_start_time=a.min_time,
		include_summary=s,
	),
	"intake-slots": _cmd_intake_slots,
	"reschedule": lambda a, c, s: c.reschedule_appointment(
		schedule_entry=a.schedule_entry,
		scheduled_date=a.date,
		start_time=a.time,
		duration_minutes=a.duration,
		service_bay=a.bay,
		technician=a.tech,
		notes=a.notes,
		include_summary=s,
	),
	"appointment-status": lambda a, c, s: c.update_appointment_status(
		schedule_entry=a.schedule_entry,
		status=a.status,
		notes=a.notes,
		include_summary=s,
	),
	"create-ro-from-entry": lambda a, c, s: c.create_ro_from_appointment(
		schedule_entry=a.schedule_entry,
		odometer_in=a.odometer_in,
		include_summary=s,
	),
	"estimate": lambda a, c, s: c.estimate_duration(
		repair_order=a.repair_order,
		operations=_parse_json_opt(a.operations_json),
		percentile=a.percentile,
		sigma=a.sigma,
		buffer_minutes=a.buffer_minutes,
		include_summary=s,
	),
	"unscheduled": lambda a, c, s: c.get_unscheduled_services(repair_order=a.repair_order, include_summary=s),
	"quote": _cmd_quote,
	"amend-quote": _cmd_amend_quote,
	"send-approval": lambda a, c, s: c.send_quote_for_approval(
		repair_order=a.repair_order,
		communication_medium=a.medium,
		phone_number=a.phone,
		recipient=a.recipient,
		include_summary=s,
	),
	"book": lambda a, c, s: c.book_appointment(
		repair_order=a.repair_order,
		scheduled_date=a.date,
		start_time=a.time,
		service_bay=a.bay,
		technician=a.tech,
		entry_type=a.entry_type,
		notes=a.notes,
		estimated_duration=a.duration,
		provisional_customer_name=a.provisional_customer,
		provisional_vehicle_info=a.provisional_vehicle,
		include_summary=s,
	),
	"job-status": lambda a, c, s: c.update_job_status(
		repair_order=a.repair_order,
		action=a.action,
		odometer_out=a.odometer_out,
		notes=a.notes,
		include_summary=s,
	),
	"check-stock": lambda a, c, s: c.check_parts_stock(
		repair_order=a.repair_order,
		items=_parse_json_opt(a.items_json),
		company=a.company,
		include_summary=s,
	),
	"procure": lambda a, c, s: c.submit_procurement_orders(
		repair_order=a.repair_order,
		parts_data=_parse_json_opt(a.parts_json, []),
		submit=bool(a.submit),
		include_summary=s,
	),
	"receive-po": lambda a, c, s: c.receive_purchase_order(
		purchase_order=a.purchase_order, submit_po=bool(a.submit_po), include_summary=s
	),
	"checkout": lambda a, c, s: c.checkout_repair_order(
		repair_order=a.repair_order,
		payment_method=a.payment_method,
		paid_amount=a.paid_amount,
		odometer_out=a.odometer_out,
		confirm=bool(a.confirm),
		include_summary=s,
	),
	"toggle-service": _cmd_toggle_service,
	"add-supplement": lambda a, c, s: c.add_supplement(
		repair_order=a.repair_order,
		service_title=a.title,
		category=a.category,
		labor_hours=a.hours,
		labor_rate=a.rate,
		parts=_parse_json_opt(a.parts_json),
		notes=a.notes,
		include_summary=s,
	),
	"duplicate-side": lambda a, c, s: c.duplicate_service_for_opposite_side(
		repair_order=a.repair_order,
		service_row_id=a.service_row,
		include_summary=s,
	),
	"deferred": lambda a, c, s: c.get_deferred_services(
		repair_order=a.repair_order,
		vehicle=a.vehicle,
		include_summary=s,
	),
	"add-deferred": lambda a, c, s: c.add_deferred_services(
		repair_order=a.repair_order,
		deferred_items=_parse_json_opt(a.items_json) or list(a.item or []),
		include_summary=s,
	),
	"action-items": lambda a, c, s: c.list_shop_action_items(
		status=a.status,
		reference_doctype=a.ref_doctype,
		reference_name=a.ref_name,
		category=a.category,
		limit=a.limit,
		include_summary=s,
	),
	"create-action": lambda a, c, s: c.create_shop_action_item(
		title=a.title,
		category=a.category,
		priority=a.priority,
		reference_doctype=a.ref_doctype,
		reference_name=a.ref_name,
		item_code=a.item_code,
		details=a.details,
		include_summary=s,
	),
	"resolve-action": lambda a, c, s: c.resolve_shop_action_item(
		action_item=a.action_item,
		resolution_notes=a.notes,
		include_summary=s,
	),
	"ingest-parts": _cmd_ingest_parts,
	"alternates": lambda a, c, s: c.get_component_alternates(
		service_package=a.service_package,
		component_slot=a.component_slot,
		slot_number=a.slot_number,
		oem_part_number=a.oem,
		item_code=a.item_code,
		include_summary=s,
	),
	"fetch-tesla": lambda a, c, s: c.fetch_tesla_procedure(
		url=a.url, repair_order=a.repair_order, vehicle=a.vehicle, include_summary=s
	),
	"import-tesla": lambda a, c, s: c.import_tesla_procedure(
		procedure_title=a.title,
		correction_code=a.code,
		labor_hours=a.hours,
		model=a.model,
		parts=_parse_json_opt(a.parts_json),
		repair_order=a.repair_order,
		url=a.url,
		data=_parse_json_opt(a.data_json),
		on_conflict=a.on_conflict,
		force=bool(a.force),
		side=a.side,
		is_supplement=1 if a.supplement else 0,
		advisor_notes=a.notes,
		include_summary=s,
	),
	"ingest-tesla": lambda a, c, s: TeslaManualParser(c).ingest_from_url(
		a.url,
		model_override=a.model,
		repair_order=a.repair_order,
		on_conflict=a.on_conflict,
		force=bool(a.force),
		side=a.side,
		include_summary=s,
	),
	"decode-vin": lambda a, c, s: c.decode_vehicle_vin(vin=a.vin, include_summary=s),
	"search-vehicles": lambda a, c, s: c.search_vehicles(
		query=a.query, customer=a.customer, model=a.model, limit=a.limit, include_summary=s
	),
	"vehicle-info": lambda a, c, s: c.get_vehicle_intelligence(
		vehicle=a.vehicle, history_limit=a.history_limit, include_summary=s
	),
	"link-vehicle-customer": lambda a, c, s: c.link_vehicle_customer(
		vehicle=a.vehicle,
		customer=a.customer,
		is_primary=bool(a.primary),
		relationship=a.relationship,
		include_summary=s,
	),
	"inspection-templates": _cmd_inspection_templates,
	"apply-inspection": lambda a, c, s: c.apply_inspection_template(
		repair_order=a.repair_order,
		template_name=a.template,
		replace_existing=bool(a.replace),
		include_summary=s,
	),
	"get-inspection": lambda a, c, s: c.get_check_in_inspection(repair_order=a.repair_order, include_summary=s),
	"record-inspection": lambda a, c, s: c.record_check_in_inspection(
		repair_order=a.repair_order,
		items=_parse_json_opt(a.items_json),
		odometer_in=a.odometer_in,
		customer_concern=a.concern,
		template_name=a.template,
		auto_apply_template=bool(a.auto_apply),
		include_summary=s,
	),
	"list-docs": lambda a, c, _s: c.list_documents(
		doctype=a.doctype,
		filters=_parse_filters_opt(a.filters_json, a.filter),
		fields=_parse_fields_opt(a.fields_json),
		limit=a.limit,
		order_by=a.order_by,
	),
	"get-doc": lambda a, c, _s: c.get_document(doctype=a.doctype, name=a.name),
	"create-doc": lambda a, c, _s: c.create_document(doctype=a.doctype, data=_parse_json_opt(a.data_json, {})),
	"update-doc": lambda a, c, _s: c.update_document(
		doctype=a.doctype, name=a.name, data=_parse_json_opt(a.data_json, {})
	),
	"call-method": lambda a, c, _s: c.call_method(method=a.method, args=_parse_json_opt(a.args_json)),
}


def _print_result(res: Any, raw_json: bool = False) -> None:
	if not raw_json and isinstance(res, dict) and res.get("summary_card"):
		print(res["summary_card"])
	else:
		print(json.dumps(res, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="ism-agent-os",
		description="ISM Agent OS CLI for direct Frappe REST and Agent Facade operations.",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog=(
			"Examples:\n"
			"  ism-agent-os --json status --date 2026-11-15\n"
			"  ism-agent-os --json slots --date 2026-11-15 --duration 60 --tags Lift\n"
			"  ism-agent-os --json quote --customer 'Jane Doe' --vehicle 5YJ3E1EA1PF200101 --service 'Brake Service'\n"
			"  ism-agent-os --json book --repair-order RO-2026-00011 --date 2026-11-15 --time 09:00\n"
			"  ism-agent-os --json batch --cmd \"decode-vin --vin 5YJ3E1EA1PF200101\" --cmd \"vehicle-info --vehicle 5YJ3E1EA1PF200101\"\n"
			"  ism-agent-os --json batch --file @cmds.json"
		),
	)
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--json", action="store_true", help="Print raw JSON output")
	parser.add_argument("--summary", action="store_true", help="Request and print markdown summary_card")
	parser.add_argument(
		"--dry-run",
		"--preview",
		action="store_true",
		dest="dry_run",
		help="Validate arguments and output ISM Pre-Dispatch Summary without dispatching write calls",
	)
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("test", help="Verify Frappe REST and ISM Agent Facade connectivity")
	sub.add_parser("verify-connection", help="Alias for 'test' connectivity check")

	p_batch = sub.add_parser("batch", help="Execute multiple CLI commands in one process")
	p_batch.add_argument(
		"input",
		nargs="?",
		help='JSON file path, @file.json, or inline JSON array (reads stdin if omitted)',
	)
	p_batch.add_argument("--file", "-f", dest="file", help="JSON file path or @file.json (overrides positional input)")
	p_batch.add_argument(
		"--cmd",
		"-c",
		action="append",
		dest="cmds",
		help='Command string to execute (repeatable, e.g. --cmd "status --date 2026-11-17" --cmd "tech-overview --date 2026-11-17")',
	)
	p_batch.add_argument("--stop-on-error", action="store_true", help="Stop executing remaining commands after the first error")

	_register_advisor_and_dispatch_parsers(sub)
	_register_floor_and_action_parsers(sub)
	_register_vehicle_and_inspection_parsers(sub)
	_register_parts_and_rest_parsers(sub)
	for sp in set(sub._name_parser_map.values()):
		sp.add_argument(
			"--dry-run",
			"--preview",
			action="store_true",
			dest="dry_run",
			default=argparse.SUPPRESS,
			help="Validate arguments and output ISM Pre-Dispatch Summary without dispatching write calls",
		)
	return parser


def _register_advisor_and_dispatch_parsers(sub: argparse._SubParsersAction) -> None:
	sub.add_parser("status", help="Show live shop floor status").add_argument(
		"--date", help="Target date (ISO YYYY-MM-DD, defaults to today)"
	)
	sub.add_parser("tech-overview", help="Show daily technician utilization").add_argument(
		"--date", help="Target date (ISO YYYY-MM-DD, defaults to today)"
	)

	p_slots = sub.add_parser("slots", help="Find available appointment slots")
	p_slots.add_argument("--repair-order", help="Repair Order ID (e.g. RO-2026-00011)")
	p_slots.add_argument("--duration", type=int, help="Slot duration in minutes (default 60)")
	p_slots.add_argument("--date", help="Start date (ISO YYYY-MM-DD, defaults to today)")
	p_slots.add_argument("--days", type=int, default=3, help="Number of days to search (default 3)")
	p_slots.add_argument(
		"--tags",
		"--equipment",
		action="append",
		help="Required equipment tag (e.g. Lift, Alignment Rack, HV Diagnostic; repeatable or comma-separated)",
	)
	p_slots.add_argument("--bay", help="Filter by Service Bay ID or name")
	p_slots.add_argument("--technician", "--tech", dest="technician", help="Filter by Technician Employee ID or name")
	p_slots.add_argument("--include-resources", action="store_true", help="Include bay and technician directories")
	p_slots.add_argument("--max-slots", type=int, help="Maximum number of slots to return")
	p_slots.add_argument("--min-time", help="Minimum 24-hour HH:MM[:SS] start time on start date")
	p_slots.add_argument("--include-past", action="store_true", help="Include elapsed slots on today's date")

	p_resched = sub.add_parser("reschedule", help="Reschedule an existing Schedule Entry")
	p_resched.add_argument("--schedule-entry", required=True, help="Schedule Entry ID (e.g. SE-00007)")
	p_resched.add_argument("--date", help="New date (ISO YYYY-MM-DD)")
	p_resched.add_argument("--time", help="New start time (24-hour HH:MM or HH:MM:SS)")
	p_resched.add_argument("--duration", type=int, help="Updated duration in minutes")
	p_resched.add_argument("--bay", help="Service Bay ID or name")
	p_resched.add_argument("--tech", help="Technician Employee ID or name")
	p_resched.add_argument("--notes", help="Reschedule notes")

	p_astat = sub.add_parser("appointment-status", help="Update Schedule Entry status")
	p_astat.add_argument("--schedule-entry", required=True)
	p_astat.add_argument(
		"--status",
		required=True,
		choices=("Draft", "Scheduled", "Confirmed", "In Progress", "Completed", "No Show", "Cancelled"),
	)
	p_astat.add_argument("--notes")

	p_cro = sub.add_parser("create-ro-from-entry", help="Create Repair Order from pre-intake Schedule Entry")
	p_cro.add_argument("--schedule-entry", required=True)
	p_cro.add_argument("--odometer-in", type=int, help="Confirmed intake odometer reading")

	p_est = sub.add_parser("estimate", help="Estimate probabilistic job duration")
	p_est.add_argument("--repair-order")
	p_est.add_argument("--operations-json", help="JSON array of operation items or FRT dicts (or @file.json)")
	p_est.add_argument("--percentile", type=float)
	p_est.add_argument("--sigma", type=float)
	p_est.add_argument("--buffer-minutes", type=int)

	sub.add_parser("unscheduled", help="List unscheduled services on RO").add_argument("--repair-order", required=True)

	p_quote = sub.add_parser("quote", help="Create Draft Estimate Repair Order")
	p_quote.add_argument("--customer", required=True, help="Customer name or ID")
	p_quote.add_argument("--vehicle", required=True, help="Vehicle ID, VIN, or license plate")
	p_quote.add_argument(
		"--service",
		action="append",
		help='Service title, "Title::hours" (e.g. "Tire Rotation::0.5"), or exact Service Package name (repeatable)',
	)
	p_quote.add_argument("--services-json", help='JSON array [{"service_title":"...","labor_hours":0.5}] (or @file.json)')
	p_quote.add_argument("--parts-json", help='JSON array [{"item_code":"...","qty":1}] (or @file.json)')
	p_quote.add_argument("--notes", help="Customer concern or advisor notes")
	p_quote.add_argument("--phone", help="Customer phone number")
	p_quote.add_argument("--email", help="Customer email address")
	p_quote.add_argument("--company", help="Company override")
	p_quote.add_argument("--odometer-in", type=int, help="Odometer reading at intake")

	p_is = sub.add_parser("intake-slots", help="Create Draft Estimate Repair Order and find candidate slots in one call")
	p_is.add_argument("--customer", required=True, help="Customer name or ID")
	p_is.add_argument("--vehicle", required=True, help="Vehicle ID, VIN, or license plate")
	p_is.add_argument(
		"--service",
		action="append",
		help='Service title, "Title::hours" (e.g. "Tire Rotation::0.5"), or exact Service Package name (repeatable)',
	)
	p_is.add_argument("--services-json", help='JSON array [{"service_title":"...","labor_hours":0.5}] (or @file.json)')
	p_is.add_argument("--parts-json", help='JSON array [{"item_code":"...","qty":1}] (or @file.json)')
	p_is.add_argument("--notes", help="Customer concern or advisor notes")
	p_is.add_argument("--phone", help="Customer phone number")
	p_is.add_argument("--email", help="Customer email address")
	p_is.add_argument("--company", help="Company override")
	p_is.add_argument("--odometer-in", type=int, help="Odometer reading at intake")
	p_is.add_argument("--date", help="Start date (ISO YYYY-MM-DD, defaults to today)")
	p_is.add_argument("--days", type=int, default=3, help="Number of days to search (default 3)")
	p_is.add_argument("--duration", type=int, help="Slot duration override in minutes")
	p_is.add_argument(
		"--tags",
		"--equipment",
		action="append",
		help="Required equipment tag (e.g. Lift, Alignment Rack, HV Diagnostic; repeatable or comma-separated)",
	)
	p_is.add_argument("--bay", help="Filter by Service Bay ID or name")
	p_is.add_argument("--technician", "--tech", dest="technician", help="Filter by Technician Employee ID or name")
	p_is.add_argument("--max-slots", type=int, help="Maximum number of slots to return")
	p_is.add_argument("--min-time", help="Minimum 24-hour HH:MM[:SS] start time on start date")
	p_is.add_argument("--include-past", action="store_true", help="Include elapsed slots on today's date")
	p_is.add_argument("--include-resources", action="store_true", help="Include bay and technician directories")

	p_amend = sub.add_parser("amend-quote", help="Amend unapproved Repair Order")
	p_amend.add_argument("--repair-order", required=True)
	p_amend.add_argument(
		"--add-service",
		action="append",
		help='Service title or "Title::hours" e.g. "Wiper Blade Replacement::0.3" (repeatable)',
	)
	p_amend.add_argument("--add-services-json", help='JSON array [{"service_title":"...","labor_hours":0.5}] (or @file.json)')
	p_amend.add_argument("--remove-indices-json", help="JSON array of 0-based indices e.g. [0] (or @file.json)")
	p_amend.add_argument("--notes")

	p_auth = sub.add_parser("send-approval", help="Generate/send quote approval link (falls back to link if SMS unconfigured)")
	p_auth.add_argument("--repair-order", required=True)
	p_auth.add_argument("--medium", default="sms", choices=("sms", "email", "link"))
	p_auth.add_argument("--phone")
	p_auth.add_argument("--recipient")

	p_book = sub.add_parser("book", help="Book bay + technician Schedule Entry")
	p_book.add_argument("--repair-order", help="Repair Order ID (omit for standalone/provisional Diagnostic bookings)")
	p_book.add_argument("--date", required=True, help="Appointment date (ISO YYYY-MM-DD)")
	p_book.add_argument("--time", required=True, help="Start time (24-hour HH:MM or HH:MM:SS)")
	p_book.add_argument("--bay", help="Service Bay ID or name")
	p_book.add_argument("--tech", help="Technician Employee ID or name")
	p_book.add_argument("--entry-type", help="Schedule Entry Type (auto-inferred Repair/Diagnostic if omitted)")
	p_book.add_argument("--duration", type=int, help="Estimated duration in minutes (optional override)")
	p_book.add_argument("--provisional-customer", help="Walk-in / provisional customer name when booking without an RO")
	p_book.add_argument("--provisional-vehicle", help="Provisional vehicle summary when booking without an RO")
	p_book.add_argument("--notes", help="Booking notes")

	p_def = sub.add_parser("deferred", help="List open deferred services for a vehicle or Repair Order")
	p_def.add_argument("--repair-order")
	p_def.add_argument("--vehicle")

	p_adef = sub.add_parser("add-deferred", help="Add previously deferred services to a Repair Order")
	p_adef.add_argument("--repair-order", required=True)
	p_adef.add_argument("--item", action="append")
	p_adef.add_argument("--items-json")


def _register_floor_and_action_parsers(sub: argparse._SubParsersAction) -> None:
	p_job = sub.add_parser("job-status", help="Transition Repair Order job status")
	p_job.add_argument("--repair-order", required=True)
	p_job.add_argument("--action", required=True, choices=("start", "hold", "park", "complete"))
	p_job.add_argument("--odometer-out", type=int)
	p_job.add_argument("--notes")

	p_ts = sub.add_parser("toggle-service", help="Toggle completion of one or more service rows")
	p_ts.add_argument("--repair-order", required=True)
	p_ts.add_argument(
		"--service-row",
		action="extend",
		nargs="+",
		help="One or more service row names/indices (repeatable or space/comma-separated)",
	)
	p_ts.add_argument("--service-rows-json", help="JSON array of service row IDs (or @file.json)")
	p_ts.add_argument("--reopen", action="store_true")

	p_sup = sub.add_parser("add-supplement", help="Add mid-job supplement for customer approval")
	p_sup.add_argument("--repair-order", required=True)
	p_sup.add_argument("--title", required=True)
	p_sup.add_argument("--category", default="Maintenance")
	p_sup.add_argument("--hours", type=float, default=1.0)
	p_sup.add_argument("--rate", type=float)
	p_sup.add_argument("--parts-json")
	p_sup.add_argument("--notes")

	p_dup = sub.add_parser("duplicate-side", help="Duplicate symmetrical LH/RH service for opposite side")
	p_dup.add_argument("--repair-order", required=True)
	p_dup.add_argument("--service-row", required=True)

	p_chk = sub.add_parser("checkout", help="Invoice, collect payment, and release Repair Order")
	p_chk.add_argument("--repair-order", required=True)
	p_chk.add_argument("--payment-method", help="Payment method (required, e.g. 'Debit / Interac', 'Visa', 'Cash')")
	p_chk.add_argument("--paid-amount", type=float)
	p_chk.add_argument("--odometer-out", type=int)
	p_chk.add_argument("--confirm", action="store_true")

	p_lai = sub.add_parser("action-items", help="List Shop Action Items")
	p_lai.add_argument("--status", default="Open")
	p_lai.add_argument("--ref-doctype")
	p_lai.add_argument("--ref-name")
	p_lai.add_argument("--category")
	p_lai.add_argument("--limit", type=int, default=20)

	p_cai = sub.add_parser("create-action", help="Create Shop Action Item")
	p_cai.add_argument("--title", required=True)
	p_cai.add_argument("--category", default="Shop Floor Discrepancy")
	p_cai.add_argument("--priority", default="High")
	p_cai.add_argument("--ref-doctype")
	p_cai.add_argument("--ref-name")
	p_cai.add_argument("--item-code")
	p_cai.add_argument("--details")

	p_rai = sub.add_parser("resolve-action", help="Resolve a Shop Action Item")
	p_rai.add_argument("--action-item", required=True)
	p_rai.add_argument("--notes")


def _register_vehicle_and_inspection_parsers(sub: argparse._SubParsersAction) -> None:
	sub.add_parser("decode-vin", help="Decode a 17-character Tesla VIN").add_argument("--vin", required=True)

	p_sv = sub.add_parser("search-vehicles", help="Search Repair Vehicles by VIN, plate, customer, or model")
	p_sv.add_argument("--query")
	p_sv.add_argument("--customer")
	p_sv.add_argument("--model")
	p_sv.add_argument("--limit", type=int, default=20)

	p_vi = sub.add_parser("vehicle-info", help="Fetch vehicle VIN decode, owners, history, and deferred work")
	p_vi.add_argument("--vehicle", required=True)
	p_vi.add_argument("--history-limit", type=int, default=20)

	p_lvc = sub.add_parser("link-vehicle-customer", help="Link a Customer to a Repair Vehicle")
	p_lvc.add_argument("--vehicle", required=True)
	p_lvc.add_argument("--customer", required=True)
	p_lvc.add_argument("--primary", action="store_true")
	p_lvc.add_argument("--relationship")

	p_it = sub.add_parser("inspection-templates", help="List check-in Inspection Templates or get one by name")
	p_it.add_argument("--template")
	p_it.add_argument("--include-inactive", action="store_true")

	p_ai = sub.add_parser("apply-inspection", help="Apply an Inspection Template to a Repair Order")
	p_ai.add_argument("--repair-order", required=True)
	p_ai.add_argument("--template")
	p_ai.add_argument("--replace", action="store_true")

	sub.add_parser("get-inspection", help="Read check-in inspection items and summary for a Repair Order").add_argument(
		"--repair-order", required=True
	)

	p_ri = sub.add_parser("record-inspection", help="Record check-in inspection findings, odometer, and concern")
	p_ri.add_argument("--repair-order", required=True)
	p_ri.add_argument("--items-json", help="JSON array of inspection item results (or @file.json)")
	p_ri.add_argument("--odometer-in", type=int)
	p_ri.add_argument("--concern")
	p_ri.add_argument("--template")
	p_ri.add_argument("--auto-apply", action="store_true")


def _register_parts_and_rest_parsers(sub: argparse._SubParsersAction) -> None:
	p_stk = sub.add_parser("check-stock", help="Check Repair Order or item list warehouse stock and deficits")
	p_stk.add_argument("--repair-order")
	p_stk.add_argument("--items-json", help="JSON array of item dicts/codes (or @file.json)")
	p_stk.add_argument("--company")

	p_po = sub.add_parser("procure", help="Create Draft POs/RFQs (or submit with --submit)")
	p_po.add_argument("--repair-order", required=True)
	p_po.add_argument("--parts-json", required=True)
	p_po.add_argument("--submit", action="store_true")

	p_rcv = sub.add_parser("receive-po", help="Submit (with --submit-po) and receive PO via Purchase Receipt")
	p_rcv.add_argument("--purchase-order", required=True)
	p_rcv.add_argument("--submit-po", action="store_true")

	p_parts = sub.add_parser("ingest-parts", help="Sanitize and import supplier catalog file as structured JSON")
	p_parts.add_argument("file")
	p_parts.add_argument("--supplier")
	p_parts.add_argument("--source", default="Aftermarket")
	p_parts.add_argument("--condition", default="New")
	p_parts.add_argument("--service-package")

	p_alt = sub.add_parser("alternates", help="Find compatible OEM, aftermarket, or used alternate parts")
	p_alt.add_argument("--service-package")
	p_alt.add_argument("--component-slot")
	p_alt.add_argument("--slot-number", type=int)
	p_alt.add_argument("--oem")
	p_alt.add_argument("--item-code")

	p_ft = sub.add_parser("fetch-tesla", help="Fetch/parse Tesla manual URL")
	p_ft.add_argument("url")
	p_ft.add_argument("--repair-order")
	p_ft.add_argument("--vehicle")

	p_imp_t = sub.add_parser("import-tesla", help="Create/update Service Package or import procedure onto Repair Order")
	p_imp_t.add_argument("--title")
	p_imp_t.add_argument("--code")
	p_imp_t.add_argument("--hours", type=float, default=1.0)
	p_imp_t.add_argument("--model", default="Model 3")
	p_imp_t.add_argument("--parts-json")
	p_imp_t.add_argument("--repair-order")
	p_imp_t.add_argument("--url")
	p_imp_t.add_argument("--data-json", help="Parsed Tesla procedure JSON dict (or @file.json)")
	p_imp_t.add_argument("--on-conflict", default="skip", choices=("skip", "overwrite", "add", "error"))
	p_imp_t.add_argument("--force", action="store_true")
	p_imp_t.add_argument("--side")
	p_imp_t.add_argument("--supplement", action="store_true")
	p_imp_t.add_argument("--notes")

	p_tesla = sub.add_parser("ingest-tesla", help="Fetch and import a Tesla service manual URL")
	p_tesla.add_argument("url")
	p_tesla.add_argument("--model")
	p_tesla.add_argument("--repair-order")
	p_tesla.add_argument("--on-conflict", default="skip", choices=("skip", "overwrite", "add", "error"))
	p_tesla.add_argument("--force", action="store_true")
	p_tesla.add_argument("--side")

	p_ld = sub.add_parser("list-docs", help="Query any Frappe DocType via direct REST")
	p_ld.add_argument("--doctype", required=True)
	p_ld.add_argument("--filters-json", "--filters", dest="filters_json", help="JSON filters array (or @file.json)")
	p_ld.add_argument("--filter", action="append", help="Filter shorthand key=val or key=op:val (repeatable)")
	p_ld.add_argument("--fields-json", "--fields", dest="fields_json", help="JSON array, @file.json, or comma-separated fields")
	p_ld.add_argument("--limit", type=int, default=20)
	p_ld.add_argument("--order-by")

	p_gd = sub.add_parser("get-doc", help="Fetch a single Frappe document via direct REST")
	p_gd.add_argument("--doctype", required=True)
	p_gd.add_argument("--name", required=True)

	p_cd = sub.add_parser("create-doc", help="Create a Frappe document via direct REST")
	p_cd.add_argument("--doctype", required=True)
	p_cd.add_argument("--data-json", "--data", dest="data_json", required=True)

	p_ud = sub.add_parser("update-doc", help="Update a Frappe document via direct REST")
	p_ud.add_argument("--doctype", required=True)
	p_ud.add_argument("--name", required=True)
	p_ud.add_argument("--data-json", "--data", dest="data_json", required=True)

	p_cm = sub.add_parser("call-method", help="Call any whitelisted Frappe method via direct REST")
	p_cm.add_argument("--method", required=True)
	p_cm.add_argument("--args-json", "--args", dest="args_json")


if __name__ == "__main__":
	sys.exit(main())

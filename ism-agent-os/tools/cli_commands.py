"""Command dispatch handlers and batch execution for the ISM Agent OS CLI."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from .cli_parsing import (
	_flatten_csv_items,
	_flatten_service_rows,
	_loads_json_with_ps_fallback,
	_parse_fields_opt,
	_parse_filters_opt,
	_parse_json_opt,
	_parse_service_specs,
	_read_utf8_file,
)
from .cli_validation import READ_ONLY_COMMANDS, validate_temporal_args, validate_write_command_args
from .client import IsmAgentClient, IsmApiError

try:
	from .ingestion.supplier_catalog import SupplierCatalogSanitizer
	from .ingestion.tesla_manual_parser import TeslaManualParser
except ImportError:
	_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
	if _REPO_ROOT not in sys.path:
		sys.path.insert(0, _REPO_ROOT)
	from ingestion.supplier_catalog import SupplierCatalogSanitizer
	from ingestion.tesla_manual_parser import TeslaManualParser


def _build_intake_kwargs(a: argparse.Namespace) -> dict[str, Any]:
	json_services = _parse_json_opt(a.services_json, expected_type=list) or []
	flag_services = _parse_service_specs(a.service)
	services = [*json_services, *flag_services] or None
	return {
		"customer_name": a.customer,
		"vehicle_identifier": a.vehicle,
		"services": services,
		"parts": _parse_json_opt(a.parts_json, expected_type=list),
		"notes": a.notes,
		"customer_phone": a.phone,
		"customer_email": a.email,
		"company": a.company,
		"odometer_in": a.odometer_in,
	}


def _cmd_quote(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	return c.create_quote(**_build_intake_kwargs(a), include_summary=s)


def _cmd_intake_slots(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	return c.intake_and_find_slots(
		**_build_intake_kwargs(a),
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
	add_json = _parse_json_opt(a.add_services_json, expected_type=list) or []
	add_flags = _parse_service_specs(a.add_service)
	add_services = [*add_json, *add_flags] or None
	return c.amend_quote(
		repair_order=a.repair_order,
		add_services=add_services,
		remove_service_indices=_parse_json_opt(a.remove_indices_json, expected_type=list),
		notes=a.notes,
		include_summary=s,
	)


def _cmd_ingest_parts(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	raw = _read_utf8_file(a.file, label="Parts catalog")
	sanitizer = SupplierCatalogSanitizer()
	if str(a.file).lower().endswith(".json") or raw.lstrip().startswith(("[", "{")):
		parsed = json.loads(raw)
		if isinstance(parsed, dict):
			if "items" in parsed:
				if not isinstance(parsed["items"], list):
					raise ValueError("Parts JSON 'items' must be a list.")
				raw_items: Any = parsed["items"]
			else:
				raw_items = [parsed]
		elif isinstance(parsed, list):
			raw_items = parsed
		else:
			raise ValueError(f"Parts JSON must be a list or object with 'items', got {type(parsed).__name__}.")
		items = sanitizer.normalize_items(
			raw_items, default_source=a.source, default_condition=a.condition
		)
	else:
		items = sanitizer.parse_rows(
			raw, default_source=a.source, default_condition=a.condition
		)
	if not items:
		raise ValueError(f"No valid parts found in '{a.file}'.")
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


def _cmd_toggle_service(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	rows = _flatten_service_rows(list(a.service_row or []))
	json_rows = _parse_json_opt(a.service_rows_json, expected_type=(list, str))
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
			raw = _read_utf8_file(text[1:], label="Batch JSON")
		elif text.lstrip().startswith("["):
			raw = text
		else:
			raw = _read_utf8_file(text, label="Batch JSON")

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


def _parse_batch_subcommand(
	item: Any,
	parser: argparse.ArgumentParser,
	suppress_stderr: bool = False,
) -> tuple[Optional[str], argparse.Namespace]:
	"""Parse one batch entry into a validated Namespace, rejecting meta-commands."""
	argv_item = _batch_item_to_argv(item)
	cmd_label = argv_item[0] if argv_item else None
	if suppress_stderr:
		err_buf = io.StringIO()
		try:
			with contextlib.redirect_stderr(err_buf):
				sub_args = parser.parse_args(argv_item)
		except SystemExit as exc:
			msg = err_buf.getvalue().strip()
			raise ValueError(msg or f"Invalid arguments (exit code {exc.code})") from exc
	else:
		sub_args = parser.parse_args(argv_item)
	if sub_args.command in ("batch", "test", "verify-connection"):
		raise ValueError(f"Command '{sub_args.command}' is not allowed inside batch.")
	return cmd_label, sub_args


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
			cmd_label, sub_args = _parse_batch_subcommand(item, parser, suppress_stderr=True)
			cmd_label = sub_args.command
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
			is_ok = not (isinstance(res, dict) and res.get("success") is False)
			entry: dict[str, Any] = {
				"index": idx,
				"command": sub_args.command,
				"ok": is_ok,
				"result": res,
			}
			if not is_ok:
				err_msg = res.get("error") or res.get("message")
				if err_msg:
					entry["error"] = str(err_msg)
			results.append(entry)
			if not is_ok and a.stop_on_error:
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
		operations=_parse_json_opt(a.operations_json, expected_type=list),
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
		items=_parse_json_opt(a.items_json, expected_type=list),
		company=a.company,
		include_summary=s,
	),
	"procure": lambda a, c, s: c.submit_procurement_orders(
		repair_order=a.repair_order,
		parts_data=_parse_json_opt(a.parts_json, [], expected_type=list),
		submit=not bool(a.draft),
		company=a.company,
		payment_method=a.payment_method,
		paid_amount=a.paid_amount,
		reference_no=a.reference_no,
		reference_date=a.reference_date,
		include_summary=s,
	),
	"receive-po": lambda a, c, s: c.receive_purchase_order(
		purchase_order=a.purchase_order,
		submit_po=True,
		purchase_invoice=a.purchase_invoice,
		include_summary=s,
	),
	"pay-purchase": lambda a, c, s: c.create_purchase_payment(
		purchase_order=a.purchase_order,
		purchase_invoice=a.purchase_invoice,
		payment_method=a.payment_method,
		paid_amount=a.paid_amount,
		reference_no=a.reference_no,
		reference_date=a.reference_date,
		include_summary=s,
	),
	"purchase-invoice": lambda a, c, s: c.create_purchase_invoice(
		purchase_order=a.purchase_order,
		supplier=a.supplier,
		parts_data=_parse_json_opt(a.parts_json, expected_type=list),
		update_stock=bool(a.update_stock),
		company=a.company,
		bill_no=a.bill_no,
		bill_date=a.bill_date,
		attach_advances=not bool(a.no_attach_advances),
		include_summary=s,
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
		parts=_parse_json_opt(a.parts_json, expected_type=list),
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
		deferred_items=_parse_json_opt(a.items_json, expected_type=list) or list(a.item or []),
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
		parts=_parse_json_opt(a.parts_json, expected_type=list),
		repair_order=a.repair_order,
		url=a.url,
		data=_parse_json_opt(a.data_json, expected_type=dict),
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
		items=_parse_json_opt(a.items_json, expected_type=list),
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
	"create-doc": lambda a, c, _s: c.create_document(
		doctype=a.doctype, data=_parse_json_opt(a.data_json, {}, expected_type=dict)
	),
	"update-doc": lambda a, c, _s: c.update_document(
		doctype=a.doctype, name=a.name, data=_parse_json_opt(a.data_json, {}, expected_type=dict)
	),
	"call-method": lambda a, c, _s: c.call_method(
		method=a.method, args=_parse_json_opt(a.args_json, expected_type=dict)
	),
}


def _print_result(res: Any, raw_json: bool = False) -> None:
	if not raw_json and isinstance(res, dict) and res.get("summary_card"):
		print(res["summary_card"])
	else:
		print(json.dumps(res, indent=2, default=str))

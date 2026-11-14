#!/usr/bin/env python3
"""Command-line interface for ISM Agent OS operations over direct Frappe REST."""

from __future__ import annotations

import argparse
import json
import sys
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


def main(argv: Optional[list[str]] = None) -> int:
	"""Parse CLI arguments and execute the requested ISM Agent OS command."""
	parser = _build_parser()
	args = parser.parse_args(argv)
	config = IsmConfig.from_env(env_path=args.env_file)

	if args.command in ("test", "verify-connection"):
		return 0 if run_diagnostics(config) else 1

	client = IsmAgentClient(config)
	show_summary = bool(args.summary and not args.json)
	try:
		handler = _COMMAND_HANDLERS.get(args.command)
		if not handler:
			return 1
		res = handler(args, client, show_summary)
		_print_result(res, raw_json=args.json)
		return 0
	except (IsmApiError, json.JSONDecodeError, OSError, ValueError) as exc:
		print(f"Error: {exc}", file=sys.stderr)
		return 1


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
		return json.loads(text)
	except json.JSONDecodeError as exc:
		raise json.JSONDecodeError(
			f"{exc.msg} (hint: on Windows PowerShell, pass @path/to/payload.json to avoid shell quoting issues)",
			exc.doc,
			exc.pos,
		) from exc


def _cmd_quote(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	services = _parse_json_opt(a.services_json) or list(a.service or []) or None
	return c.create_quote(
		customer_name=a.customer,
		vehicle_identifier=a.vehicle,
		services=services,
		parts=_parse_json_opt(a.parts_json),
		notes=a.notes,
		customer_phone=a.phone,
		customer_email=a.email,
		odometer_in=a.odometer_in,
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


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, IsmAgentClient, bool], Any]] = {
	"status": lambda a, c, s: c.get_shop_floor_status(date=a.date, include_summary=s),
	"tech-overview": lambda a, c, s: c.get_daily_tech_overview(date=a.date, include_summary=s),
	"slots": lambda a, c, s: c.find_available_slots(
		repair_order=a.repair_order,
		duration_minutes=a.duration,
		start_date=a.date,
		days_ahead=a.days,
		equipment_tags=_flatten_csv_items(a.tags),
		include_resources=bool(a.include_resources),
		max_slots=a.max_slots,
		include_past=bool(a.include_past),
		min_start_time=a.min_time,
		include_summary=s,
	),
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
	"amend-quote": lambda a, c, s: c.amend_quote(
		repair_order=a.repair_order,
		add_services=_parse_json_opt(a.add_services_json),
		remove_service_indices=_parse_json_opt(a.remove_indices_json),
		notes=a.notes,
		include_summary=s,
	),
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
	parser = argparse.ArgumentParser(prog="ism-agent-os", description="ISM Agent OS CLI")
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--json", action="store_true", help="Print raw JSON output")
	parser.add_argument("--summary", action="store_true", help="Request and print markdown summary_card")
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("test", help="Verify Frappe REST and ISM Agent Facade connectivity")
	sub.add_parser("verify-connection", help="Alias for 'test' connectivity check")
	_register_advisor_and_dispatch_parsers(sub)
	_register_floor_and_action_parsers(sub)
	_register_vehicle_and_inspection_parsers(sub)
	_register_parts_and_rest_parsers(sub)
	return parser


def _register_advisor_and_dispatch_parsers(sub: argparse._SubParsersAction) -> None:
	sub.add_parser("status", help="Show live shop floor status").add_argument("--date")
	sub.add_parser("tech-overview", help="Show daily technician utilization").add_argument("--date")

	p_slots = sub.add_parser("slots", help="Find available appointment slots")
	p_slots.add_argument("--repair-order")
	p_slots.add_argument("--duration", type=int)
	p_slots.add_argument("--date", "--target-date", dest="date")
	p_slots.add_argument("--days", type=int, default=3)
	p_slots.add_argument("--tags", "--equipment", action="append", help="Required equipment tag (repeatable or comma-separated)")
	p_slots.add_argument("--include-resources", action="store_true")
	p_slots.add_argument("--max-slots", type=int)
	p_slots.add_argument("--min-time", help="Minimum HH:MM[:SS] start time on start date")
	p_slots.add_argument("--include-past", action="store_true", help="Include elapsed slots on today's date")

	p_resched = sub.add_parser("reschedule", help="Reschedule an existing Schedule Entry")
	p_resched.add_argument("--schedule-entry", required=True)
	p_resched.add_argument("--date")
	p_resched.add_argument("--time")
	p_resched.add_argument("--duration", type=int)
	p_resched.add_argument("--bay")
	p_resched.add_argument("--tech")
	p_resched.add_argument("--notes")

	p_astat = sub.add_parser("appointment-status", help="Update Schedule Entry status")
	p_astat.add_argument("--schedule-entry", required=True)
	p_astat.add_argument(
		"--status",
		required=True,
		choices=("Draft", "Scheduled", "Confirmed", "In Progress", "Completed", "No Show", "Cancelled"),
	)
	p_astat.add_argument("--notes")

	sub.add_parser("create-ro-from-entry", help="Create Repair Order from pre-intake Schedule Entry").add_argument(
		"--schedule-entry", required=True
	)

	p_est = sub.add_parser("estimate", help="Estimate probabilistic job duration")
	p_est.add_argument("--repair-order")
	p_est.add_argument("--operations-json", help="JSON array of operation items or FRT dicts (or @file.json)")
	p_est.add_argument("--percentile", type=float)
	p_est.add_argument("--sigma", type=float)
	p_est.add_argument("--buffer-minutes", type=int)

	sub.add_parser("unscheduled", help="List unscheduled services on RO").add_argument("--repair-order", required=True)

	p_quote = sub.add_parser("quote", help="Create Draft Estimate Repair Order")
	p_quote.add_argument("--customer", required=True)
	p_quote.add_argument("--vehicle", required=True)
	p_quote.add_argument("--service", action="append", help="Service title or exact Service Package name (repeatable)")
	p_quote.add_argument("--services-json", help="JSON array of service objects (or @file.json)")
	p_quote.add_argument("--parts-json", help="JSON array of part objects (or @file.json)")
	p_quote.add_argument("--notes", "--concern", dest="notes")
	p_quote.add_argument("--phone")
	p_quote.add_argument("--email")
	p_quote.add_argument("--odometer-in", type=int)

	p_amend = sub.add_parser("amend-quote", help="Amend unapproved Repair Order")
	p_amend.add_argument("--repair-order", required=True)
	p_amend.add_argument("--add-services-json", help="JSON array of services to add (or @file.json)")
	p_amend.add_argument("--remove-indices-json", help="JSON array of 0-based indices to remove (or @file.json)")
	p_amend.add_argument("--notes")

	p_auth = sub.add_parser("send-approval", help="Generate/send quote approval link (falls back to link if SMS unconfigured)")
	p_auth.add_argument("--repair-order", required=True)
	p_auth.add_argument("--medium", default="sms", choices=("sms", "email", "link"))
	p_auth.add_argument("--phone")
	p_auth.add_argument("--recipient")

	p_book = sub.add_parser("book", help="Book bay + technician Schedule Entry")
	p_book.add_argument("--repair-order", help="Optional Repair Order (omit for standalone/provisional Diagnostic bookings)")
	p_book.add_argument("--date", required=True)
	p_book.add_argument("--time", required=True)
	p_book.add_argument("--bay")
	p_book.add_argument("--tech")
	p_book.add_argument("--entry-type", help="Schedule Entry Type (auto-inferred Repair/Diagnostic if omitted)")
	p_book.add_argument("--duration", type=int, help="Estimated duration in minutes (optional override)")
	p_book.add_argument("--provisional-customer", help="Walk-in / provisional customer name when booking without an RO")
	p_book.add_argument("--provisional-vehicle", help="Provisional vehicle summary when booking without an RO")
	p_book.add_argument("--notes")

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
	p_chk.add_argument("--payment-method", default="Debit / Interac")
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

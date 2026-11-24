"""Declarative validation and pre-dispatch summary helpers for the ISM Agent OS CLI."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from typing import Any, Callable, Optional

from .cli_parsing import _parse_json_opt

READ_ONLY_COMMANDS = frozenset({
	"test",
	"verify-connection",
	"status",
	"tech-overview",
	"slots",
	"estimate",
	"unscheduled",
	"check-stock",
	"bins",
	"alternates",
	"fetch-tesla",
	"tesla-generations",
	"price-part",
	"decode-vin",
	"search-vehicles",
	"vehicle-info",
	"inspection-templates",
	"get-inspection",
	"deferred",
	"action-items",
	"cockpit",
	"pnl-digest",
	"list-docs",
	"get-doc",
	"action-cards",
	"board-threads",
	"board-thread",
})

DIRECT_HOUSEKEEPING_COMMANDS = frozenset({
	"resolve-action",
})

_STEP_PATH_RE = r"[A-Za-z0-9_-]+(?:\[\d+\])*(?:\.[A-Za-z0-9_-]+(?:\[\d+\])*)*"
_STEP_REF_PATTERN = re.compile(
	rf"(?:"
	rf"\$\{{(?:step\d+|prev)(?:\.{_STEP_PATH_RE})?\}}"
	rf"|\$(?:step\d+|prev)(?:\.{_STEP_PATH_RE})?(?![A-Za-z0-9_])"
	rf"|(?<![A-Za-z0-9._%+-])@(?:step\d+|prev)(?:\.{_STEP_PATH_RE})?(?![A-Za-z0-9_@-])"
	rf"|(?<![A-Za-z0-9_.$@-])(?:step\d+|prev)\.{_STEP_PATH_RE}(?![A-Za-z0-9_@-])"
	rf")"
)
_CORRUPTED_STEP_REF_PATTERN = re.compile(
	r"(?<![A-Za-z0-9_./\\])\.(?:data|urls|result|ids)(?:\.[A-Za-z0-9_-]+(?:\[\d+\])*)+(?![A-Za-z0-9_./\\-])"
)
_AT_SPECIAL_REF_RE = re.compile(r"^@(?:step\d+|prev)(?:\.|$)")
_AT_FILE_JSON_ATTRS = (
	"parts_json",
	"items_json",
	"data_json",
	"services_json",
	"add_services_json",
	"remove_indices_json",
	"update_services",
	"service_rows_json",
	"payments",
	"operations_json",
	"args_json",
	"bin_allocations_json",
)


def find_corrupted_step_ref(val: Any) -> Optional[str]:
	"""Return the first PowerShell-corrupted leading-dot step reference token, if any."""
	if isinstance(val, str):
		m = _CORRUPTED_STEP_REF_PATTERN.search(val)
		return m.group(0) if m else None
	if isinstance(val, (list, tuple)):
		for item in val:
			found = find_corrupted_step_ref(item)
			if found:
				return found
	elif isinstance(val, dict):
		for v in val.values():
			found = find_corrupted_step_ref(v)
			if found:
				return found
	elif isinstance(val, argparse.Namespace):
		for k, v in vars(val).items():
			if k not in ("command", "env_file", "json", "summary", "dry_run"):
				found = find_corrupted_step_ref(v)
				if found:
					return found
	return None


def raise_if_corrupted_step_ref(val: Any) -> None:
	"""Raise ValueError if val contains a PowerShell-stripped leading-dot step reference."""
	bad = find_corrupted_step_ref(val)
	if bad:
		raise ValueError(
			f"Detected corrupted step reference '{bad}' (PowerShell likely stripped unescaped '$stepN'/'$prev'). "
			"Use '@step0.data.name' or '@prev.data.name' (or 'step0.data.name') instead of '$step0'/'$prev'."
		)


def _contains_step_ref(val: Any) -> bool:
	if isinstance(val, str):
		return bool(_STEP_REF_PATTERN.search(val))
	if isinstance(val, (list, tuple)):
		return any(_contains_step_ref(item) for item in val)
	if isinstance(val, dict):
		return any(_contains_step_ref(v) for v in val.values())
	return False


def _has_value(val: Any) -> bool:
	if val is None or val is False:
		return False
	if isinstance(val, str):
		return bool(val.strip())
	if isinstance(val, (list, tuple, set)):
		return any(_has_value(item) for item in val)
	if isinstance(val, dict):
		return len(val) > 0
	return True


def _check_non_negative_int_arg(
	args: argparse.Namespace,
	attr: str,
	flag_name: str,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	val = getattr(args, attr, None)
	if val is None or (isinstance(val, str) and not val.strip()):
		missing.append(flag_name)
		return
	if allow_step_refs and _contains_step_ref(val):
		return
	if isinstance(val, bool):
		missing.append(f"{flag_name} (must be >= 0)")
		return
	try:
		parsed = int(val)
	except (TypeError, ValueError):
		missing.append(f"{flag_name} (must be a non-negative integer)")
		return
	if parsed < 0:
		missing.append(f"{flag_name} (must be >= 0)")


def _validate_job_status(args: argparse.Namespace, missing: list[str], *, allow_step_refs: bool = False) -> None:
	if getattr(args, "action", None) == "complete":
		_check_non_negative_int_arg(
			args, "odometer_out", "--odometer-out", missing, allow_step_refs=allow_step_refs
		)


def _validate_deposit(args: argparse.Namespace, missing: list[str], *, allow_step_refs: bool = False) -> None:
	amount = getattr(args, "amount", None)
	if allow_step_refs and _contains_step_ref(amount):
		return
	if amount is not None and not isinstance(amount, bool):
		try:
			if float(amount) <= 0:
				missing.append("--amount (must be > 0)")
		except (TypeError, ValueError):
			missing.append("--amount (must be > 0)")


def _validate_record_inspection(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	if getattr(args, "odometer_in", None) is not None:
		_check_non_negative_int_arg(
			args, "odometer_in", "--odometer-in", missing, allow_step_refs=allow_step_refs
		)


def _validate_procure(args: argparse.Namespace, missing: list[str], *, allow_step_refs: bool = False) -> None:
	has_pay_extras = any(
		_has_value(getattr(args, f, None))
		for f in ("paid_amount", "reference_no", "reference_date")
	)
	has_pay_method = _has_value(getattr(args, "payment_method", None))
	if has_pay_extras and not has_pay_method:
		missing.append("--payment-method")
	if bool(getattr(args, "draft", False)) and (has_pay_method or has_pay_extras):
		missing.append("--draft (cannot record payment against a Draft PO)")


def _validate_purchase_invoice(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	if not _has_value(getattr(args, "purchase_order", None)):
		if not _has_value(getattr(args, "supplier", None)):
			missing.append("--supplier (or --purchase-order)")
		if not _has_value(getattr(args, "parts_json", None)):
			missing.append("--parts-json (or --purchase-order)")


def _validate_import_tesla(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	if _has_value(getattr(args, "repair_order", None)):
		choices = ("url", "data_json", "title", "code")
		label = "--url/--data-json/--title/--code (at least one required)"
	else:
		choices = ("url", "data_json", "title")
		label = "--url/--data-json/--title (at least one required)"
	if not any(_has_value(getattr(args, f, None)) for f in choices):
		missing.append(label)


def _validate_publish_score(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	score_val = getattr(args, "score", None)
	if not _has_value(score_val):
		return
	if allow_step_refs and _contains_step_ref(score_val):
		return
	if isinstance(score_val, bool):
		missing.append("--score (must be 0..100)")
		return
	try:
		parsed = int(score_val)
	except (TypeError, ValueError):
		missing.append("--score (must be 0..100)")
		return
	if parsed < 0 or parsed > 100:
		missing.append("--score (must be 0..100)")


def _validate_sync_action(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	action_val = getattr(args, "action", None)
	if _has_value(action_val) and not (allow_step_refs and _contains_step_ref(action_val)):
		if str(action_val).strip().lower() not in ("snooze", "dismiss", "reopen"):
			missing.append("--action (must be snooze, dismiss, or reopen)")
	snooze_val = getattr(args, "snooze_minutes", None)
	if _has_value(snooze_val) and not (allow_step_refs and _contains_step_ref(snooze_val)):
		if isinstance(snooze_val, bool):
			missing.append("--snooze-minutes (must be 1..43200)")
			return
		try:
			parsed = int(snooze_val)
		except (TypeError, ValueError):
			missing.append("--snooze-minutes (must be 1..43200)")
			return
		if parsed < 1 or parsed > 43200:
			missing.append("--snooze-minutes (must be 1..43200)")


def _validate_board_target(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	del allow_step_refs
	if _has_value(getattr(args, "thread_key", None)) and _has_value(getattr(args, "repair_order", None)):
		missing.append("--thread-key/--repair-order (mutually exclusive)")


def _validate_move_stock(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	from_bin = getattr(args, "from_bin", None)
	to_bin = getattr(args, "to_bin", None)
	if (
		_has_value(from_bin)
		and _has_value(to_bin)
		and not (allow_step_refs and (_contains_step_ref(from_bin) or _contains_step_ref(to_bin)))
	):
		if str(from_bin).strip().upper() == str(to_bin).strip().upper():
			missing.append("--to-bin (must differ from --from-bin)")

	has_items_json = _has_value(getattr(args, "items_json", None))
	if has_items_json:
		return

	item_code = getattr(args, "item_code", None)
	qty = getattr(args, "qty", None)
	has_item = _has_value(item_code)
	has_qty = _has_value(qty)
	if not has_item and not has_qty:
		missing.append("--items-json or (--item-code and --qty)")
		return
	if not has_item:
		missing.append("--item-code (required with --qty)")
	if not has_qty:
		missing.append("--qty (required with --item-code)")
		return
	if allow_step_refs and _contains_step_ref(qty):
		return
	if isinstance(qty, bool):
		missing.append("--qty (must be > 0)")
		return
	try:
		if float(qty) <= 0:
			missing.append("--qty (must be > 0)")
	except (TypeError, ValueError):
		missing.append("--qty (must be > 0)")


def _validate_adjust_stock(
	args: argparse.Namespace,
	missing: list[str],
	*,
	allow_step_refs: bool = False,
) -> None:
	purpose = getattr(args, "purpose", None)
	if _has_value(purpose) and not (allow_step_refs and _contains_step_ref(purpose)):
		if str(purpose).strip().lower() not in ("receipt", "issue", "material receipt", "material issue"):
			missing.append("--purpose (must be receipt or issue)")


WRITE_COMMAND_META: dict[str, dict[str, Any]] = {
	"quote": {
		"target": "ism.api.agent.create_quote -> Repair Order (Draft Estimate)",
		"fields": ("customer", "vehicle", "odometer_in", "vehicle_color", "license_plate", "service", "services_json", "parts_json", "notes", "phone", "email", "company"),
		"required": (("customer", "--customer"), ("vehicle", "--vehicle")),
		"non_neg_ints": (("odometer_in", "--odometer-in"),),
		"any_of": ((("service", "services_json", "notes"), "--service/--services-json/--notes (at least one required)"),),
	},
	"intake-slots": {
		"target": "ism.api.agent.intake_and_find_slots -> Repair Order (Draft Estimate) + Slots",
		"fields": ("customer", "vehicle", "odometer_in", "vehicle_color", "license_plate", "service", "services_json", "parts_json", "notes", "phone", "email", "date", "days", "duration", "tags", "bay", "technician"),
		"required": (("customer", "--customer"), ("vehicle", "--vehicle")),
		"non_neg_ints": (("odometer_in", "--odometer-in"),),
		"any_of": ((("service", "services_json", "notes"), "--service/--services-json/--notes (at least one required)"),),
	},
	"create-ro-from-entry": {
		"target": "ism.api.agent.create_ro_from_appointment -> Repair Order (Checked In)",
		"fields": ("schedule_entry", "odometer_in", "vehicle_color", "license_plate"),
		"required": (("schedule_entry", "--schedule-entry"),),
		"non_neg_ints": (("odometer_in", "--odometer-in"),),
	},
	"amend-quote": {
		"target": "ism.api.agent.amend_quote -> Repair Order",
		"fields": (
			"repair_order",
			"add_service",
			"add_services_json",
			"remove_indices_json",
			"update_services",
			"notes",
			"discount_amount",
			"shop_supplies_amount",
			"environmental_fee_amount",
		),
		"required": (("repair_order", "--repair-order"),),
		"any_of": ((
			(
				"add_service",
				"add_services_json",
				"remove_indices_json",
				"update_services",
				"notes",
				"discount_amount",
				"shop_supplies_amount",
				"environmental_fee_amount",
			),
			"--add-service/--add-services-json/--remove-indices-json/--update-services/--notes/--discount-amount/--shop-supplies-amount/--environmental-fee-amount (at least one required)",
		),),
	},
	"send-approval": {
		"target": "ism.api.agent.send_quote_for_approval -> Approval Dispatch",
		"fields": ("repair_order", "medium", "phone", "recipient"),
		"required": (("repair_order", "--repair-order"), ("medium", "--medium")),
	},
	"book": {
		"target": "ism.api.agent.book_appointment -> Schedule Entry",
		"fields": ("repair_order", "date", "time", "bay", "tech", "entry_type", "duration", "provisional_customer", "provisional_vehicle", "notes"),
		"required": (("date", "--date"), ("time", "--time")),
	},
	"reschedule": {
		"target": "ism.api.agent.reschedule_appointment -> Schedule Entry",
		"fields": ("schedule_entry", "date", "time", "duration", "bay", "tech", "notes"),
		"required": (("schedule_entry", "--schedule-entry"),),
		"any_of": ((("date", "time", "duration", "bay", "tech", "notes"), "--date/--time/--duration/--bay/--tech/--notes (at least one change required)"),),
	},
	"appointment-status": {
		"target": "ism.api.agent.update_appointment_status -> Schedule Entry",
		"fields": ("schedule_entry", "status", "notes"),
		"required": (("schedule_entry", "--schedule-entry"), ("status", "--status")),
	},
	"job-status": {
		"target": "ism.api.agent.update_job_status -> Repair Order",
		"fields": ("repair_order", "action", "odometer_out", "notes"),
		"required": (("repair_order", "--repair-order"), ("action", "--action")),
		"custom": _validate_job_status,
	},
	"deposit": {
		"target": "ism.api.agent.record_ro_deposit -> Payment Entry (Advance Deposit)",
		"fields": ("repair_order", "amount", "payment_method", "reference_no"),
		"required": (("repair_order", "repair_order"), ("amount", "--amount")),
		"custom": _validate_deposit,
	},
	"checkout": {
		"target": "ism.api.agent.checkout_repair_order -> Sales Invoice + Payment + Release",
		"fields": ("repair_order", "payment_method", "payments", "odometer_out", "paid_amount", "confirm"),
		"required": (("repair_order", "--repair-order"),),
		"any_of": ((("payment_method", "payments"), "--payment-method or --payments"),),
		"non_neg_ints": (("odometer_out", "--odometer-out"),),
		"required_bool": (("confirm", "--confirm"),),
	},
	"toggle-service": {
		"target": "ism.api.agent.toggle_service_completion -> Repair Order Service",
		"fields": ("repair_order", "service_row", "service_rows_json", "all", "reopen"),
		"required": (("repair_order", "--repair-order"),),
		"any_of": ((("service_row", "service_rows_json", "all"), "--service-row, --service-rows-json, or --all"),),
	},
	"add-supplement": {
		"target": "ism.api.agent.add_supplement -> Repair Order Supplement",
		"fields": ("repair_order", "title", "category", "hours", "rate", "parts_json", "notes"),
		"required": (("repair_order", "--repair-order"), ("title", "--title")),
	},
	"duplicate-side": {
		"target": "ism.api.agent.duplicate_service_for_opposite_side -> Repair Order Service",
		"fields": ("repair_order", "service_row"),
		"required": (("repair_order", "--repair-order"), ("service_row", "--service-row")),
	},
	"add-deferred": {
		"target": "ism.api.agent.add_deferred_services -> Repair Order Service",
		"fields": ("repair_order", "item", "items_json"),
		"required": (("repair_order", "--repair-order"),),
		"any_of": ((("item", "items_json"), "--item or --items-json"),),
	},
	"create-action": {
		"target": "ism.api.agent.create_shop_action_item -> Shop Action Item",
		"fields": ("title", "category", "priority", "ref_doctype", "ref_name", "item_code", "details", "dedup_key", "source"),
		"required": (("title", "--title"),),
	},
	"resolve-action": {
		"target": "ism.api.agent.resolve_shop_action_item -> Shop Action Item",
		"fields": ("action_item", "notes"),
		"required": (("action_item", "--action-item"),),
	},
	"sync-action": {
		"target": "ism.api.agent.sync_action_item_state -> Shop Action Item",
		"fields": ("dedup_key", "action", "snooze_minutes", "title", "notes", "ref_doctype", "ref_name"),
		"required": (("dedup_key", "--dedup-key"), ("action", "--action")),
		"custom": _validate_sync_action,
	},
	"publish-score": {
		"target": "ism.api.agent.publish_health_snapshot -> Shop Health Snapshot",
		"fields": ("metric", "score", "grade", "verdict", "period_date", "report_url", "details_json"),
		"required": (("metric", "--metric"), ("score", "--score"), ("verdict", "--verdict")),
		"custom": _validate_publish_score,
	},
	"apply-inspection": {
		"target": "ism.api.agent.apply_inspection_template -> Repair Order Inspection",
		"fields": ("repair_order", "template", "replace"),
		"required": (("repair_order", "--repair-order"),),
	},
	"record-inspection": {
		"target": "ism.api.agent.record_check_in_inspection -> Repair Order Inspection",
		"fields": ("repair_order", "odometer_in", "vehicle_color", "license_plate", "concern", "template", "auto_apply", "items_json"),
		"required": (("repair_order", "--repair-order"),),
		"custom": _validate_record_inspection,
	},
	"procure": {
		"target": "ism.api.agent.submit_procurement_orders -> Purchase Order / RFQ (+ optional Payment Entry)",
		"fields": ("repair_order", "parts_json", "company", "draft", "payment_method", "paid_amount", "reference_no", "reference_date"),
		"required": (("parts_json", "--parts-json"),),
		"custom": _validate_procure,
	},
	"receive-po": {
		"target": "ism.api.agent.receive_purchase_order -> Purchase Receipt",
		"fields": ("purchase_order", "purchase_invoice", "submit_po", "bin_location", "bin_allocations_json"),
		"any_of": ((("purchase_order", "purchase_invoice"), "--purchase-order or --purchase-invoice"),),
	},
	"pay-purchase": {
		"target": "ism.api.agent.create_purchase_payment -> Supplier Payment Entry",
		"fields": ("purchase_order", "purchase_invoice", "payment_method", "paid_amount", "reference_no", "reference_date"),
		"required": (("payment_method", "--payment-method"),),
		"any_of": ((("purchase_order", "purchase_invoice"), "--purchase-order or --purchase-invoice"),),
	},
	"purchase-invoice": {
		"target": "ism.api.agent.create_purchase_invoice -> Purchase Invoice",
		"fields": ("purchase_order", "supplier", "parts_json", "update_stock", "bill_no", "bill_date", "company", "no_attach_advances", "bin_location"),
		"custom": _validate_purchase_invoice,
	},
	"create-bin": {
		"target": "ism.api.agent.create_bin_location -> Storage Bin",
		"fields": ("bin_code", "warehouse", "zone", "aisle", "rack", "shelf", "bin_type", "description"),
		"required": (("bin_code", "--bin-code"),),
	},
	"assign-bin": {
		"target": "ism.api.agent.set_item_default_bin -> Item",
		"fields": ("item_code", "bin_location"),
		"required": (("item_code", "--item-code"),),
	},
	"move-stock": {
		"target": "ism.api.agent.transfer_bin_stock -> Stock Entry (Material Transfer)",
		"fields": ("from_bin", "to_bin", "item_code", "qty", "items_json", "warehouse", "notes"),
		"required": (("from_bin", "--from-bin"), ("to_bin", "--to-bin")),
		"custom": _validate_move_stock,
	},
	"adjust-stock": {
		"target": "ism.api.agent.adjust_bin_stock -> Stock Entry (Material Receipt / Issue)",
		"fields": ("purpose", "items_json", "bin_location", "warehouse", "reason"),
		"required": (("purpose", "--purpose"), ("items_json", "--items-json")),
		"custom": _validate_adjust_stock,
	},
	"ingest-parts": {
		"target": "SupplierCatalogSanitizer + import_supplier_catalog -> Item",
		"fields": ("file", "supplier", "source", "condition", "service_package"),
		"required": (("file", "file"), ("service_package", "--service-package")),
	},
	"import-tesla": {
		"target": "ism.api.agent.import_tesla_procedure -> Service Package / Repair Order",
		"fields": ("url", "data_json", "title", "code", "hours", "model", "generation", "parts_json", "repair_order", "on_conflict", "force", "side", "supplement", "notes"),
		"custom": _validate_import_tesla,
	},
	"ingest-tesla": {
		"target": "TeslaManualParser.ingest_from_url -> Service Package / Repair Order",
		"fields": ("url", "model", "generation", "repair_order", "on_conflict", "force", "side"),
		"required": (("url", "url"),),
	},
	"link-vehicle-customer": {
		"target": "ism.api.agent.link_vehicle_customer -> Repair Vehicle",
		"fields": ("vehicle", "customer", "primary", "relationship"),
		"required": (("vehicle", "--vehicle"), ("customer", "--customer")),
	},
	"create-doc": {
		"target": "REST POST /api/resource/<DocType>",
		"fields": ("doctype", "data_json"),
		"required": (("doctype", "--doctype"), ("data_json", "--data-json")),
	},
	"update-doc": {
		"target": "REST PUT /api/resource/<DocType>/<Name>",
		"fields": ("doctype", "name", "data_json"),
		"required": (("doctype", "--doctype"), ("name", "--name"), ("data_json", "--data-json")),
	},
	"call-method": {
		"target": "REST POST /api/method/<method>",
		"fields": ("method", "args_json"),
		"required": (("method", "--method"),),
	},
	"board-post": {
		"target": "ism.api.board.post_board_capture -> Shop Board Event",
		"fields": ("thread_key", "repair_order", "text", "intent", "idempotency_key", "run_ai"),
		"required": (("text", "--text"),),
		"any_of": ((("thread_key", "repair_order"), "--thread-key/--repair-order (one required)"),),
		"custom": _validate_board_target,
	},
}

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


def validate_temporal_args(args: argparse.Namespace, *, allow_step_refs: bool = False) -> list[str]:
	"""Return invalid date/time flag messages for any CLI command."""
	errors: list[str] = []
	date_val = getattr(args, "date", None)
	if _has_value(date_val) and not (allow_step_refs and _contains_step_ref(date_val)) and not _is_valid_iso_date(date_val):
		errors.append("--date (must be ISO YYYY-MM-DD)")

	time_val = getattr(args, "time", None)
	if _has_value(time_val) and not (allow_step_refs and _contains_step_ref(time_val)) and not _is_valid_iso_time(time_val):
		errors.append("--time (must be 24-hour HH:MM or HH:MM:SS)")

	min_time_val = getattr(args, "min_time", None)
	if _has_value(min_time_val) and not (allow_step_refs and _contains_step_ref(min_time_val)) and not _is_valid_iso_time(min_time_val):
		errors.append("--min-time (must be 24-hour HH:MM or HH:MM:SS)")

	for flag_name, attr_name in (
		("--reference-date", "reference_date"),
		("--bill-date", "bill_date"),
		("--from-date", "from_date"),
		("--to-date", "to_date"),
		("--period-date", "period_date"),
	):
		val = getattr(args, attr_name, None)
		if _has_value(val) and not (allow_step_refs and _contains_step_ref(val)) and not _is_valid_iso_date(val):
			errors.append(f"{flag_name} (must be ISO YYYY-MM-DD)")
	return errors


def validate_write_command_args(args: argparse.Namespace, *, allow_step_refs: bool = False) -> list[str]:
	"""Return a list of missing or invalid required flags for write CLI commands."""
	raise_if_corrupted_step_ref(args)

	cmd = getattr(args, "command", None)
	if not cmd or cmd in READ_ONLY_COMMANDS or cmd == "batch":
		return []

	for attr in _AT_FILE_JSON_ATTRS:
		raw_val = getattr(args, attr, None)
		if isinstance(raw_val, str):
			stripped = raw_val.strip()
			if stripped.startswith("@") and not _AT_SPECIAL_REF_RE.match(stripped):
				try:
					_parse_json_opt(stripped)
				except (OSError, json.JSONDecodeError, ValueError) as exc:
					flag_label = f"--{attr.replace('_', '-')}"
					raise ValueError(f"Invalid JSON file '{stripped}' for {flag_label}: {exc}") from exc

	missing: list[str] = []
	meta = WRITE_COMMAND_META.get(cmd)
	if meta:
		for attr, flag_label in meta.get("required", ()):
			if not _has_value(getattr(args, attr, None)):
				missing.append(flag_label)
		for attr, flag_label in meta.get("non_neg_ints", ()):
			_check_non_negative_int_arg(
				args, attr, flag_label, missing, allow_step_refs=allow_step_refs
			)
		for attr, flag_label in meta.get("required_bool", ()):
			if not bool(getattr(args, attr, False)):
				missing.append(flag_label)
		for choices, label in meta.get("any_of", ()):
			if not any(_has_value(getattr(args, f, None)) for f in choices):
				missing.append(label)
		custom: Callable[..., None] | None = meta.get("custom")
		if custom is not None:
			custom(args, missing, allow_step_refs=allow_step_refs)

	missing.extend(validate_temporal_args(args, allow_step_refs=allow_step_refs))
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
			is_missing_field = any(
				m in (flag, field) or m.startswith((f"{flag} ", f"{field} "))
				for m in missing
			)
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
			if not any(
				m in (f"--{f.replace('_', '-')}", f) or m.startswith((f"--{f.replace('_', '-')} ", f"{f} "))
				for f in fields_list
			):
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

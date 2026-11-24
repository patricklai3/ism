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
	"stage-receipt",
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
_AT_SPECIAL_REF_RE = re.compile(r"^@(?:last|step\d+|prev)(?:\.|$)")
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
			if k not in ("command", "env_file", "json", "summary", "dry_run", "stage"):
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
			k for k in vars(sub_args).keys() if k not in ("command", "env_file", "json", "summary", "dry_run", "stage")
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


STAGE_FIELD_HINTS: dict[str, dict[str, Any]] = {
	"customer": {"type": "text", "label": "Customer", "placeholder": "Customer name or ID"},
	"vehicle": {"type": "text", "label": "Vehicle", "placeholder": "VIN, plate, or Repair Vehicle ID"},
	"vehicle_color": {"type": "text", "label": "Vehicle Color", "placeholder": "e.g. Pearl White"},
	"license_plate": {"type": "text", "label": "License Plate", "placeholder": "e.g. EV-402"},
	"odometer_in": {"type": "int", "label": "Odometer In (km)", "min": 0, "placeholder": "e.g. 48250"},
	"odometer_out": {"type": "int", "label": "Odometer Out (km)", "min": 0, "placeholder": "e.g. 48260"},
	"service": {"type": "textarea", "label": "Services", "multi": True, "placeholder": "One per line, e.g. Brake Service::1.0"},
	"add_service": {"type": "textarea", "label": "Add Services", "multi": True, "placeholder": "One per line, e.g. Wiper Blade Replacement::0.3"},
	"services_json": {"type": "json", "label": "Services JSON", "placeholder": '[{"service_title": "...", "labor_hours": 1.0}]'},
	"add_services_json": {"type": "json", "label": "Add Services JSON", "placeholder": '[{"service_title": "...", "labor_hours": 0.5}]'},
	"remove_indices_json": {"type": "json", "label": "Remove Service Indices JSON", "placeholder": "[0]"},
	"update_services": {"type": "json", "label": "Update Services JSON", "placeholder": '[{"idx": 1, "labor_hours": 1.5}]'},
	"parts_json": {"type": "json", "label": "Parts JSON", "placeholder": '[{"item_code": "...", "qty": 1}]'},
	"notes": {"type": "textarea", "label": "Notes"},
	"phone": {"type": "text", "label": "Phone", "placeholder": "+1..."},
	"email": {"type": "text", "label": "Email", "placeholder": "customer@example.com"},
	"recipient": {"type": "text", "label": "Recipient", "placeholder": "Email address or contact"},
	"company": {"type": "text", "label": "Company"},
	"date": {"type": "date", "label": "Date (YYYY-MM-DD)", "placeholder": "YYYY-MM-DD"},
	"time": {"type": "time", "label": "Start Time (HH:MM:SS)", "placeholder": "09:00:00"},
	"min_time": {"type": "time", "label": "Min Time (HH:MM:SS)", "placeholder": "08:00:00"},
	"days": {"type": "int", "label": "Days Ahead", "min": 1},
	"duration": {"type": "int", "label": "Duration (minutes)", "min": 1},
	"tags": {"type": "textarea", "label": "Equipment Tags", "multi": True, "placeholder": "Lift, Alignment Rack, HV Diagnostic"},
	"bay": {"type": "text", "label": "Service Bay", "placeholder": "Service Bay ID or name"},
	"technician": {"type": "text", "label": "Technician", "placeholder": "Technician ID or name"},
	"tech": {"type": "text", "label": "Technician", "placeholder": "Technician ID or name"},
	"schedule_entry": {"type": "text", "label": "Schedule Entry", "placeholder": "SE-00001"},
	"repair_order": {"type": "text", "label": "Repair Order", "placeholder": "RO-2026-00001"},
	"discount_amount": {"type": "float", "label": "Discount Amount (CAD)", "min": 0},
	"shop_supplies_amount": {"type": "float", "label": "Shop Supplies Amount (CAD)", "min": 0},
	"environmental_fee_amount": {"type": "float", "label": "Environmental Fee Amount (CAD)", "min": 0},
	"medium": {"type": "select", "label": "Approval Medium", "choices": ["sms", "email", "link"]},
	"entry_type": {"type": "text", "label": "Entry Type", "suggestions": ["Diagnostic", "Repair"]},
	"provisional_customer": {"type": "text", "label": "Provisional Customer"},
	"provisional_vehicle": {"type": "text", "label": "Provisional Vehicle"},
	"status": {"type": "text", "label": "Status"},
	"action": {"type": "select", "label": "Action", "choices": ["start", "hold", "park", "complete"]},
	"amount": {"type": "float", "label": "Amount (CAD)", "min": 0},
	"payment_method": {"type": "text", "label": "Payment Method", "suggestions": ["Debit / Interac", "Visa", "Mastercard", "Amex", "Cash", "E-Transfer"]},
	"reference_no": {"type": "text", "label": "Reference No"},
	"reference_date": {"type": "date", "label": "Reference Date (YYYY-MM-DD)", "placeholder": "YYYY-MM-DD"},
	"payments": {"type": "json", "label": "Split Payments JSON", "placeholder": '[{"payment_method": "Visa", "amount": 100}]'},
	"paid_amount": {"type": "float", "label": "Paid Amount (CAD)", "min": 0},
	"confirm": {"type": "bool", "label": "Confirm Checkout"},
	"service_row": {"type": "textarea", "label": "Service Row(s)", "multi": True, "placeholder": "One row ID or 1-based index per line"},
	"service_rows_json": {"type": "json", "label": "Service Rows JSON", "placeholder": '["row-1"]'},
	"reopen": {"type": "bool", "label": "Reopen Service(s)"},
	"title": {"type": "text", "label": "Title"},
	"category": {"type": "text", "label": "Category"},
	"hours": {"type": "float", "label": "Labor Hours", "min": 0},
	"rate": {"type": "float", "label": "Labor Rate (CAD/hr)", "min": 0},
	"item": {"type": "textarea", "label": "Deferred Items", "multi": True, "placeholder": "One item per line"},
	"items_json": {"type": "json", "label": "Items JSON", "placeholder": "[]"},
	"priority": {"type": "select", "label": "Priority", "choices": ["Low", "Medium", "High", "Critical"]},
	"ref_doctype": {"type": "text", "label": "Reference DocType"},
	"ref_name": {"type": "text", "label": "Reference Name"},
	"item_code": {"type": "text", "label": "Item Code"},
	"details": {"type": "textarea", "label": "Details"},
	"dedup_key": {"type": "text", "label": "Deduplication Key", "placeholder": "e.g. pnl_margin:2026-11-17"},
	"snooze_minutes": {"type": "int", "label": "Snooze Minutes", "min": 1, "placeholder": "60"},
	"metric": {"type": "text", "label": "Metric Key", "placeholder": "pnl_health or tomorrow_readiness"},
	"score": {"type": "int", "label": "Health Score (0-100)", "min": 0, "placeholder": "0..100"},
	"grade": {"type": "select", "label": "Grade", "choices": ["Healthy", "Watch", "Critical"]},
	"verdict": {"type": "textarea", "label": "Verdict (1-Line Driver)"},
	"period_date": {"type": "date", "label": "Period Date (YYYY-MM-DD)", "placeholder": "YYYY-MM-DD"},
	"report_url": {"type": "text", "label": "Frappe Report URL"},
	"details_json": {"type": "json", "label": "Details JSON", "placeholder": "{}"},
	"action_item": {"type": "text", "label": "Action Item ID"},
	"template": {"type": "text", "label": "Inspection Template"},
	"replace": {"type": "bool", "label": "Replace Existing"},
	"concern": {"type": "textarea", "label": "Customer Concern"},
	"auto_apply": {"type": "bool", "label": "Auto-Apply Template"},
	"draft": {"type": "bool", "label": "Keep in Draft"},
	"purchase_order": {"type": "text", "label": "Purchase Order", "placeholder": "PUR-ORD-..."},
	"purchase_invoice": {"type": "text", "label": "Purchase Invoice", "placeholder": "ACC-PINV-..."},
	"submit_po": {"type": "bool", "label": "Submit Purchase Order"},
	"supplier": {"type": "text", "label": "Supplier"},
	"update_stock": {"type": "bool", "label": "Update Stock"},
	"bill_no": {"type": "text", "label": "Supplier Invoice / Bill No"},
	"bill_date": {"type": "date", "label": "Bill Date (YYYY-MM-DD)", "placeholder": "YYYY-MM-DD"},
	"no_attach_advances": {"type": "bool", "label": "Do Not Attach Advances"},
	"file": {"type": "text", "label": "Catalog File Path"},
	"source": {"type": "select", "label": "Part Source", "choices": ["OEM", "Aftermarket"]},
	"condition": {"type": "select", "label": "Part Condition", "choices": ["New", "Used", "Reconditioned"]},
	"service_package": {"type": "text", "label": "Service Package"},
	"url": {"type": "text", "label": "Tesla Manual URL", "placeholder": "https://service.tesla.com/..."},
	"data_json": {"type": "json", "label": "Data JSON", "placeholder": "{}"},
	"code": {"type": "text", "label": "Correction Code"},
	"model": {"type": "text", "label": "Vehicle Model"},
	"generation": {"type": "text", "label": "Vehicle Generation", "placeholder": "e.g. Model 3 (2017-2023)"},
	"on_conflict": {"type": "select", "label": "On Conflict", "choices": ["skip", "overwrite", "add", "error"]},
	"force": {"type": "bool", "label": "Force"},
	"side": {"type": "select", "label": "Side", "choices": ["", "LH", "RH", "Both"]},
	"supplement": {"type": "bool", "label": "Add as Supplement"},
	"primary": {"type": "bool", "label": "Primary Owner"},
	"relationship": {"type": "text", "label": "Relationship"},
	"doctype": {"type": "text", "label": "DocType"},
	"name": {"type": "text", "label": "Document Name"},
	"method": {"type": "text", "label": "Frappe Method"},
	"args_json": {"type": "json", "label": "Arguments JSON", "placeholder": "{}"},
	"thread_key": {"type": "text", "label": "Thread Key", "placeholder": "RO:RO-2026-00011"},
	"text": {"type": "textarea", "label": "Capture Text", "placeholder": "Message or observation to post"},
	"intent": {"type": "text", "label": "Intent Tag", "placeholder": "Optional quick-intent tag"},
	"idempotency_key": {"type": "text", "label": "Idempotency Key"},
	"run_ai": {"type": "bool", "label": "Run Site Board AI"},
	"bin_code": {"type": "text", "label": "Bin Code", "placeholder": "e.g. A2-03"},
	"bin_location": {"type": "text", "label": "Bin Location", "placeholder": "e.g. A2-03"},
	"from_bin": {"type": "text", "label": "From Bin", "placeholder": "e.g. RCV-01"},
	"to_bin": {"type": "text", "label": "To Bin", "placeholder": "e.g. A2-03"},
	"bin_type": {"type": "select", "label": "Bin Type", "choices": ["Standard", "Overflow", "Receiving", "Bulk", "Hazmat"]},
	"bin_allocations_json": {"type": "json", "label": "Bin Allocations JSON", "placeholder": '{"ITEM-CODE": "A2-03"}'},
	"purpose": {"type": "select", "label": "Adjustment Purpose", "choices": ["receipt", "issue"]},
	"zone": {"type": "text", "label": "Zone", "placeholder": "e.g. A"},
	"aisle": {"type": "text", "label": "Aisle", "placeholder": "e.g. 2"},
	"rack": {"type": "text", "label": "Rack", "placeholder": "e.g. R1"},
	"shelf": {"type": "text", "label": "Shelf", "placeholder": "e.g. 03"},
	"reason": {"type": "textarea", "label": "Reason", "placeholder": "Cycle count or stock adjustment reason"},
	"warehouse": {"type": "text", "label": "Warehouse", "placeholder": "Shop Store - ISM"},
	"qty": {"type": "float", "label": "Quantity", "min": 0},
	"description": {"type": "textarea", "label": "Description"},
}

_STAGE_CMD_FIELD_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
	("appointment-status", "status"): {
		"type": "select",
		"label": "Status",
		"choices": ["Draft", "Scheduled", "Confirmed", "In Progress", "Completed", "No Show", "Cancelled"],
	},
	("duplicate-side", "service_row"): {
		"type": "text",
		"label": "Service Row",
		"multi": False,
		"placeholder": "Service row ID or 1-based index",
	},
	("sync-action", "action"): {
		"type": "select",
		"label": "Sync Action",
		"choices": ["snooze", "dismiss", "reopen"],
	},
	("create-action", "source"): {
		"type": "select",
		"label": "Source",
		"choices": ["Manual", "Agent", "Board AI", "Rule Snooze"],
	},
}

_STAGE_POSITIONAL_FIELDS: dict[str, set[str]] = {
	"deposit": {"repair_order"},
	"ingest-parts": {"file"},
	"fetch-tesla": {"url"},
	"ingest-tesla": {"url"},
}


def _default_hint_for_field(cmd: str, field: str, raw_val: Any) -> dict[str, Any]:
	override = _STAGE_CMD_FIELD_OVERRIDES.get((cmd, field))
	if override:
		return dict(override)
	base = STAGE_FIELD_HINTS.get(field)
	if base:
		return dict(base)
	if isinstance(raw_val, bool):
		ftype = "bool"
	elif isinstance(raw_val, int):
		ftype = "int"
	elif isinstance(raw_val, float):
		ftype = "float"
	else:
		ftype = "text"
	return {"type": ftype, "label": field.replace("_", " ").title()}


def build_stage_form_spec(
	parsed_items: list[argparse.Namespace],
	parser: Optional[argparse.ArgumentParser] = None,
) -> dict[str, Any]:
	"""Build typed form schema and pre-populated step definitions for --stage UI."""
	del parser  # Reserved for optional parser introspection; metadata drives field schemas.
	steps: list[dict[str, Any]] = []
	commands_summary: list[dict[str, Any]] = []
	all_missing: list[str] = []
	has_writes = False

	for idx, sub_args in enumerate(parsed_items):
		cmd = getattr(sub_args, "command", "unknown")
		meta = WRITE_COMMAND_META.get(cmd)
		is_write = cmd not in READ_ONLY_COMMANDS
		if is_write:
			has_writes = True

		missing = validate_write_command_args(sub_args, allow_step_refs=True)
		if cmd in READ_ONLY_COMMANDS:
			missing.extend(validate_temporal_args(sub_args, allow_step_refs=True))
		all_missing.extend(f"{cmd}:{m}" for m in missing)

		fields_list = meta["fields"] if meta else tuple(
			k
			for k in vars(sub_args).keys()
			if k not in ("command", "env_file", "json", "summary", "dry_run", "stage")
		)
		required_attrs = {attr for attr, _ in (meta.get("required", ()) if meta else ())}
		required_attrs.update(attr for attr, _ in (meta.get("required_bool", ()) if meta else ()))
		non_neg_attrs = {attr for attr, _ in (meta.get("non_neg_ints", ()) if meta else ())}
		if cmd == "job-status" and getattr(sub_args, "action", None) == "complete":
			required_attrs.add("odometer_out")
			non_neg_attrs.add("odometer_out")

		any_of_groups: list[dict[str, Any]] = []
		any_of_by_field: dict[str, str] = {}
		if meta:
			for group_idx, (choices, label) in enumerate(meta.get("any_of", ())):
				satisfied = any(_has_value(getattr(sub_args, f, None)) for f in choices)
				any_of_groups.append({
					"id": f"any_of_{group_idx}",
					"fields": list(choices),
					"label": label,
					"satisfied": satisfied,
				})
				for f in choices:
					any_of_by_field[f] = label

		positional_set = _STAGE_POSITIONAL_FIELDS.get(cmd, set())
		field_specs: list[dict[str, Any]] = []
		values: dict[str, Any] = {}
		payload: dict[str, Any] = {}

		for field in fields_list:
			val = getattr(sub_args, field, None)
			flag = field if field in positional_set else f"--{field.replace('_', '-')}"
			is_missing_field = any(
				m in (flag, field) or m.startswith((f"{flag} ", f"{field} "))
				for m in missing
			)
			hint = _default_hint_for_field(cmd, field, val)
			if field in non_neg_attrs and "min" not in hint:
				hint["min"] = 0

			has_val = _has_value(val) or val == 0
			is_invalid = bool(is_missing_field and has_val)
			is_missing = bool(is_missing_field and not has_val)
			is_req = field in required_attrs or field in non_neg_attrs or is_missing_field

			norm_val = list(val) if hint.get("multi") and isinstance(val, (list, tuple)) else val
			values[field] = norm_val
			if is_missing:
				payload[field] = None
			elif has_val:
				payload[field] = norm_val

			field_specs.append({
				"name": field,
				"flag": flag,
				"positional": field in positional_set,
				"label": hint.get("label") or field.replace("_", " ").title(),
				"type": hint.get("type", "text"),
				"required": is_req,
				"missing": is_missing,
				"invalid": is_invalid,
				"value": norm_val,
				"placeholder": hint.get("placeholder"),
				"choices": hint.get("choices"),
				"suggestions": hint.get("suggestions"),
				"multi": bool(hint.get("multi", False)),
				"min": hint.get("min"),
				"any_of_group": any_of_by_field.get(field),
				"is_step_ref": _contains_step_ref(norm_val),
			})

		target = meta["target"] if meta else f"Read-only command ({cmd})"
		steps.append({
			"index": idx,
			"command": cmd,
			"is_write": is_write,
			"target": target,
			"ready": len(missing) == 0,
			"missing_required": list(missing),
			"any_of": any_of_groups,
			"fields": field_specs,
			"values": values,
		})
		commands_summary.append({
			"command": cmd,
			"is_write": is_write,
			"target": target,
			"ready": len(missing) == 0,
			"missing_required": list(missing),
			"payload": payload,
		})

	return {
		"ready": len(all_missing) == 0,
		"has_writes": has_writes,
		"missing_required": all_missing,
		"steps": steps,
		"commands_summary": commands_summary,
	}

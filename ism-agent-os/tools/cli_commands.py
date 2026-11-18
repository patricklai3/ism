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
from .cli_validation import (
	READ_ONLY_COMMANDS,
	raise_if_corrupted_step_ref,
	validate_temporal_args,
	validate_write_command_args,
)
from .client import IsmAgentClient, IsmApiError
from .monitoring import build_cockpit_view
from .pricing import calculate_part_price, load_pricing_config
from .stage_store import extract_receipt_metadata, read_latest_receipt, resolve_staged_dir

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
		update_services=_parse_json_opt(a.update_services, expected_type=list),
		notes=a.notes,
		discount_amount=a.discount_amount,
		shop_supplies_amount=a.shop_supplies_amount,
		environmental_fee_amount=a.environmental_fee_amount,
		include_summary=s,
	)


def _canonical_item_code(part_source: str, part_number: str, part_condition: str) -> str:
	src = "OEM" if str(part_source or "").strip().upper().startswith("OEM") else "AFT"
	cond_low = str(part_condition or "New").strip().lower()
	cond = "USED" if "used" in cond_low else ("REC" if cond_low.startswith(("rec", "rem", "reb")) else "NEW")
	clean_part = re.sub(r"[^A-Za-z0-9_\-]", "", str(part_number or "").strip().upper()) or "UNKNOWN"
	return f"{src}-{clean_part}-{cond}"


def _cmd_price_part(a: argparse.Namespace, _c: Optional[IsmAgentClient], _s: bool) -> dict[str, Any]:
	cfg = load_pricing_config(a.config) if getattr(a, "config", None) else None
	result = calculate_part_price(
		tesla_oem_price=a.tesla_price,
		cost=a.cost,
		part_source=a.source,
		part_condition=a.condition,
		override_rate=a.override_rate,
		part_number=a.part_number,
		part_name=a.part_name,
		config=cfg,
	)
	return {"success": True, "data": result}


def _cmd_ingest_parts(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	raw = _read_utf8_file(a.file, label="Parts catalog")
	sanitizer = SupplierCatalogSanitizer()
	pricing_cfg = load_pricing_config(a.pricing_config) if getattr(a, "pricing_config", None) else None
	preserve_rate = bool(getattr(a, "preserve_rate", False))
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
			raw_items,
			default_source=a.source,
			default_condition=a.condition,
			preserve_explicit_rate=preserve_rate,
			pricing_config=pricing_cfg,
		)
	else:
		items = sanitizer.parse_rows(
			raw,
			default_source=a.source,
			default_condition=a.condition,
			preserve_explicit_rate=preserve_rate,
			pricing_config=pricing_cfg,
		)
	if not items:
		raise ValueError(f"No valid parts found in '{a.file}'.")

	uniform_tesla = getattr(a, "tesla_price", None)
	if uniform_tesla is not None and float(uniform_tesla) > 0.0:
		for item in items:
			if "_pricing" in item:
				continue
			existing_rate = float(item.get("unit_rate") or 0.0)
			override_val = existing_rate if (preserve_rate and existing_rate > 0.0) else None
			cost_val = float(item.get("valuation_rate") or 0.0)
			pricing_meta = calculate_part_price(
				tesla_oem_price=float(uniform_tesla),
				cost=cost_val if cost_val > 0.0 else None,
				part_source=item.get("part_source", a.source),
				part_condition=item.get("part_condition", a.condition),
				override_rate=override_val,
				part_number=item.get("oem_part_number") or item.get("part_number"),
				part_name=item.get("part_name"),
				config=pricing_cfg,
			)
			item["unit_rate"] = pricing_meta["sell_price"]
			item["_pricing"] = pricing_meta

	pricing_summary: list[dict[str, Any]] = []
	frappe_items: list[dict[str, Any]] = []
	for item in items:
		meta = item.get("_pricing")
		if isinstance(meta, dict):
			pricing_summary.append({"part_number": item.get("part_number"), **meta})
		frappe_items.append({k: v for k, v in item.items() if k != "_pricing"})

	res = c.import_supplier_catalog(
		items=frappe_items,
		supplier=a.supplier,
		default_source=a.source,
		default_condition=a.condition,
		service_package=a.service_package,
		include_summary=s,
	)
	if bool(getattr(a, "sync_rates", False)):
		synced: list[str] = []
		for item in frappe_items:
			rate_val = float(item.get("unit_rate") or 0.0)
			if rate_val <= 0.0:
				continue
			code = _canonical_item_code(
				item.get("part_source", a.source),
				item.get("part_number", ""),
				item.get("part_condition", a.condition),
			)
			c.update_document("Item", code, {"standard_rate": rate_val})
			synced.append(code)
		if isinstance(res, dict):
			res["synced_rates"] = synced
	if pricing_summary and isinstance(res, dict):
		res["pricing_summary"] = pricing_summary
	return res


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
	if not rows and bool(getattr(a, "all", False)):
		ro_resp = c.get_document("Repair Order", a.repair_order)
		ro_doc = ro_resp.get("data") if isinstance(ro_resp, dict) and isinstance(ro_resp.get("data"), dict) else ro_resp
		services = (ro_doc.get("services") or []) if isinstance(ro_doc, dict) else []
		rows = [
			str(row["name"]).strip()
			for row in services
			if isinstance(row, dict)
			and row.get("name")
			and str(row.get("status") or "").strip() not in ("Declined", "Deferred", "Estimating", "Pending Approval")
		]
		if not rows:
			rows = [
				str(row["name"]).strip()
				for row in services
				if isinstance(row, dict)
				and row.get("name")
				and str(row.get("status") or "").strip() not in ("Declined", "Deferred")
			]
	if not rows:
		raise ValueError("Provide --service-row, --service-rows-json, or --all.")
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


_JSON_ARRAY_FLAGS: frozenset[str] = frozenset({"--update-services", "--payments"})
_POSITIONAL_ARG_BY_CMD: dict[str, str] = {
	"deposit": "repair_order",
	"ingest-parts": "file",
	"fetch-tesla": "url",
	"ingest-tesla": "url",
}
_STEP_PATH_RE = r"[A-Za-z0-9_-]+(?:\[\d+\])*(?:\.[A-Za-z0-9_-]+(?:\[\d+\])*)*"
_STEP_TOKEN_RE = re.compile(
	rf"(?:"
	rf"\$\{{(?P<b_target>step\d+|prev)(?:\.(?P<b_path>{_STEP_PATH_RE}))?\}}"
	rf"|\$(?P<d_target>step\d+|prev)(?:\.(?P<d_path>{_STEP_PATH_RE}))?(?![A-Za-z0-9_])"
	rf"|(?<![A-Za-z0-9._%+-])@(?P<a_target>step\d+|prev)(?:\.(?P<a_path>{_STEP_PATH_RE}))?(?![A-Za-z0-9_@-])"
	rf"|(?<![A-Za-z0-9_.$@-])(?P<r_target>step\d+|prev)\.(?P<r_path>{_STEP_PATH_RE})(?![A-Za-z0-9_@-])"
	rf")"
)
_AT_LAST_RE = re.compile(
	rf"(?<![A-Za-z0-9._%+-])@last(?:\.({_STEP_PATH_RE}))?(?![A-Za-z0-9_@-])"
)


def _extract_step_match(m: re.Match[str]) -> tuple[str, Optional[str]]:
	for prefix in ("b", "d", "a", "r"):
		target = m.group(f"{prefix}_target")
		if target is not None:
			return target, m.group(f"{prefix}_path")
	return "", None


def _traverse_path(obj: Any, path: str) -> tuple[bool, Any]:
	cur = obj
	norm_path = re.sub(r"\[(\d+)\]", r".\1", path).lstrip(".")
	for seg in norm_path.split("."):
		if isinstance(cur, dict) and seg in cur:
			cur = cur[seg]
		elif isinstance(cur, (list, tuple)):
			try:
				idx = int(seg)
			except ValueError:
				return (False, None)
			if idx < 0 or idx >= len(cur):
				return (False, None)
			cur = cur[idx]
		else:
			return (False, None)
	return (True, cur)


def _stringify_resolved_value(val: Any) -> str:
	if isinstance(val, (dict, list)):
		return json.dumps(val)
	if val is None:
		return ""
	return str(val)


def _lookup_at_last_token(path: Optional[str], receipt: dict[str, Any]) -> Any:
	if not path:
		primary = receipt.get("primary_id")
		if not primary and isinstance(receipt.get("data"), dict):
			primary = receipt["data"].get("name")
		if not primary:
			raise ValueError("Latest staged receipt has no primary_id to resolve '@last'.")
		return primary

	for candidate in (receipt, receipt.get("ids"), receipt.get("data"), receipt.get("result")):
		if isinstance(candidate, dict):
			found, val = _traverse_path(candidate, path)
			if found and val is not None:
				return val
	raise ValueError(f"Could not resolve '@last.{path}' from latest staged receipt.")


def resolve_at_last_value(
	val: Any,
	*,
	staged_dir: Optional[str | Path] = None,
	_receipt_cache: Optional[dict[str, Any]] = None,
) -> Any:
	"""Recursively resolve @last and @last.<field> tokens from .ism_staged/latest.json."""
	cache = _receipt_cache if _receipt_cache is not None else {}

	def _get_receipt() -> dict[str, Any]:
		if "receipt" not in cache:
			resp = read_latest_receipt(staged_dir=staged_dir)
			rec = resp.get("data")
			if not isinstance(rec, dict):
				raise ValueError("Latest staged receipt is empty.")
			cache["receipt"] = rec
		return cache["receipt"]

	if isinstance(val, str):
		if not _AT_LAST_RE.search(val):
			return val
		stripped = val.strip()
		exact = _AT_LAST_RE.fullmatch(stripped)
		if exact:
			return _lookup_at_last_token(exact.group(1), _get_receipt())
		return _AT_LAST_RE.sub(
			lambda m: _stringify_resolved_value(_lookup_at_last_token(m.group(1), _get_receipt())),
			val,
		)
	if isinstance(val, list):
		return [resolve_at_last_value(x, staged_dir=staged_dir, _receipt_cache=cache) for x in val]
	if isinstance(val, tuple):
		return tuple(resolve_at_last_value(x, staged_dir=staged_dir, _receipt_cache=cache) for x in val)
	if isinstance(val, dict):
		return {
			k: resolve_at_last_value(v, staged_dir=staged_dir, _receipt_cache=cache)
			for k, v in val.items()
		}
	return val


def resolve_namespace_at_last(
	args: argparse.Namespace,
	*,
	staged_dir: Optional[str | Path] = None,
) -> argparse.Namespace:
	"""Resolve @last tokens across parsed argparse.Namespace attributes."""
	cache: dict[str, Any] = {}
	for key, val in list(vars(args).items()):
		if key in ("command", "env_file", "json", "summary", "dry_run", "stage"):
			continue
		if val is not None:
			setattr(args, key, resolve_at_last_value(val, staged_dir=staged_dir, _receipt_cache=cache))
	return args


def _iter_strings(value: Any):
	if isinstance(value, str):
		yield value
	elif isinstance(value, (list, tuple)):
		for elem in value:
			yield from _iter_strings(elem)
	elif isinstance(value, dict):
		for elem in value.values():
			yield from _iter_strings(elem)
	elif isinstance(value, argparse.Namespace):
		for k, v in vars(value).items():
			if k not in ("command", "env_file", "json", "summary", "dry_run", "stage"):
				yield from _iter_strings(v)


def validate_batch_step_references(items: list[Any], *, is_batch: bool = True) -> None:
	"""Validate @stepN/stepN/$stepN and @prev/prev/$prev reference bounds before staging or executing."""
	for idx, item in enumerate(items):
		for text in _iter_strings(item):
			raise_if_corrupted_step_ref(text)
			for match in _STEP_TOKEN_RE.finditer(text):
				target, _ = _extract_step_match(match)
				if not is_batch:
					raise ValueError(
						f"Step reference '@{target}' is only supported inside 'batch' commands."
					)
				if target == "prev":
					if idx == 0:
						raise ValueError("Step 0 cannot reference '@prev'.")
				elif target.startswith("step"):
					ref_idx = int(target[4:])
					if ref_idx >= idx:
						valid = f"0..{idx - 1}" if idx > 0 else "none"
						raise ValueError(
							f"Step {idx} cannot reference future/current '@step{ref_idx}' (valid: {valid})."
						)


def _lookup_step_token(
	target: str,
	path: Optional[str],
	step_idx: int,
	prior_results: list[dict[str, Any]],
) -> Any:
	ref_idx = (step_idx - 1) if target == "prev" else int(target[4:])
	if ref_idx < 0 or ref_idx >= step_idx or ref_idx >= len(prior_results):
		raise ValueError(f"Step {step_idx} references out-of-range '${target}'.")
	prior = prior_results[ref_idx]
	if not prior.get("ok"):
		raise ValueError(
			f"Cannot resolve '${target}' at step {step_idx}: step {ref_idx} did not succeed."
		)
	res_obj = prior.get("result")
	if not path:
		meta = extract_receipt_metadata(
			res_obj if isinstance(res_obj, dict) else {},
			mode="single",
			commands=[str(prior.get("command") or "")],
		)
		primary = meta.get("primary_id")
		if not primary:
			raise ValueError(f"Step {ref_idx} produced no primary ID for '${target}'.")
		return primary

	clean_path = path
	if clean_path.startswith("result.") and (not isinstance(res_obj, dict) or "result" not in res_obj):
		clean_path = clean_path[len("result.") :]
	if isinstance(res_obj, dict):
		found, resolved = _traverse_path(res_obj, clean_path)
		if found:
			return resolved
		if isinstance(res_obj.get("data"), dict):
			found, resolved = _traverse_path(res_obj["data"], clean_path)
			if found:
				return resolved
	raise ValueError(f"Could not resolve '${target}.{path}' from step {ref_idx} result.")


def resolve_step_tokens(value: Any, step_idx: int, prior_results: list[dict[str, Any]]) -> Any:
	"""Recursively resolve @stepN/stepN/$stepN and @prev/prev/$prev tokens against prior batch outputs."""
	if isinstance(value, str):
		if not _STEP_TOKEN_RE.search(value):
			return value
		stripped = value.strip()
		exact = _STEP_TOKEN_RE.fullmatch(stripped)
		if exact:
			target, path = _extract_step_match(exact)
			return _lookup_step_token(target, path, step_idx, prior_results)

		def _replace(m: re.Match[str]) -> str:
			target, path = _extract_step_match(m)
			return _stringify_resolved_value(_lookup_step_token(target, path, step_idx, prior_results))

		return _STEP_TOKEN_RE.sub(_replace, value)
	if isinstance(value, list):
		return [resolve_step_tokens(x, step_idx, prior_results) for x in value]
	if isinstance(value, tuple):
		return tuple(resolve_step_tokens(x, step_idx, prior_results) for x in value)
	if isinstance(value, dict):
		return {k: resolve_step_tokens(v, step_idx, prior_results) for k, v in value.items()}
	return value


def _resolve_batch_item_to_argv(
	item: Any,
	step_idx: int,
	prior_results: list[dict[str, Any]],
	*,
	staged_dir: Optional[str | Path] = None,
) -> list[str]:
	"""Convert a batch item to argv and resolve @last / $stepN / $prev references safely."""
	cache: dict[str, Any] = {}
	if isinstance(item, (dict, list)):
		resolved_item = resolve_at_last_value(item, staged_dir=staged_dir, _receipt_cache=cache)
		resolved_item = resolve_step_tokens(resolved_item, step_idx, prior_results)
		return _batch_item_to_argv(resolved_item)
	argv_item = _batch_item_to_argv(item)
	out: list[str] = []
	for tok in argv_item:
		tok_val = resolve_at_last_value(tok, staged_dir=staged_dir, _receipt_cache=cache)
		tok_val = resolve_step_tokens(tok_val, step_idx, prior_results)
		out.append(_stringify_resolved_value(tok_val))
	return out


def _dict_args_to_argv(cmd: str, kwargs: dict[str, Any]) -> list[str]:
	"""Convert a command name and flag dictionary into CLI argv tokens."""
	argv = [cmd]
	positional_key = _POSITIONAL_ARG_BY_CMD.get(cmd)
	if positional_key and kwargs.get(positional_key) is not None and str(kwargs[positional_key]).strip() != "":
		argv.append(str(kwargs[positional_key]))
	for key, val in kwargs.items():
		if key == positional_key:
			continue
		if val is None or val is False:
			continue
		flag = f"--{str(key).lstrip('-').replace('_', '-')}"
		if val is True:
			argv.append(flag)
		elif isinstance(val, (list, tuple)):
			if flag.endswith("-json") or flag in _JSON_ARRAY_FLAGS:
				argv.extend([flag, json.dumps(val)])
			else:
				for elem in val:
					elem_str = str(elem)
					if elem_str.startswith("-"):
						argv.append(f"{flag}={elem_str}")
					else:
						argv.extend([flag, elem_str])
		elif isinstance(val, dict):
			argv.extend([flag, json.dumps(val)])
		else:
			val_str = str(val)
			if val_str.startswith("-"):
				argv.append(f"{flag}={val_str}")
			else:
				argv.extend([flag, val_str])
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
	if bool(getattr(sub_args, "stage", False)):
		raise ValueError("Nested '--stage' is not allowed inside batch subcommands.")
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
			resolved_argv = _resolve_batch_item_to_argv(item, idx, results)
			cmd_label, sub_args = _parse_batch_subcommand(resolved_argv, parser, suppress_stderr=True)
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


def _cmd_stage_receipt(a: argparse.Namespace, _c: Optional[IsmAgentClient], _s: bool) -> dict[str, Any]:
	return read_latest_receipt(stage_id=getattr(a, "stage_id", None))


def _cmd_cockpit(a: argparse.Namespace, c: Optional[IsmAgentClient], s: bool) -> dict[str, Any]:
	if getattr(a, "data_only", False):
		if c is None:
			raise ValueError("Frappe client is required for cockpit --data-only.")
		snap = c.get_cockpit_snapshot(date=getattr(a, "date", None), include_summary=s)
		if isinstance(snap, dict) and snap.get("success") is False:
			return snap
		view = build_cockpit_view(snap)
		out: dict[str, Any] = {"success": True, **view}
		if s and isinstance(snap, dict) and snap.get("summary_card"):
			out["summary_card"] = snap["summary_card"]
		return out

	from .stage_server import ensure_server_running

	raw_env = getattr(a, "env_file", None)
	resolved_env = str(Path(raw_env).expanduser().resolve()) if raw_env else None
	staged_dir = resolve_staged_dir()
	srv = ensure_server_running(staged_dir, require_cockpit=True, env_file=resolved_env)
	url = f"http://127.0.0.1:{srv['port']}/cockpit?token={srv['cockpit_token']}"
	if getattr(a, "date", None):
		url += f"&date={a.date}"
	return {
		"success": True,
		"url": url,
		"port": srv["port"],
		"date": getattr(a, "date", None),
	}


def _cmd_publish_score(a: argparse.Namespace, c: IsmAgentClient, s: bool) -> dict[str, Any]:
	details = _parse_json_opt(getattr(a, "details_json", None))
	return c.publish_health_snapshot(
		metric_key=a.metric,
		score=int(a.score),
		verdict=a.verdict,
		grade=a.grade,
		period_date=a.period_date,
		report_url=a.report_url,
		details=details,
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
	"deposit": lambda a, c, s: c.record_ro_deposit(
		repair_order=a.repair_order,
		amount=a.amount,
		payment_method=a.payment_method,
		reference_no=a.reference_no,
		include_summary=s,
	),
	"checkout": lambda a, c, s: c.checkout_repair_order(
		repair_order=a.repair_order,
		payment_method=a.payment_method,
		paid_amount=a.paid_amount,
		odometer_out=a.odometer_out,
		payments=_parse_json_opt(a.payments, expected_type=list),
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
		dedup_key=a.dedup_key,
		source=a.source,
		include_summary=s,
	),
	"resolve-action": lambda a, c, s: c.resolve_shop_action_item(
		action_item=a.action_item,
		resolution_notes=a.notes,
		include_summary=s,
	),
	"sync-action": lambda a, c, s: c.sync_action_item_state(
		dedup_key=a.dedup_key,
		action=a.action,
		snooze_minutes=a.snooze_minutes,
		title=a.title,
		notes=a.notes,
		reference_doctype=a.ref_doctype,
		reference_name=a.ref_name,
		include_summary=s,
	),
	"cockpit": _cmd_cockpit,
	"pnl-digest": lambda a, c, s: c.get_pnl_digest_inputs(
		from_date=a.from_date,
		to_date=a.to_date,
		company=a.company,
		include_summary=s,
	),
	"publish-score": _cmd_publish_score,
	"ingest-parts": _cmd_ingest_parts,
	"price-part": _cmd_price_part,
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
	"stage-receipt": _cmd_stage_receipt,
}


def _print_result(res: Any, raw_json: bool = False) -> None:
	if not raw_json and isinstance(res, dict) and res.get("summary_card"):
		print(res["summary_card"])
	else:
		print(json.dumps(res, indent=2, default=str))

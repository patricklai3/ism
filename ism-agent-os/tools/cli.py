#!/usr/bin/env python3
"""Command-line interface for ISM Agent OS operations over direct Frappe REST."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if not __package__:
	_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
	if _REPO_ROOT not in sys.path:
		sys.path.insert(0, _REPO_ROOT)
	__package__ = "tools"

from .cli_commands import (
	_COMMAND_HANDLERS,
	_batch_item_to_argv,
	_cmd_batch,
	_cmd_ingest_parts,
	_dict_args_to_argv,
	_load_batch_items,
	_parse_batch_subcommand,
	_print_result,
	resolve_namespace_at_last,
	SupplierCatalogSanitizer,
	TeslaManualParser,
	validate_batch_step_references,
)
from .cli_parsers import _build_parser
from .cli_parsing import (
	_flatten_csv_items,
	_flatten_service_rows,
	_loads_json_with_ps_fallback,
	_parse_fields_opt,
	_parse_filters_opt,
	_parse_json_opt,
	_parse_service_specs,
	_repair_unquoted_json,
)
from .cli_validation import (
	READ_ONLY_COMMANDS,
	STAGE_FIELD_HINTS,
	WRITE_COMMAND_META,
	_check_non_negative_int_arg,
	_has_value,
	build_pre_dispatch_summary,
	build_stage_form_spec,
	validate_temporal_args,
	validate_write_command_args,
)
from .client import IsmAgentClient, IsmApiError
from .config import IsmConfig
from .diagnostics import run_diagnostics
from .stage_server import ensure_server_running
from .stage_store import create_stage_record, resolve_staged_dir

__all__ = [
	"IsmAgentClient",
	"IsmApiError",
	"IsmConfig",
	"READ_ONLY_COMMANDS",
	"STAGE_FIELD_HINTS",
	"SupplierCatalogSanitizer",
	"TeslaManualParser",
	"WRITE_COMMAND_META",
	"_COMMAND_HANDLERS",
	"_batch_item_to_argv",
	"_build_parser",
	"_check_non_negative_int_arg",
	"_cmd_batch",
	"_cmd_ingest_parts",
	"_dict_args_to_argv",
	"_flatten_csv_items",
	"_flatten_service_rows",
	"_has_value",
	"_load_batch_items",
	"_loads_json_with_ps_fallback",
	"_parse_batch_subcommand",
	"_parse_fields_opt",
	"_parse_filters_opt",
	"_parse_json_opt",
	"_parse_service_specs",
	"_print_result",
	"_repair_unquoted_json",
	"build_pre_dispatch_summary",
	"build_stage_form_spec",
	"main",
	"run_diagnostics",
	"validate_temporal_args",
	"validate_write_command_args",
]


def _emit_cli_error(exc: BaseException | str, *, raw_json: bool) -> None:
	"""Emit a structured JSON error envelope to stdout when --json is set, in addition to stderr text."""
	msg = str(exc)
	if raw_json:
		if isinstance(exc, IsmApiError):
			details = exc.details if isinstance(exc.details, dict) else {}
			payload: dict[str, Any] = {
				**details,
				"success": False,
				"error": msg,
				"message": msg,
				"status_code": exc.status_code,
			}
		else:
			payload = {"success": False, "error": msg}
		_print_result(payload, raw_json=True)
	print(f"Error: {msg}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
	"""Parse CLI arguments and execute the requested ISM Agent OS command."""
	raw_argv = list(argv) if argv is not None else sys.argv[1:]
	has_stage_flag = "--stage" in raw_argv and not any(tok in ("-h", "--help") for tok in raw_argv)
	parser = _build_parser(relax_required=has_stage_flag)
	args = parser.parse_args(argv)
	stage_mode = bool(getattr(args, "stage", False))
	if has_stage_flag and not stage_mode:
		parser = _build_parser(relax_required=False)
		args = parser.parse_args(argv)

	dry_run = bool(getattr(args, "dry_run", False))
	raw_json = bool(getattr(args, "json", False))
	if stage_mode and dry_run:
		_emit_cli_error("Cannot combine --stage and --dry-run.", raw_json=raw_json)
		return 1

	if stage_mode:
		if args.command in ("test", "verify-connection", "stage-receipt"):
			_emit_cli_error(f"Command '{args.command}' cannot be used with --stage.", raw_json=raw_json)
			return 1
		try:
			if args.command == "batch":
				items = _load_batch_items(args)
				parsed_items = [
					resolve_namespace_at_last(_parse_batch_subcommand(item, parser, suppress_stderr=True)[1])
					for item in items
				]
				validate_batch_step_references(parsed_items, is_batch=True)
				mode = "batch"
			else:
				parsed_items = [resolve_namespace_at_last(args)]
				validate_batch_step_references(parsed_items, is_batch=False)
				mode = "single"

			form_spec = build_stage_form_spec(parsed_items, parser)
			staged_dir = resolve_staged_dir()
			resolved_env_file = str(Path(args.env_file).expanduser().resolve()) if args.env_file else None
			record = create_stage_record(
				form_spec,
				mode=mode,
				env_file=resolved_env_file,
				cwd=str(Path.cwd()),
				staged_dir=staged_dir,
			)
			srv = ensure_server_running(staged_dir)
			url = f"http://127.0.0.1:{srv['port']}/?id={record['stage_id']}&token={record['nonce']}"
			res = {
				"success": True,
				"staged": True,
				"stage_id": record["stage_id"],
				"url": url,
				"ready": form_spec["ready"],
				"missing_required": form_spec["missing_required"],
				"commands": form_spec["commands_summary"],
			}
			_print_result(res, raw_json=True)
			return 0
		except (SystemExit, json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
			_emit_cli_error(exc, raw_json=raw_json)
			return 1

	if dry_run:
		try:
			if args.command == "batch":
				items = _load_batch_items(args)
				parsed_items = [
					_parse_batch_subcommand(item, parser)[1] for item in items
				]
				res = build_pre_dispatch_summary(parsed_items)
			else:
				res = build_pre_dispatch_summary([args])
			_print_result(res, raw_json=args.json)
			return 0
		except (SystemExit, json.JSONDecodeError, OSError, ValueError) as exc:
			_emit_cli_error(exc, raw_json=raw_json)
			return 1

	config = IsmConfig.from_env(env_path=args.env_file)

	if args.command in ("test", "verify-connection"):
		return 0 if run_diagnostics(config) else 1

	show_summary = bool(args.summary and not args.json)
	try:
		if args.command == "batch":
			with IsmAgentClient(config) as client:
				res = _cmd_batch(args, client, show_summary, parser)
			_print_result(res, raw_json=args.json)
			return 0 if res.get("success") else 1

		if args.command not in ("price-part", "stage-receipt"):
			resolve_namespace_at_last(args)
		missing = validate_write_command_args(args)
		if args.command in READ_ONLY_COMMANDS:
			missing.extend(validate_temporal_args(args))
		if missing:
			raise ValueError(
				f"Missing or invalid required field(s) for '{args.command}': {', '.join(missing)}. "
				"Run with --dry-run or --stage to inspect and confirm before dispatching."
			)
		handler = _COMMAND_HANDLERS.get(args.command)
		if not handler:
			return 1
		if args.command in ("price-part", "stage-receipt"):
			res = handler(args, None, show_summary)
		else:
			with IsmAgentClient(config) as client:
				res = handler(args, client, show_summary)
		_print_result(res, raw_json=args.json)
		if isinstance(res, dict) and res.get("success") is False:
			return 1
		return 0
	except (IsmApiError, json.JSONDecodeError, OSError, ValueError) as exc:
		_emit_cli_error(exc, raw_json=raw_json)
		return 1


if __name__ == "__main__":
	sys.exit(main())

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
	WRITE_COMMAND_META,
	_check_non_negative_int_arg,
	_has_value,
	build_pre_dispatch_summary,
	validate_temporal_args,
	validate_write_command_args,
)
from .client import IsmAgentClient, IsmApiError
from .config import IsmConfig
from .diagnostics import run_diagnostics

__all__ = [
	"IsmAgentClient",
	"IsmApiError",
	"IsmConfig",
	"READ_ONLY_COMMANDS",
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
	parser = _build_parser()
	args = parser.parse_args(argv)

	dry_run = bool(getattr(args, "dry_run", False))
	raw_json = bool(getattr(args, "json", False))

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

		missing = validate_write_command_args(args)
		if args.command in READ_ONLY_COMMANDS:
			missing.extend(validate_temporal_args(args))
		if missing:
			raise ValueError(
				f"Missing or invalid required field(s) for '{args.command}': {', '.join(missing)}. "
				"Run with --dry-run to inspect and confirm before dispatching."
			)
		handler = _COMMAND_HANDLERS.get(args.command)
		if not handler:
			return 1
		if args.command == "price-part":
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

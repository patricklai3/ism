#!/usr/bin/env python3
"""Command-line interface for ISM Agent OS operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from ingestion.supplier_catalog import SupplierCatalogSanitizer  # noqa: E402
from ingestion.tesla_manual_parser import TeslaManualParser  # noqa: E402
from tools.client import IsmAgentClient, IsmApiError  # noqa: E402
from tools.config import IsmConfig  # noqa: E402
from tools.ism_mcp_server import IsmMcpServer, run_diagnostics  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)
	config = IsmConfig.from_env(env_path=args.env_file)

	if args.command == "mcp":
		IsmMcpServer(IsmAgentClient(config)).serve_stdio()
		return 0
	if args.command == "test":
		return 0 if run_diagnostics(config) else 1

	client = IsmAgentClient(config)
	try:
		return _run_subcommand(args, client)
	except IsmApiError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		return 1


def _run_subcommand(args: argparse.Namespace, client: IsmAgentClient) -> int:
	if args.command == "status":
		res = client.get_shop_floor_status(date=args.date)
		_print_result(res, raw_json=args.json)
		return 0

	if args.command == "slots":
		res = client.find_available_slots(
			repair_order=args.repair_order,
			duration_minutes=args.duration,
			start_date=args.date,
			days_ahead=args.days,
		)
		_print_result(res, raw_json=args.json)
		return 0

	if args.command == "quote":
		services = list(args.service or [])
		res = client.create_quote(
			customer_name=args.customer,
			vehicle_identifier=args.vehicle,
			services=services or None,
			notes=args.notes,
			customer_phone=args.phone,
		)
		_print_result(res, raw_json=args.json)
		return 0

	if args.command == "ingest-parts":
		raw = Path(args.file).read_text(encoding="utf-8")
		sanitizer = SupplierCatalogSanitizer()
		normalized = sanitizer.normalize_text(raw, default_source=args.source, default_condition=args.condition)
		res = client.import_supplier_catalog(
			raw_text=normalized,
			supplier=args.supplier,
			default_source=args.source,
			default_condition=args.condition,
		)
		_print_result(res, raw_json=args.json)
		return 0

	if args.command == "ingest-tesla":
		parser_engine = TeslaManualParser(client)
		res = parser_engine.ingest_from_url(args.url, model_override=args.model)
		_print_result(res, raw_json=args.json)
		return 0

	return 1


def _print_result(res: dict, raw_json: bool = False) -> None:
	if not raw_json and isinstance(res, dict) and res.get("summary_card"):
		print(res["summary_card"])
	else:
		print(json.dumps(res, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="ism-agent-os", description="ISM Agent OS CLI")
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--json", action="store_true", help="Print raw JSON output")
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("mcp", help="Start Stdio JSON-RPC 2.0 MCP server")
	sub.add_parser("test", help="Verify connection and MCP tools")

	p_status = sub.add_parser("status", help="Show live shop floor status")
	p_status.add_argument("--date", help="Target date (YYYY-MM-DD)")

	p_slots = sub.add_parser("slots", help="Find available appointment slots")
	p_slots.add_argument("--repair-order", help="Repair Order ID")
	p_slots.add_argument("--duration", type=int, help="Duration in minutes")
	p_slots.add_argument("--date", help="Start date (YYYY-MM-DD)")
	p_slots.add_argument("--days", type=int, default=5, help="Days ahead to search")

	p_quote = sub.add_parser("quote", help="Create a Draft Estimate Repair Order")
	p_quote.add_argument("--customer", required=True, help="Customer name or ID")
	p_quote.add_argument("--vehicle", required=True, help="VIN or license plate")
	p_quote.add_argument("--service", action="append", help="Service title or package (repeatable)")
	p_quote.add_argument("--notes", help="Customer concern")
	p_quote.add_argument("--phone", help="Customer phone")

	p_parts = sub.add_parser("ingest-parts", help="Sanitize and import supplier catalog file")
	p_parts.add_argument("file", help="Path to CSV, TSV, or pipe-delimited file")
	p_parts.add_argument("--supplier", help="Supplier name")
	p_parts.add_argument("--source", default="Aftermarket", help="Default source (OEM or Aftermarket)")
	p_parts.add_argument("--condition", default="New", help="Default condition (New, Used, Reman)")

	p_tesla = sub.add_parser("ingest-tesla", help="Fetch and import a Tesla service manual URL")
	p_tesla.add_argument("url", help="https://service.tesla.com/... URL")
	p_tesla.add_argument("--model", help="Override vehicle model (e.g. Model 3)")

	return parser


if __name__ == "__main__":
	sys.exit(main())

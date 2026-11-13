#!/usr/bin/env python3
"""5-step connection and protocol verifier for ISM Agent OS."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from tools.client import IsmAgentClient, IsmApiError  # noqa: E402
from tools.config import IsmConfig  # noqa: E402
from tools.tool_registry import get_mcp_tools  # noqa: E402


def verify(config: IsmConfig, full_e2e: bool = False) -> bool:
	"""Run end-to-end protocol and facade verification against Frappe + ISM."""
	print("=" * 64)
	print("ISM AGENT OS — SITE CONNECTIVITY VERIFICATION")
	print(f"Site URL : {config.base_url}")
	print(f"Transport: {config.transport}")
	print("=" * 64)

	# 1. Config check
	errors = config.validate()
	if errors:
		print("[1/5] Configuration FAILED:")
		for err in errors:
			print(f"  - {err}")
		return False
	masked = config.api_secret[:4] + "..." if len(config.api_secret) > 4 else "***"
	print(f"[1/5] Config OK (key={config.api_key}, secret={masked})")

	client = IsmAgentClient(config)

	# 2. MCP Handshake
	try:
		init_res = client.initialize_mcp()
		server_info = init_res.get("result", {}).get("serverInfo", {})
		print(f"[2/5] Frappe MCP OK ({server_info.get('name', 'frappe')} v{server_info.get('version', '?')})")
	except IsmApiError as exc:
		print(f"[2/5] Frappe MCP unavailable ({exc}); using REST fallback")

	# 3. Tool Registry
	tools = get_mcp_tools()
	print(f"[3/5] Agent OS Tool Registry OK ({len(tools)} tools ready)")

	# 4. Read-only Shop Telemetry & Capacity
	try:
		floor = client.get_shop_floor_status()
		summary = floor.get("summary") or floor.get("data", {}).get("summary", {})
		slots = client.find_available_slots(duration_minutes=60, days_ahead=2)
		slot_count = len(slots.get("slots") or slots.get("data", {}).get("slots", []))
		print(
			f"[4/5] ISM Agent Facade OK — Bays: {summary.get('active_bays', '?')}, "
			f"Techs: {summary.get('active_technicians', '?')}, Slots found: {slot_count}"
		)
	except IsmApiError as exc:
		print(f"[4/5] ISM Agent Facade FAILED: {exc}")
		return False

	# 5. Optional E2E Quote Draft Flow
	if not full_e2e:
		print("[5/5] Write flow skipped (pass --full-e2e to test draft quote creation)")
		return True

	try:
		quote = client.create_quote(
			customer_name="_TEST_ISM_AgentOS_Customer",
			vehicle_identifier="5YJ3E1EBXNF123456",
			services=[{"service_title": "Front Lateral Link Inspection", "labor_hours": 1.0}],
			notes="ISM Agent OS E2E verification draft",
		)
		ro_name = quote.get("repair_order")
		amend = client.amend_quote(ro_name, add_services=[{"service_title": "Alignment Check", "labor_hours": 0.5}])
		approval = client.send_quote_for_approval(ro_name, communication_medium="link")
		print(
			f"[5/5] E2E Quote Flow OK — {ro_name}, "
			f"services={amend.get('total_services')}, "
			f"approval_url={approval.get('urls', {}).get('approval', 'ready')}"
		)
		return True
	except IsmApiError as exc:
		print(f"[5/5] E2E Quote Flow FAILED: {exc}")
		return False


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Verify ISM Agent OS connection")
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--full-e2e", action="store_true", help="Create a _TEST_ISM_ draft quote to verify writes")
	args = parser.parse_args(argv)
	config = IsmConfig.from_env(env_path=args.env_file)
	return 0 if verify(config, full_e2e=args.full_e2e) else 1


if __name__ == "__main__":
	sys.exit(main())

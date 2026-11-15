#!/usr/bin/env python3
"""Demonstrate a complete shop lifecycle orchestrated through ISM Agent OS."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

try:
	from ism_agent_os.client import IsmAgentClient
	from ism_agent_os.config import IsmConfig
except ImportError:
	_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
	if _REPO_ROOT not in sys.path:
		sys.path.insert(0, _REPO_ROOT)
	from tools.client import IsmAgentClient  # noqa: E402
	from tools.config import IsmConfig  # noqa: E402


def run_demo(client: IsmAgentClient, live: bool = False) -> None:
	if not live:
		print("Dry-run preview (pass --live to execute against configured Frappe site):")
		steps = [
			"1. ism-agent-os status -> Inspect active bays & technicians",
			"2. ism-agent-os quote ... -> Create Draft Estimate RO with 13% HST",
			"3. ism-agent-os amend-quote ... -> Append additional service operation",
			"4. ism-agent-os send-approval ... -> Dispatch 48h SMS approval link",
			"5. ism-agent-os slots ... -> Query dual-resource bay & tech capacity",
			"6. ism-agent-os book ... -> Book bay & push ism_calendar_update event",
		]
		print("\n".join(steps))
		return

	floor = client.get_shop_floor_status(include_summary=True)
	print(floor.get("summary_card", floor))

	quote = client.create_quote(
		customer_name="_TEST_ISM_Demo_Customer",
		vehicle_identifier="5YJ3E1EBXNF123456",
		odometer_in=42000,
		services=[{"service_title": "Brake Inspection", "labor_hours": 1.0}],
		notes="Demo intake via ISM Agent OS",
		include_summary=True,
	)
	ro_name = (quote.get("data") or {}).get("name") or quote.get("repair_order")
	print(quote.get("summary_card", quote))

	slots_res = client.find_available_slots(repair_order=ro_name, days_ahead=3, include_summary=True)
	print(slots_res.get("summary_card", slots_res))


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="ISM Agent OS shop flow demo")
	parser.add_argument("--live", action="store_true", help="Execute calls against Frappe")
	args = parser.parse_args(argv)
	with IsmAgentClient(IsmConfig.from_env()) as client:
		run_demo(client, live=args.live)
	return 0


if __name__ == "__main__":
	sys.exit(main())

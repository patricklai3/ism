#!/usr/bin/env python3
"""Demonstrate a complete shop lifecycle orchestrated through ISM Agent OS."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

_AGENT_OS_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_OS_ROOT) not in sys.path:
	sys.path.insert(0, str(_AGENT_OS_ROOT))

try:
	from ism_agent_os.client import IsmAgentClient
	from ism_agent_os.config import IsmConfig
except ImportError:
	from tools.client import IsmAgentClient  # noqa: E402
	from tools.config import IsmConfig  # noqa: E402

from scripts.sim_runner import (  # noqa: E402
	default_offline_eval_manifest,
	interpolate_manifest,
	resolve_manifest_arg,
	resolve_manifest_path,
)


def run_demo(
	client: IsmAgentClient,
	live: bool = False,
	manifest: Optional[dict[str, Any]] = None,
) -> None:
	resolved = manifest if manifest is not None else default_offline_eval_manifest()
	customer_name = str(resolve_manifest_path(resolved, "customers.retail_1"))
	vehicle_vin = str(resolve_manifest_path(resolved, "vehicles.veh_model3_liam"))

	if not live:
		print("Dry-run preview (pass --live to execute against configured Frappe site):")
		templates = [
			"1. ism-agent-os status --date {{metadata.target_date}} -> Inspect active bays ({{bays.main_lift}}) & technicians",
			"2. ism-agent-os quote --customer '{{customers.retail_1}}' --vehicle {{vehicles.veh_model3_liam}} ... -> Create Draft Estimate RO with 13% HST",
			"3. ism-agent-os amend-quote ... -> Append additional service operation",
			"4. ism-agent-os send-approval ... -> Dispatch 48h SMS/link approval",
			"5. ism-agent-os slots --date {{metadata.target_date}} --bay '{{bays.main_lift}}' -> Query dual-resource bay & tech capacity",
			"6. ism-agent-os book --bay '{{bays.main_lift}}' --tech {{techs.marcus}} ... -> Book bay & push ism_calendar_update event",
		]
		print("\n".join(interpolate_manifest(step, resolved) for step in templates))
		return

	floor = client.get_shop_floor_status(include_summary=True)
	print(floor.get("summary_card", floor))

	quote = client.create_quote(
		customer_name=customer_name,
		vehicle_identifier=vehicle_vin,
		odometer_in=48200,
		services=[{"service_title": "Brake Inspection", "labor_hours": 1.0}],
		notes=interpolate_manifest("Demo intake for {{customers.retail_1}} ({{vehicles.veh_model3_liam}}) via ISM Agent OS", resolved),
		include_summary=True,
	)
	ro_name = (quote.get("data") or {}).get("name") or quote.get("repair_order")
	print(quote.get("summary_card", quote))

	slots_res = client.find_available_slots(repair_order=ro_name, days_ahead=3, include_summary=True)
	print(slots_res.get("summary_card", slots_res))


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="ISM Agent OS shop flow demo")
	parser.add_argument("--live", action="store_true", help="Execute calls against Frappe")
	parser.add_argument("--manifest", help="Path to SimManifest JSON file")
	parser.add_argument("--seed-slot", type=int, help="Seed eval_standard in bench slot N and use its SimManifest")
	parser.add_argument("--clean-reinstall", action="store_true", help="Pass -CleanReinstall when seeding via --seed-slot")
	args = parser.parse_args(argv)

	manifest = resolve_manifest_arg(
		manifest_path=args.manifest,
		seed_slot=args.seed_slot,
		clean_reinstall=bool(args.clean_reinstall),
	)
	with IsmAgentClient(IsmConfig.from_env()) as client:
		run_demo(client, live=args.live, manifest=manifest)
	return 0


if __name__ == "__main__":
	sys.exit(main())

"""4-step Frappe REST and ISM Agent Facade diagnostics."""

from __future__ import annotations

from .client import IsmAgentClient, IsmApiError
from .config import IsmConfig


def run_diagnostics(config: IsmConfig, full_e2e: bool = False) -> bool:
	"""Verify config, direct Frappe REST auth, ISM Agent Facade, and optional draft write flow."""
	print("=" * 64)
	print("ISM AGENT OS — REST CONNECTIVITY VERIFICATION")
	print(f"Site URL : {config.base_url}")
	print("Transport: Direct Frappe REST (/api/method & /api/resource)")
	print("=" * 64)

	errors = config.validate()
	if errors:
		print("[1/4] Configuration FAILED:")
		for err in errors:
			print(f"  - {err}")
		return False
	print("[1/4] Config OK (key=***, secret=***)")

	with IsmAgentClient(config) as client:
		if not _check_rest_auth(client):
			return False
		if not _check_agent_facade(client):
			return False
		if not full_e2e:
			print("[4/4] Write flow skipped (pass --full-e2e to test draft quote creation)")
			return True
		return _check_e2e_draft_flow(client)


def _check_rest_auth(client: IsmAgentClient) -> bool:
	try:
		user = client.call_method("frappe.auth.get_logged_user")
		print(f"[2/4] Frappe REST API OK (user={user})")
		return True
	except IsmApiError as exc:
		print(f"[2/4] Frappe REST API FAILED: {exc}")
		return False


def _check_agent_facade(client: IsmAgentClient) -> bool:
	try:
		floor = client.get_shop_floor_status()
		summary = floor.get("summary") or (floor.get("data") or {}).get("summary", {})
		slots = client.find_available_slots(duration_minutes=60, days_ahead=2)
		slot_count = len(slots.get("slots") or (slots.get("data") or {}).get("slots", []))
		print(
			f"[3/4] ISM Agent Facade OK — Bays: {summary.get('active_bays', '?')}, "
			f"Techs: {summary.get('active_technicians', '?')}, Slots found: {slot_count}"
		)
		return True
	except IsmApiError as exc:
		print(f"[3/4] ISM Agent Facade FAILED: {exc}")
		return False


def _check_e2e_draft_flow(client: IsmAgentClient) -> bool:
	try:
		quote = client.create_quote(
			customer_name="_TEST_ISM_AgentOS_Customer",
			vehicle_identifier="5YJ3E1EBXNF123456",
			odometer_in=42000,
			services=[{"service_title": "Front Lateral Link Inspection", "labor_hours": 1.0}],
			notes="ISM Agent OS E2E verification draft",
		)
		ro_name = (quote.get("data") or {}).get("name") or quote.get("repair_order")
		amend = client.amend_quote(ro_name, add_services=[{"service_title": "Alignment Check", "labor_hours": 0.5}])
		approval = client.send_quote_for_approval(ro_name, communication_medium="link")
		print(
			f"[4/4] E2E Quote Flow OK — {ro_name}, "
			f"services={(amend.get('data') or {}).get('total_services')}, "
			f"approval_url={(approval.get('urls') or {}).get('approval', 'ready')}"
		)
		return True
	except IsmApiError as exc:
		print(f"[4/4] E2E Quote Flow FAILED: {exc}")
		return False

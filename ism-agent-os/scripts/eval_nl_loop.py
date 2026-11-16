#!/usr/bin/env python3
"""Blinded natural-language CLI evaluation loop: fixture seeding, DB verification, and KPI scoring."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

from tools.cli import (  # noqa: E402
	READ_ONLY_COMMANDS,
	_batch_item_to_argv,
	_build_parser as _build_cli_parser,
	_load_batch_items,
)
from tools.client import IsmAgentClient  # noqa: E402
from tools.config import IsmConfig  # noqa: E402
from tools.transport import IsmApiError  # noqa: E402

_RESULTS_DIR = Path(__file__).resolve().parent / "eval_results"
_PROTECTED_ROS = {"RO-2026-00009", "RO-2026-00010", "RO-2026-00011", "RO-2026-00067"}
_EVAL_CUSTOMERS = {"_EVAL_AgentOS_Customer", "_TEST_ISM_Demo_Customer"}
_EVAL_SUPPLIERS = {"_EVAL_Supplier"}
_EVAL_PART_ROTOR = "_EVAL_Part_Rotor"
_RO_PATTERN = re.compile(r"\bRO-\d{4}-\d+\b")
_SE_PATTERN = re.compile(r"\bSE-\d+\b")
_PO_PATTERN = re.compile(r"\bPUR-ORD-\d{4}-\d+\b")
_PI_PATTERN = re.compile(r"\b(?:ACC-)?PINV-\d{4}-\d+\b")
_PE_PATTERN = re.compile(r"\bACC-PAY-\d{4}-\d+\b")
_URL_PATTERN = re.compile(r"https?://[^\s\)\]>\"']+")


@dataclass(frozen=True)
class NLScenario:
	"""Definition of a single blinded natural-language evaluation scenario."""

	id: str
	category: str
	nl_prompt: str
	expected_commands: list[str]
	optimal_cli_calls: int
	allowed_dry_runs: int = 0
	is_conflict_scenario: bool = False
	allows_server_feedback_retry: bool = False
	notes: str = ""


def get_scenarios() -> list[NLScenario]:
	"""Return the 15 realistic messy natural-language evaluation scenarios."""
	return [
		NLScenario(
			id="S01_shop_status_relative",
			category="status_relative_date",
			nl_prompt="Hey, what's on the shop floor tomorrow?",
			expected_commands=["status"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Relative date 'tomorrow' should map directly to status --date <YYYY-MM-DD> with no extra tech-overview.",
		),
		NLScenario(
			id="S02_tech_util_leave",
			category="tech_overview_leave",
			nl_prompt="Who's on leave tomorrow and how busy are the techs?",
			expected_commands=["tech-overview"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Should use tech-overview --date <YYYY-MM-DD> and surface Elena on leave + tech utilization.",
		),
		NLScenario(
			id="S03_slots_alignment_rack",
			category="equipment_slots",
			nl_prompt="Customer needs a 1-hour alignment rack slot tomorrow—what's open?",
			expected_commands=["slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Should call slots with --date <YYYY-MM-DD> --duration 60 --tags 'Alignment Rack'.",
		),
		NLScenario(
			id="S04_vin_decode_history",
			category="vin_and_vehicle_intel",
			nl_prompt="Check VIN 5YJ3E1EA1PF200101 and pull its service history.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Optimal: 1 batch call combining decode-vin + vehicle-info (no search-vehicles).",
		),
		NLScenario(
			id="S05_unscheduled_queue",
			category="unscheduled_queue",
			nl_prompt="What services on RO-2026-00011 still need a bay?",
			expected_commands=["unscheduled"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Direct unscheduled --repair-order RO-2026-00011.",
		),
		NLScenario(
			id="S06_direct_book_human_names",
			category="direct_booking",
			nl_prompt="Book RO-2026-00011 into Main Lift tomorrow at 1pm with Devon for 60 min.",
			expected_commands=["book"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			notes="Write-gate: --dry-run book -> confirm -> 1 live book with ISO date & 13:00:00.",
		),
		NLScenario(
			id="S07_conflict_and_recovery",
			category="conflict_recovery",
			nl_prompt="Book RO-2026-00011 into Bay 1 - Main Lift tomorrow at 9am with Marcus.",
			expected_commands=["book"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			notes="Write-gate: --dry-run book -> confirm -> 1 live book hitting occupied slot; report conflict + alternatives + deep link without follow-up slots/batch.",
		),
		NLScenario(
			id="S08_composite_intake_slots",
			category="composite_intake_slots",
			nl_prompt=(
				"Create a draft quote for _EVAL_AgentOS_Customer's Model 3 (5YJ3E1EA1PF200101, odometer 48,200 km) "
				"for Front Brake Inspection and find tomorrow morning slots with Marcus in Main Lift."
			),
			expected_commands=["intake-slots"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			notes="Write-gate: --dry-run intake-slots -> confirm -> 1 live intake-slots.",
		),
		NLScenario(
			id="S09_batch_three_reads",
			category="batching",
			nl_prompt="Check tomorrow's shop status, tech overview, and unscheduled queue for RO-2026-00011.",
			expected_commands=["batch"],
			optimal_cli_calls=1,
			allowed_dry_runs=0,
			notes="Should pick batch in 1 CLI call combining status, tech-overview, and unscheduled.",
		),
		NLScenario(
			id="S10_quote_amend_approval",
			category="quote_amend_approval",
			nl_prompt=(
				"Create a quote for _EVAL_AgentOS_Customer on 5YJ3E1EA1PF200101 (odometer 48,200 km) for Tire Rotation (0.5h), "
				"add Wiper Blade Replacement (0.3h), and get an approval link."
			),
			expected_commands=["quote", "amend-quote", "send-approval"],
			optimal_cli_calls=2,
			allowed_dry_runs=2,
			notes="Write-gate -> quote -> batch(amend-quote + send-approval --medium link) with approval URL + deep links.",
		),
		NLScenario(
			id="S11_ambiguous_bay_recovery",
			category="ambiguous_resource_edge",
			nl_prompt=(
				"Schedule a walk-in diagnostic for _EVAL_WalkIn_Test (2022 Tesla Model Y) tomorrow at 10am "
				"in 'Bay' for 60 min—if 'Bay' is ambiguous, pick an open lift bay."
			),
			expected_commands=["book"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			is_conflict_scenario=True,
			allows_server_feedback_retry=True,
			notes="Write-gate: --dry-run book -> confirm -> live book --bay 'Bay' (417) -> retry book with [Available] lift bay.",
		),
		NLScenario(
			id="S12_multi_service_quote_estimate",
			category="multi_service_quote_estimate",
			nl_prompt=(
				"Quote _EVAL_AgentOS_Customer's 5YJ3E1EA1PF200101 (odometer 48,200 km) for Brake Fluid Flush (1.0h) "
				"and Cabin Air Filter Replacement (0.5h), and give me the P80 estimated shop time and customer total with tax."
			),
			expected_commands=["quote", "estimate"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			notes="Write-gate: --dry-run quote -> confirm -> live quote + estimate.",
		),
		NLScenario(
			id="S13_missing_field_write_gate",
			category="missing_field_write_gate",
			nl_prompt="Create a draft quote for _EVAL_AgentOS_Customer on 5YJ3E1EA1PF200101 for Cabin Air Filter Replacement (0.5h).",
			expected_commands=["quote"],
			optimal_cli_calls=1,
			allowed_dry_runs=2,
			notes="Turn 1 --dry-run quote flags odometer_in [MISSING — REQUIRED]; Turn 2 user gives 54,100 km -> READY summary; Turn 3 confirm -> 1 live quote.",
		),
		NLScenario(
			id="S14_standalone_procure_advance_pay",
			category="standalone_procure_advance_pay",
			nl_prompt=(
				"Order 2 units of Brembo Front Brake Rotor (item _EVAL_Part_Rotor) from _EVAL_Supplier "
				"for shop stock (no RO) at $120 each, paid in full today via Bank Draft (ref BD-9001)."
			),
			expected_commands=["procure"],
			optimal_cli_calls=1,
			allowed_dry_runs=1,
			notes="Write-gate: --dry-run procure -> confirm -> 1 live procure (no --repair-order, --payment-method 'Bank Draft', --paid-amount, --reference-no BD-9001).",
		),
		NLScenario(
			id="S15_purchase_invoice_with_stock",
			category="purchase_invoice_with_stock",
			nl_prompt=(
				"The parts for that _EVAL_Supplier Purchase Order just arrived with supplier invoice INV-9001 "
				"inside the box—post the Purchase Invoice and update stock in one step."
			),
			expected_commands=["list-docs", "purchase-invoice"],
			optimal_cli_calls=2,
			allowed_dry_runs=1,
			notes="Lookup open _EVAL_Supplier PO -> --dry-run purchase-invoice -> confirm -> 1 live purchase-invoice --update-stock --bill-no INV-9001.",
		),
	]


def _delete_or_cancel_doc(client: IsmAgentClient, doctype: str, name: str, cancel_status: str = "Cancelled") -> str:
	"""Delete a document via REST DELETE, falling back to setting status=Cancelled."""
	encoded_dt = urllib.parse.quote(doctype, safe="")
	encoded_name = urllib.parse.quote(name, safe="")
	url = f"{client._tx.config.base_url}/api/resource/{encoded_dt}/{encoded_name}"
	try:
		client._tx.request_json(url, "DELETE")
		return "deleted"
	except IsmApiError:
		try:
			client.update_document(doctype, name, {"status": cancel_status})
			return "cancelled"
		except IsmApiError as exc:
			return f"failed:{exc}"


def _cancel_and_delete_doc(client: IsmAgentClient, doctype: str, name: str, docstatus: int = 0) -> str:
	"""Cancel a submitted submittable document (if docstatus==1) and delete it via REST."""
	if int(docstatus or 0) == 1:
		try:
			client.call_method("frappe.client.cancel", {"doctype": doctype, "name": name})
		except IsmApiError as exc:
			return f"cancel_failed:{exc}"
	encoded_dt = urllib.parse.quote(doctype, safe="")
	encoded_name = urllib.parse.quote(name, safe="")
	url = f"{client._tx.config.base_url}/api/resource/{encoded_dt}/{encoded_name}"
	try:
		client._tx.request_json(url, "DELETE")
		return "deleted"
	except IsmApiError as exc:
		return f"cancelled_only:{exc}" if int(docstatus or 0) == 1 else f"failed:{exc}"


def _resolve_tomorrow_date(client: IsmAgentClient) -> str:
	"""Resolve the Frappe server's 'tomorrow' ISO date from server today()."""
	try:
		res = client.get_shop_floor_status()
		d = res.get("data", {}).get("date")
		if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
			return (date.fromisoformat(d) + timedelta(days=1)).isoformat()
	except Exception:
		pass
	return (date.today() + timedelta(days=1)).isoformat()


def _find_employee_by_name(client: IsmAgentClient, fragment: str) -> Optional[str]:
	"""Look up an Active Employee ID matching employee_name fragment."""
	rows = client.list_documents(
		"Employee",
		filters=[["status", "=", "Active"], ["employee_name", "like", f"%{fragment}%"]],
		fields=["name", "employee_name", "designation"],
		limit=10,
	)
	if not rows:
		return None
	techs = [r for r in rows if r.get("designation") == "Technician"]
	chosen = techs[0] if techs else rows[0]
	return chosen.get("name")


def _ensure_eval_technicians(client: IsmAgentClient) -> list[str]:
	"""Idempotently ensure Marcus Vance, Elena Rostova, and Devon Brooks exist as Active Technicians."""
	sample = client.list_documents("Employee", fields=["company"], limit=1)
	company = (sample[0].get("company") if sample else None) or client._tx.config.company or "Lightspeed EV"
	specs = [
		("Marcus", "Vance", "Male", "1988-05-12", "2021-03-01"),
		("Elena", "Rostova", "Female", "1991-08-20", "2022-01-15"),
		("Devon", "Brooks", "Male", "1993-02-10", "2022-06-01"),
	]
	created: list[str] = []
	for first, last, gender, dob, doj in specs:
		if _find_employee_by_name(client, f"{first} {last}"):
			continue
		doc = client.create_document(
			"Employee",
			{
				"first_name": first,
				"last_name": last,
				"employee_name": f"{first} {last}",
				"gender": gender,
				"date_of_birth": dob,
				"date_of_joining": doj,
				"status": "Active",
				"company": company,
				"designation": "Technician",
			},
		)
		if doc.get("name"):
			created.append(doc["name"])
	return created


def _ensure_service_bays(client: IsmAgentClient) -> list[str]:
	"""Idempotently ensure core Service Bays exist."""
	existing = {b["name"] for b in client.list_documents("Service Bay", fields=["name"], limit=50)}
	bays = [
		{
			"bay_name": "Bay 1 - Main Lift",
			"is_active": 1,
			"description": "Primary 2-post lift bay",
			"equipment": [{"equipment_tag": "Lift"}],
		},
		{
			"bay_name": "Bay 2 - Alignment & Suspension",
			"is_active": 1,
			"description": "Four-wheel alignment rack and lift",
			"equipment": [{"equipment_tag": "Lift"}, {"equipment_tag": "Alignment Rack"}],
		},
		{
			"bay_name": "Bay 3 - HV & Diagnostics",
			"is_active": 1,
			"description": "High-voltage battery and EV diagnostics bay",
			"equipment": [{"equipment_tag": "EV High Voltage"}, {"equipment_tag": "HV Battery Station"}],
		},
	]
	created: list[str] = []
	for bay in bays:
		if bay["bay_name"] not in existing:
			client.create_document("Service Bay", bay)
			created.append(bay["bay_name"])
	return created


def _cleanup_eval_artifacts(client: IsmAgentClient, target_date: str) -> dict[str, Any]:
	"""Remove or cancel mutated eval Schedule Entries, Repair Orders, and S14-S15 procurement docs."""
	se_actions: list[dict[str, str]] = []
	ro_actions: list[dict[str, str]] = []
	pe_actions: list[dict[str, str]] = []
	pi_actions: list[dict[str, str]] = []
	pr_actions: list[dict[str, str]] = []
	po_actions: list[dict[str, str]] = []

	eval_ros = client.list_documents(
		"Repair Order",
		filters=[["customer", "in", list(_EVAL_CUSTOMERS)]],
		fields=["name", "customer", "status", "docstatus"],
		limit=100,
	)
	eval_ro_names = {
		r["name"] for r in eval_ros if r.get("name") and r["name"] not in _PROTECTED_ROS
	}

	all_entries = client.list_documents(
		"Schedule Entry",
		filters=[["status", "!=", "Cancelled"]],
		fields=["name", "repair_order", "scheduled_date", "scheduled_time", "service_bay", "provisional_customer_name", "notes"],
		limit=200,
	)
	for entry in all_entries:
		ename = entry.get("name")
		if not ename:
			continue
		ro = entry.get("repair_order") or ""
		prov = entry.get("provisional_customer_name") or ""
		notes = (entry.get("notes") or "").lower()
		is_eval_entry = (
			ro == "RO-2026-00011"
			or ro in eval_ro_names
			or prov.startswith("_EVAL_")
			or prov.startswith("_TEST_")
			or "eval" in notes
		)
		if is_eval_entry:
			outcome = _delete_or_cancel_doc(client, "Schedule Entry", ename)
			se_actions.append({"name": ename, "outcome": outcome})

	# Ensure RO-2026-00011 services are unscheduled
	try:
		ro11 = client.get_document("Repair Order", "RO-2026-00011")
		services = ro11.get("services") or []
		dirty = False
		updated_services = []
		for svc in services:
			row = dict(svc)
			if row.get("schedule_entry"):
				row["schedule_entry"] = None
				dirty = True
			updated_services.append(row)
		if dirty:
			client.update_document("Repair Order", "RO-2026-00011", {"services": updated_services})
	except IsmApiError:
		pass

	for ro_name in sorted(eval_ro_names):
		outcome = _delete_or_cancel_doc(client, "Repair Order", ro_name)
		ro_actions.append({"name": ro_name, "outcome": outcome})

	# Child-first cleanup for _EVAL_ procurement docs: Purchase Invoice -> Payment Entry -> Purchase Receipt -> Purchase Order
	eval_suppliers = list(_EVAL_SUPPLIERS)
	eval_pis = client.list_documents(
		"Purchase Invoice",
		filters=[["supplier", "in", eval_suppliers]],
		fields=["name", "docstatus"],
		limit=100,
	)
	for doc in eval_pis:
		if doc.get("name"):
			pi_actions.append({"name": doc["name"], "outcome": _cancel_and_delete_doc(client, "Purchase Invoice", doc["name"], doc.get("docstatus", 0))})

	eval_pes = client.list_documents(
		"Payment Entry",
		filters=[["party_type", "=", "Supplier"], ["party", "in", eval_suppliers]],
		fields=["name", "docstatus"],
		limit=100,
	)
	for doc in eval_pes:
		if doc.get("name"):
			pe_actions.append({"name": doc["name"], "outcome": _cancel_and_delete_doc(client, "Payment Entry", doc["name"], doc.get("docstatus", 0))})

	eval_prs = client.list_documents(
		"Purchase Receipt",
		filters=[["supplier", "in", eval_suppliers]],
		fields=["name", "docstatus"],
		limit=100,
	)
	for doc in eval_prs:
		if doc.get("name"):
			pr_actions.append({"name": doc["name"], "outcome": _cancel_and_delete_doc(client, "Purchase Receipt", doc["name"], doc.get("docstatus", 0))})

	eval_pos = client.list_documents(
		"Purchase Order",
		filters=[["supplier", "in", eval_suppliers]],
		fields=["name", "docstatus"],
		limit=100,
	)
	for doc in eval_pos:
		if doc.get("name"):
			po_actions.append({"name": doc["name"], "outcome": _cancel_and_delete_doc(client, "Purchase Order", doc["name"], doc.get("docstatus", 0))})

	return {
		"schedule_entries": se_actions,
		"repair_orders": ro_actions,
		"payment_entries": pe_actions,
		"purchase_invoices": pi_actions,
		"purchase_receipts": pr_actions,
		"purchase_orders": po_actions,
		"target_date": target_date,
	}


def _ensure_conflict_and_baseline_entries(client: IsmAgentClient, target_date: str) -> list[dict[str, Any]]:
	"""Ensure baseline Schedule Entries exist (including 09:00 Bay 1 - Main Lift + Marcus conflict)."""
	marcus_id = _find_employee_by_name(client, "Marcus") or "HR-EMP-00004"
	edwin_or_other = _find_employee_by_name(client, "David") or "HR-EMP-00006"

	existing = client.list_documents(
		"Schedule Entry",
		filters=[["scheduled_date", "=", target_date], ["status", "!=", "Cancelled"]],
		fields=["name", "repair_order", "scheduled_time", "service_bay", "assigned_technician"],
		limit=100,
	)
	actions: list[dict[str, Any]] = []

	desired = [
		("RO-2026-00009", "Bay 1 - Main Lift", marcus_id, "09:00", 90, "Repair", "Baseline Sarah Jenkins brake job"),
		("RO-2026-00010", "Bay 3 - HV & Diagnostics", edwin_or_other, "10:00", 60, "Diagnostic", "Baseline David Kim HV service"),
	]
	for ro_name, bay, tech, start, dur, etype, notes in desired:
		has_match = any(
			e.get("repair_order") == ro_name
			and str(e.get("scheduled_time") or "").lstrip("0").startswith(start.lstrip("0"))
			for e in existing
		)
		if not has_match:
			res = client.book_appointment(
				repair_order=ro_name,
				scheduled_date=target_date,
				start_time=start,
				estimated_duration=dur,
				service_bay=bay,
				technician=tech,
				entry_type=etype,
				notes=notes,
			)
			actions.append({"repair_order": ro_name, "booked": res.get("success"), "data": res.get("data", {}).get("name")})
		else:
			actions.append({"repair_order": ro_name, "status": "already_present"})
	return actions


def _ensure_elena_on_leave(client: IsmAgentClient, target_date: str) -> dict[str, Any]:
	"""Verify Elena (HR-EMP-00005) is marked on leave on target_date."""
	elena_id = _find_employee_by_name(client, "Elena") or "HR-EMP-00005"
	overview = client.get_daily_tech_overview(date=target_date)
	techs = overview.get("data", {}).get("technicians", [])
	elena_row = next((t for t in techs if t.get("employee") == elena_id), None)
	if elena_row and elena_row.get("on_leave") is True:
		return {"employee": elena_id, "on_leave": True, "status": "already_on_leave"}

	existing_leave = client.list_documents(
		"Leave Application",
		filters=[
			["employee", "=", elena_id],
			["from_date", "<=", target_date],
			["to_date", ">=", target_date],
			["docstatus", "=", 1],
		],
		fields=["name", "status"],
		limit=5,
	)
	if existing_leave:
		return {"employee": elena_id, "leave": existing_leave[0], "status": "leave_app_exists"}

	emp = client.get_document("Employee", elena_id)
	year_str = target_date[:4]
	hl_name = f"_EVAL_Holiday_List_{year_str}"
	if not client.list_documents("Holiday List", filters=[["name", "=", hl_name]], fields=["name"], limit=1):
		client.create_document(
			"Holiday List",
			{
				"holiday_list_name": hl_name,
				"from_date": f"{year_str}-01-01",
				"to_date": f"{year_str}-12-31",
				"holidays": [
					{"holiday_date": f"{year_str}-01-01", "description": "New Year's Day"},
				],
			},
		)
	if not client.list_documents(
		"Holiday List Assignment",
		filters=[["assigned_to", "=", elena_id], ["docstatus", "=", 1]],
		fields=["name"],
		limit=1,
	):
		client.create_document(
			"Holiday List Assignment",
			{
				"applicable_for": "Employee",
				"assigned_to": elena_id,
				"holiday_list": hl_name,
				"from_date": f"{year_str}-01-01",
				"docstatus": 1,
			},
		)
	existing_alloc = client.list_documents(
		"Leave Allocation",
		filters=[
			["employee", "=", elena_id],
			["leave_type", "=", "Casual Leave"],
			["from_date", "<=", target_date],
			["to_date", ">=", target_date],
			["docstatus", "=", 1],
		],
		fields=["name"],
		limit=1,
	)
	if not existing_alloc:
		client.create_document(
			"Leave Allocation",
			{
				"employee": elena_id,
				"leave_type": "Casual Leave",
				"from_date": f"{year_str}-01-01",
				"to_date": f"{year_str}-12-31",
				"new_leaves_allocated": 10,
				"company": emp.get("company"),
				"docstatus": 1,
			},
		)
	leave_doc = client.create_document(
		"Leave Application",
		{
			"employee": elena_id,
			"leave_type": "Casual Leave",
			"from_date": target_date,
			"to_date": target_date,
			"company": emp.get("company"),
			"status": "Approved",
			"docstatus": 1,
		},
	)
	return {"employee": elena_id, "created_leave": leave_doc.get("name"), "status": "created"}


def _ensure_eval_parts_and_supplier(client: IsmAgentClient) -> dict[str, Any]:
	"""Idempotently ensure _EVAL_Supplier, _EVAL_Part_Rotor, and Bank Draft Mode of Payment exist."""
	sup_created = False
	item_created = False
	if not client.list_documents("Supplier", filters=[["name", "=", "_EVAL_Supplier"]], fields=["name"], limit=1):
		client.create_document(
			"Supplier",
			{
				"supplier_name": "_EVAL_Supplier",
				"supplier_group": "Local",
				"supplier_type": "Company",
			},
		)
		sup_created = True

	if not client.list_documents("Item", filters=[["name", "=", _EVAL_PART_ROTOR]], fields=["name"], limit=1):
		client.create_document(
			"Item",
			{
				"item_code": _EVAL_PART_ROTOR,
				"item_name": "Brembo Front Brake Rotor",
				"item_group": "Parts",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"valuation_rate": 120.0,
				"standard_rate": 180.0,
			},
		)
		item_created = True

	bank_draft = client.list_documents(
		"Mode of Payment",
		filters=[["name", "=", "Bank Draft"], ["enabled", "=", 1]],
		fields=["name"],
		limit=1,
	)
	return {
		"supplier": "_EVAL_Supplier",
		"supplier_created": sup_created,
		"item_code": _EVAL_PART_ROTOR,
		"item_created": item_created,
		"bank_draft_enabled": bool(bank_draft),
	}


def _ensure_eval_customer_and_vehicle(client: IsmAgentClient) -> dict[str, Any]:
	"""Idempotently ensure _EVAL_AgentOS_Customer and Repair Vehicle 5YJ3E1EA1PF200101 exist."""
	cust_created = False
	veh_created = False
	if not client.list_documents("Customer", filters=[["name", "=", "_EVAL_AgentOS_Customer"]], fields=["name"], limit=1):
		client.create_document(
			"Customer",
			{
				"customer_name": "_EVAL_AgentOS_Customer",
				"customer_type": "Individual",
				"customer_group": "Individual",
				"territory": "All Territories",
			},
		)
		cust_created = True

	if not client.list_documents("Repair Vehicle", filters=[["vin", "=", "5YJ3E1EA1PF200101"]], fields=["name"], limit=1):
		client.create_document(
			"Repair Vehicle",
			{
				"vin": "5YJ3E1EA1PF200101",
				"customer": "_EVAL_AgentOS_Customer",
				"make": "Tesla",
				"model": "Model 3",
				"year": 2023,
			},
		)
		veh_created = True

	return {
		"customer": "_EVAL_AgentOS_Customer",
		"customer_created": cust_created,
		"vin": "5YJ3E1EA1PF200101",
		"vehicle_created": veh_created,
	}


def _ensure_s15_open_po(client: IsmAgentClient) -> dict[str, Any]:
	"""Seed a standalone submitted PO with Bank Draft advance payment for standalone S15 runs."""
	res = client.submit_procurement_orders(
		parts_data=[{"item_code": _EVAL_PART_ROTOR, "qty": 2, "rate": 120.0, "supplier": "_EVAL_Supplier"}],
		payment_method="Bank Draft",
		reference_no="BD-9001",
	)
	data = res.get("data", {})
	return {
		"purchase_orders": data.get("purchase_orders") or [],
		"payment_entries": data.get("payment_entries") or [],
	}


def run_seed(
	client: IsmAgentClient,
	target_date: Optional[str] = None,
	seed_s15_po: bool = False,
) -> dict[str, Any]:
	"""Clean up prior eval artifacts and seed baseline fixtures idempotently."""
	resolved_date = target_date or _resolve_tomorrow_date(client)
	bays_created = _ensure_service_bays(client)
	techs_created = _ensure_eval_technicians(client)
	cleanup = _cleanup_eval_artifacts(client, resolved_date)
	baseline = _ensure_conflict_and_baseline_entries(client, resolved_date)
	elena_leave = _ensure_elena_on_leave(client, resolved_date)
	customer_vehicle = _ensure_eval_customer_and_vehicle(client)
	procurement_fixtures = _ensure_eval_parts_and_supplier(client)
	s15_po = _ensure_s15_open_po(client) if seed_s15_po else None
	unsched = client.get_unscheduled_services("RO-2026-00011")
	unsched_count = unsched.get("data", {}).get("unscheduled_count", 0)
	return {
		"success": unsched_count >= 1 and procurement_fixtures.get("bank_draft_enabled", False),
		"target_date": resolved_date,
		"bays_created": bays_created,
		"techs_created": techs_created,
		"cleanup": cleanup,
		"baseline_entries": baseline,
		"elena_leave": elena_leave,
		"customer_vehicle": customer_vehicle,
		"procurement_fixtures": procurement_fixtures,
		"s15_open_po": s15_po,
		"ro11_unscheduled_count": unsched_count,
	}


def _extract_claimed_ids(entry: dict[str, Any]) -> dict[str, list[str]]:
	"""Extract RO, SE, PO, PI, PE, and URL IDs mentioned in final_answer or command summaries."""
	chunks = [str(entry.get("final_answer") or "")]
	for cmd in entry.get("commands_run") or []:
		chunks.append(str(cmd.get("summary") or ""))
		chunks.append(str(cmd.get("cmd") or ""))
	blob = "\n".join(chunks)
	ros = sorted(set(_RO_PATTERN.findall(blob)))
	ses = sorted(set(_SE_PATTERN.findall(blob)))
	pos = sorted(set(_PO_PATTERN.findall(blob)))
	pis = sorted(set(_PI_PATTERN.findall(blob)))
	pes = sorted(set(_PE_PATTERN.findall(blob)))
	urls = sorted(set(_URL_PATTERN.findall(blob)))
	return {
		"repair_orders": ros,
		"schedule_entries": ses,
		"purchase_orders": pos,
		"purchase_invoices": pis,
		"payment_entries": pes,
		"urls": urls,
	}


def verify_scenario_db(
	client: IsmAgentClient,
	scenario: NLScenario,
	entry: dict[str, Any],
	target_date: str,
) -> dict[str, Any]:
	"""Verify one scenario's claimed outcomes and DB state against Frappe."""
	checks: list[tuple[str, bool, str]] = []
	claimed = _extract_claimed_ids(entry)
	answer = str(entry.get("final_answer") or "")
	answer_lower = answer.lower()
	cmds_text = "\n".join(str(c.get("cmd") or "") for c in (entry.get("commands_run") or []))

	if scenario.id == "S01_shop_status_relative":
		live = client.get_shop_floor_status(date=target_date)
		summary = live.get("data", {}).get("summary", {})
		bays = summary.get("active_bays")
		appts = summary.get("scheduled_appointments")
		checks.append(("date_in_answer_or_cmd", target_date in answer or target_date in cmds_text, f"expected {target_date}"))
		checks.append(("mentions_bays_count", str(bays) in answer, f"active_bays={bays}"))
		checks.append(("mentions_appts_count", str(appts) in answer, f"scheduled_appointments={appts}"))

	elif scenario.id == "S02_tech_util_leave":
		elena_id = _find_employee_by_name(client, "Elena") or "HR-EMP-00005"
		overview = client.get_daily_tech_overview(date=target_date)
		techs = overview.get("data", {}).get("technicians", [])
		elena = next((t for t in techs if t.get("employee") == elena_id), None)
		elena_on_leave = bool(elena and elena.get("on_leave") is True)
		checks.append(("db_elena_on_leave", elena_on_leave, f"elena={elena}"))
		checks.append(("mentions_elena_leave", "elena" in answer_lower and "leave" in answer_lower, "expected Elena on leave"))
		checks.append(("mentions_other_tech", any(n in answer_lower for n in ("marcus", "devon")), "expected tech utilization details"))

	elif scenario.id == "S03_slots_alignment_rack":
		slots_res = client.find_available_slots(
			start_date=target_date,
			duration_minutes=60,
			days_ahead=1,
			equipment_tags=["Alignment Rack"],
		)
		slots = slots_res.get("data", {}).get("slots", [])
		checks.append(("db_slots_exist", len(slots) > 0, f"slot_count={len(slots)}"))
		checks.append(("mentions_alignment_bay", "alignment" in answer_lower or "bay 2" in answer_lower, "expected Alignment Rack bay"))
		checks.append(("mentions_time_slot", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)), "expected HH:MM slot"))

	elif scenario.id == "S04_vin_decode_history":
		vin_data = client.decode_vehicle_vin("5YJ3E1EA1PF200101").get("data", {})
		try:
			intel = client.get_vehicle_intelligence("5YJ3E1EA1PF200101").get("data", {})
		except IsmApiError:
			intel = {}
		checks.append(("mentions_model_3", "model 3" in answer_lower, f"db_model={vin_data.get('model')}"))
		checks.append(("mentions_year_2023", "2023" in answer, f"db_year={vin_data.get('model_year')}"))
		has_history = bool(intel.get("service_history") or intel.get("repair_orders") or "history" in answer_lower or "ro-" in answer_lower)
		checks.append(("mentions_history_or_profile", has_history, "expected vehicle intelligence/history"))

	elif scenario.id == "S05_unscheduled_queue":
		unsched = client.get_unscheduled_services("RO-2026-00011").get("data", {})
		svcs = unsched.get("services") or []
		first_title = str((svcs[0].get("service_title") or "") if svcs else "").lower()
		core_title = first_title.split("(")[0].strip()
		checks.append(("db_has_unscheduled", len(svcs) >= 1, f"count={len(svcs)}"))
		checks.append(("mentions_service_title", bool(core_title) and (first_title in answer_lower or core_title in answer_lower), f"expected '{first_title}'"))

	elif scenario.id == "S06_direct_book_human_names":
		devon_id = _find_employee_by_name(client, "Devon") or "HR-EMP-00006"
		entries = client.list_documents(
			"Schedule Entry",
			filters=[
				["repair_order", "=", "RO-2026-00011"],
				["scheduled_date", "=", target_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time", "service_bay", "assigned_technician", "estimated_duration", "status"],
			limit=10,
		)
		match = next((e for e in entries if str(e.get("scheduled_time") or "").startswith("13:00")), None)
		checks.append(("db_entry_exists_1300", match is not None, f"entries={entries}"))
		if match:
			checks.append(("db_bay_main_lift", match.get("service_bay") == "Bay 1 - Main Lift", f"bay={match.get('service_bay')}"))
			checks.append(("db_tech_devon", match.get("assigned_technician") == devon_id, f"tech={match.get('assigned_technician')}"))
			checks.append(("db_duration_60", int(match.get("estimated_duration") or 0) == 60, f"dur={match.get('estimated_duration')}"))
			checks.append(("claimed_se_matches", match["name"] in claimed["schedule_entries"], f"claimed={claimed['schedule_entries']}"))

	elif scenario.id == "S07_conflict_and_recovery":
		entries_9am = client.list_documents(
			"Schedule Entry",
			filters=[
				["repair_order", "=", "RO-2026-00011"],
				["scheduled_date", "=", target_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time"],
			limit=10,
		)
		created_9am = [
			e for e in entries_9am
			if str(e.get("scheduled_time") or "").startswith(("09:00", "9:00"))
		]
		checks.append(("no_9am_entry_created", len(created_9am) == 0, f"unexpected={created_9am}"))
		checks.append(("surfaces_conflict", any(w in answer_lower for w in ("conflict", "occupied", "booked", "unavailable")), "expected conflict notice"))
		checks.append(("surfaces_alternatives", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)) or "alternative" in answer_lower, "expected alternative slots"))
		checks.append(("surfaces_deep_link", len(claimed["urls"]) > 0 or "/app/" in answer, f"urls={claimed['urls']}"))

	elif scenario.id == "S08_composite_intake_slots":
		new_ros = [r for r in claimed["repair_orders"] if r not in _PROTECTED_ROS]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == "_EVAL_AgentOS_Customer", f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0 and ro.get("status") == "Draft Estimate", f"status={ro.get('status')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_ro_has_brake_inspection", any("front brake inspection" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))
		checks.append(("mentions_slots", bool(re.search(r"\b\d{1,2}:\d{2}\b", answer)), "expected candidate slot times"))

	elif scenario.id == "S09_batch_three_reads":
		live = client.get_shop_floor_status(date=target_date).get("data", {}).get("summary", {})
		unsched = client.get_unscheduled_services("RO-2026-00011").get("data", {}).get("services") or []
		checks.append(("mentions_bays", str(live.get("active_bays")) in answer, f"active_bays={live.get('active_bays')}"))
		checks.append(("mentions_elena_leave", "elena" in answer_lower, "expected Elena leave info"))
		first_svc = str((unsched[0].get("service_title") or "") if unsched else "").lower()
		core_svc = first_svc.split("(")[0].strip()
		checks.append(("mentions_unscheduled_ro11", bool(core_svc) and (first_svc in answer_lower or core_svc in answer_lower), f"expected '{first_svc}'"))

	elif scenario.id == "S10_quote_amend_approval":
		new_ros = [r for r in claimed["repair_orders"] if r not in _PROTECTED_ROS]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == "_EVAL_AgentOS_Customer", f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0, f"docstatus={ro.get('docstatus')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_has_2_services", len(svcs) == 2, f"services={svcs}"))
				checks.append(("db_has_tire_rotation", any("tire rotation" in s for s in svcs), f"services={svcs}"))
				checks.append(("db_has_wiper", any("wiper" in s for s in svcs), f"services={svcs}"))
				checks.append(("db_has_approval_token", bool(ro.get("approval_token")), "approval_token missing"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))
		checks.append(("answer_has_approval_url", "token=" in answer, "expected approval URL with token="))

	elif scenario.id == "S11_ambiguous_bay_recovery":
		entries = client.list_documents(
			"Schedule Entry",
			filters=[
				["provisional_customer_name", "=", "_EVAL_WalkIn_Test"],
				["scheduled_date", "=", target_date],
				["status", "!=", "Cancelled"],
			],
			fields=["name", "scheduled_time", "service_bay", "estimated_duration"],
			limit=10,
		)
		match = next((e for e in entries if str(e.get("scheduled_time") or "").startswith("10:00")), None)
		checks.append(("db_walkin_entry_exists", match is not None, f"entries={entries}"))
		if match:
			checks.append(("db_duration_60", int(match.get("estimated_duration") or 0) == 60, f"dur={match.get('estimated_duration')}"))
			checks.append(("db_valid_bay", bool(match.get("service_bay")), f"bay={match.get('service_bay')}"))
			checks.append(("claimed_se_matches", match["name"] in claimed["schedule_entries"], f"claimed={claimed['schedule_entries']}"))

	elif scenario.id == "S12_multi_service_quote_estimate":
		new_ros = [r for r in claimed["repair_orders"] if r not in _PROTECTED_ROS]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				gt = float(ro.get("grand_total") or 0.0)
				est = client.estimate_duration(repair_order=ro_id).get("data", {})
				p80 = est.get("p80_minutes") or est.get("total_duration_minutes") or est.get("estimated_minutes")
				checks.append(("db_ro_customer", ro.get("customer") == "_EVAL_AgentOS_Customer", f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0, f"docstatus={ro.get('docstatus')}"))
				checks.append(("db_ro_odometer_48200", int(ro.get("odometer_in") or 0) == 48200, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_has_2_services", len(svcs) == 2, f"services={svcs}"))
				checks.append(("db_grand_total_positive", gt > 0, f"grand_total={gt}"))
				gt_str = f"{gt:.2f}"
				checks.append(("answer_has_server_total", gt_str in answer or f"{gt:,.2f}" in answer, f"expected {gt_str}"))
				checks.append(("answer_has_p80", bool(p80) and str(int(round(float(p80)))) in answer, f"expected p80={p80}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif scenario.id == "S13_missing_field_write_gate":
		new_ros = [r for r in claimed["repair_orders"] if r not in _PROTECTED_ROS]
		ro_id = new_ros[-1] if new_ros else None
		checks.append(("claimed_new_ro", ro_id is not None, f"claimed_ros={claimed['repair_orders']}"))
		if ro_id:
			try:
				ro = client.get_document("Repair Order", ro_id)
				svcs = [str(s.get("service_title") or "").lower() for s in (ro.get("services") or [])]
				checks.append(("db_ro_customer", ro.get("customer") == "_EVAL_AgentOS_Customer", f"customer={ro.get('customer')}"))
				checks.append(("db_ro_draft", ro.get("docstatus") == 0 and ro.get("status") == "Draft Estimate", f"status={ro.get('status')}"))
				checks.append(("db_ro_odometer_54100", int(ro.get("odometer_in") or 0) == 54100, f"odometer_in={ro.get('odometer_in')}"))
				checks.append(("db_ro_has_cabin_filter", any("cabin air filter" in s for s in svcs), f"services={svcs}"))
			except IsmApiError as exc:
				checks.append(("db_ro_exists", False, str(exc)))

	elif scenario.id == "S14_standalone_procure_advance_pay":
		po_id = claimed["purchase_orders"][-1] if claimed["purchase_orders"] else None
		if not po_id:
			pos = client.list_documents(
				"Purchase Order",
				filters=[["supplier", "=", "_EVAL_Supplier"], ["docstatus", "=", 1]],
				fields=["name"],
				order_by="creation desc",
				limit=1,
			)
			po_id = pos[0]["name"] if pos else None
		checks.append(("db_po_found", po_id is not None, f"claimed_pos={claimed['purchase_orders']}"))
		if po_id:
			try:
				po = client.get_document("Purchase Order", po_id)
				items = po.get("items") or []
				gt = float(po.get("grand_total") or 0.0)
				adv = float(po.get("advance_paid") or 0.0)
				checks.append(("db_po_submitted", int(po.get("docstatus") or 0) == 1, f"docstatus={po.get('docstatus')}"))
				checks.append(("db_po_standalone_no_ro", not po.get("repair_order"), f"repair_order={po.get('repair_order')}"))
				checks.append(("db_po_supplier", po.get("supplier") == "_EVAL_Supplier", f"supplier={po.get('supplier')}"))
				checks.append((
					"db_po_item_qty_rate",
					any(it.get("item_code") == _EVAL_PART_ROTOR and float(it.get("qty") or 0) == 2.0 and float(it.get("rate") or 0) == 120.0 for it in items),
					f"items={items}",
				))
				checks.append(("db_po_advance_paid_full", gt > 0 and abs(adv - gt) < 0.05, f"grand_total={gt}, advance_paid={adv}"))
				checks.append(("claimed_po_matches", po_id in claimed["purchase_orders"], f"claimed={claimed['purchase_orders']}"))
			except IsmApiError as exc:
				checks.append(("db_po_exists", False, str(exc)))

		pes = client.list_documents(
			"Payment Entry",
			filters=[["party_type", "=", "Supplier"], ["party", "=", "_EVAL_Supplier"], ["reference_no", "=", "BD-9001"], ["docstatus", "=", 1]],
			fields=["name", "mode_of_payment", "paid_amount", "docstatus", "reference_no"],
			order_by="creation desc",
			limit=1,
		)
		pe = pes[0] if pes else None
		checks.append(("db_pe_submitted", bool(pe and int(pe.get("docstatus") or 0) == 1), f"pe={pe}"))
		if pe:
			checks.append(("db_pe_bank_draft", pe.get("mode_of_payment") == "Bank Draft", f"mop={pe.get('mode_of_payment')}"))
			checks.append(("claimed_pe_matches", pe["name"] in claimed["payment_entries"], f"claimed={claimed['payment_entries']}"))

	elif scenario.id == "S15_purchase_invoice_with_stock":
		pi_id = claimed["purchase_invoices"][-1] if claimed["purchase_invoices"] else None
		if not pi_id:
			pis = client.list_documents(
				"Purchase Invoice",
				filters=[["supplier", "=", "_EVAL_Supplier"], ["bill_no", "=", "INV-9001"], ["docstatus", "=", 1]],
				fields=["name"],
				order_by="creation desc",
				limit=1,
			)
			pi_id = pis[0]["name"] if pis else None
		checks.append(("db_pi_found", pi_id is not None, f"claimed_pis={claimed['purchase_invoices']}"))
		if pi_id:
			try:
				pi = client.get_document("Purchase Invoice", pi_id)
				checks.append(("db_pi_submitted", int(pi.get("docstatus") or 0) == 1, f"docstatus={pi.get('docstatus')}"))
				checks.append(("db_pi_update_stock", int(pi.get("update_stock") or 0) == 1, f"update_stock={pi.get('update_stock')}"))
				checks.append(("db_pi_bill_no", pi.get("bill_no") == "INV-9001", f"bill_no={pi.get('bill_no')}"))
				checks.append(("db_pi_advance_attached", float(pi.get("total_advance") or 0.0) > 0, f"total_advance={pi.get('total_advance')}"))
				checks.append(("db_pi_outstanding_zero", abs(float(pi.get("outstanding_amount") or 0.0)) < 0.05, f"outstanding={pi.get('outstanding_amount')}"))
				checks.append(("claimed_pi_matches", pi_id in claimed["purchase_invoices"], f"claimed={claimed['purchase_invoices']}"))
				po_links = [it.get("purchase_order") for it in (pi.get("items") or []) if it.get("purchase_order")]
				if po_links:
					po = client.get_document("Purchase Order", po_links[0])
					checks.append(("db_po_per_received_100", float(po.get("per_received") or 0.0) >= 99.9, f"per_received={po.get('per_received')}"))
				else:
					checks.append(("db_po_linked_on_pi", False, "no purchase_order on PI items"))
			except IsmApiError as exc:
				checks.append(("db_pi_exists", False, str(exc)))

	passed_count = sum(1 for _, ok, _ in checks if ok)
	failures = [f"{label}: {detail}" for label, ok, detail in checks if not ok]
	return {
		"db_verified": passed_count == len(checks) and len(checks) > 0,
		"checks_passed": passed_count,
		"checks_total": len(checks),
		"claimed_ids": claimed,
		"failures": failures,
	}


_CLI_INVOCATION_PATTERN = re.compile(
	r"(?:^|[;&|\s])(?:"
	r"(?:[^\s;&|]*[\\/])?(?:py|python(?:\d+(?:\.\d+)?)?(?:\.exe)?)\s+(?:-m\s+tools\.cli|(?:[^\s;&|]*[\\/])?tools[\\/]cli\.py)"
	r"|ism-agent-os"
	r")(?:\s|$)"
)


def _split_shell_segments(command_line: str) -> list[str]:
	"""Split a shell command string on separators outside single/double quotes."""
	segments: list[str] = []
	buf: list[str] = []
	in_single = False
	in_double = False
	i = 0
	n = len(command_line)
	while i < n:
		ch = command_line[i]
		if ch == "'" and not in_double:
			in_single = not in_single
			buf.append(ch)
			i += 1
			continue
		if ch == '"' and not in_single:
			in_double = not in_double
			buf.append(ch)
			i += 1
			continue
		if ch == "\\" and in_double and i + 1 < n:
			buf.append(command_line[i : i + 2])
			i += 2
			continue
		if not in_single and not in_double:
			two = command_line[i : i + 2]
			if two in ("&&", "||"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 2
				continue
			if ch in (";", "|", "\n", "\r"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 1
				continue
		buf.append(ch)
		i += 1
	tail = "".join(buf).strip()
	if tail:
		segments.append(tail)
	return [s for s in segments if s]


def _tokenize_segment(segment: str) -> list[str]:
	escaped = re.sub(r'\\(?![\\"\'])', r"\\\\", segment)
	try:
		return shlex.split(escaped)
	except ValueError:
		return segment.split()


def _extract_cli_argv_from_tokens(tokens: list[str]) -> Optional[list[str]]:
	"""Return CLI argv tokens if a command segment invokes the ISM Agent OS CLI."""
	if not tokens:
		return None
	first_base = Path(tokens[0]).name.lower()
	if first_base in ("ism-agent-os", "ism-agent-os.exe"):
		return tokens[1:]
	if first_base in ("python", "python.exe", "python3", "python3.exe", "py", "py.exe"):
		for idx in range(1, len(tokens)):
			tok = tokens[idx]
			if tok == "-m" and idx + 1 < len(tokens):
				if tokens[idx + 1] in ("tools.cli", "ism_agent_os.cli"):
					return tokens[idx + 2 :]
				return None
			if tok.startswith("-"):
				continue
			if Path(tok).name.lower() == "cli.py" and (
				"tools" in tok.replace("\\", "/").lower() or tok.lower() == "cli.py"
			):
				return tokens[idx + 1 :]
			return None
	return None


def _extract_subcommand(cmd_str: str) -> Optional[str]:
	"""Extract the ISM CLI subcommand name from a command string."""
	for seg in _split_shell_segments(cmd_str):
		argv = _extract_cli_argv_from_tokens(_tokenize_segment(seg))
		if argv is None:
			continue
		j = 0
		while j < len(argv) and argv[j].startswith("-"):
			if argv[j] == "--env-file":
				j += 2
			else:
				j += 1
		if j < len(argv):
			return argv[j]
	return None


def _is_cli_command(cmd_str: str) -> bool:
	"""Return True if cmd_str invokes the ISM Agent OS CLI."""
	for seg in _split_shell_segments(cmd_str):
		if _extract_cli_argv_from_tokens(_tokenize_segment(seg)) is not None:
			return True
	return bool(_CLI_INVOCATION_PATTERN.search(cmd_str))


def _unwrap_batch_argv(argv: list[str], cli_parser: argparse.ArgumentParser, parsed_args: Optional[argparse.Namespace]) -> list[dict[str, Any]]:
	"""Unwrap batch --cmd / --file items using _batch_item_to_argv into inner command records."""
	raw_items: list[Any] = []
	if parsed_args is not None:
		try:
			raw_items = _load_batch_items(parsed_args)
		except (OSError, ValueError, json.JSONDecodeError):
			raw_items = list(getattr(parsed_args, "cmds", None) or [])
	else:
		i = 0
		while i < len(argv):
			if argv[i] in ("--cmd", "-c") and i + 1 < len(argv):
				raw_items.append(argv[i + 1])
				i += 2
			else:
				i += 1

	inner: list[dict[str, Any]] = []
	for raw_item in raw_items:
		try:
			sub_argv = _batch_item_to_argv(raw_item)
		except ValueError:
			continue
		sub_cmd: Optional[str] = None
		sub_dry = ("--dry-run" in sub_argv) or ("--preview" in sub_argv)
		err_buf = io.StringIO()
		try:
			with contextlib.redirect_stderr(err_buf):
				sub_ns = cli_parser.parse_args(sub_argv)
			sub_cmd = getattr(sub_ns, "command", None)
			sub_dry = bool(getattr(sub_ns, "dry_run", sub_dry))
		except SystemExit:
			for tok in sub_argv:
				if not tok.startswith("-"):
					sub_cmd = tok
					break
		inner.append({"subcommand": sub_cmd, "argv": sub_argv, "dry_run": sub_dry})
	return inner


def _parse_cli_invocations(cmd_str: str, cli_parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
	"""Parse one shell command string into structured CLI invocation descriptors."""
	invocations: list[dict[str, Any]] = []
	for seg in _split_shell_segments(cmd_str):
		tokens = _tokenize_segment(seg)
		argv = _extract_cli_argv_from_tokens(tokens)
		if argv is None:
			continue
		is_help = ("--help" in argv) or ("-h" in argv)
		outer_cmd: Optional[str] = None
		is_dry_run = ("--dry-run" in argv) or ("--preview" in argv)
		parsed_args: Optional[argparse.Namespace] = None

		if not is_help:
			err_buf = io.StringIO()
			try:
				with contextlib.redirect_stderr(err_buf):
					parsed_args = cli_parser.parse_args(argv)
				outer_cmd = getattr(parsed_args, "command", None)
				is_dry_run = bool(getattr(parsed_args, "dry_run", is_dry_run))
			except SystemExit:
				j = 0
				while j < len(argv) and argv[j].startswith("-"):
					j += 2 if argv[j] == "--env-file" else 1
				if j < len(argv):
					outer_cmd = argv[j]
		else:
			j = 0
			while j < len(argv) and argv[j].startswith("-"):
				j += 2 if argv[j] == "--env-file" else 1
			if j < len(argv):
				outer_cmd = argv[j]

		inner_calls: list[dict[str, Any]] = []
		if outer_cmd == "batch" and not is_help:
			inner_calls = _unwrap_batch_argv(argv, cli_parser, parsed_args)
			if inner_calls and all(ic.get("dry_run") for ic in inner_calls):
				is_dry_run = True
		elif outer_cmd and not is_help:
			inner_calls = [{"subcommand": outer_cmd, "argv": argv, "dry_run": is_dry_run}]

		invocations.append({
			"outer_command": outer_cmd,
			"is_help": is_help,
			"is_dry_run": is_dry_run,
			"inner_calls": inner_calls,
			"argv": argv,
		})
	return invocations


def _analyze_commands(scenario: NLScenario, entry: dict[str, Any]) -> dict[str, Any]:
	"""Analyze CLI commands with batch unwrapping and dry-run aware scoring."""
	commands_run = entry.get("commands_run") or []
	cli_parser = _build_cli_parser()

	live_outer_subcommands: list[str] = []
	live_inner_subcommands: list[str] = []
	dry_run_subcommands: list[str] = []
	cli_calls_count = 0
	dry_run_calls = 0
	live_cli_calls = 0
	redundant_calls = 0
	duplicate_dry_runs = 0
	readonly_dry_runs = 0
	help_calls = 0
	unexpected_errors = 0
	self_corrections = 0
	seen_cmds: set[str] = set()

	for item in commands_run:
		cmd_str = str(item.get("cmd") or "").strip()
		if not _is_cli_command(cmd_str):
			continue
		parsed_list = _parse_cli_invocations(cmd_str, cli_parser)
		if not parsed_list:
			continue

		rc = int(item.get("exit_code") or 0)
		summary = str(item.get("summary") or item.get("stdout_summary") or "").lower()
		is_duplicate_cmd = cmd_str in seen_cmds
		if is_duplicate_cmd:
			redundant_calls += 1
		seen_cmds.add(cmd_str)

		for inv in parsed_list:
			cli_calls_count += 1
			outer = inv["outer_command"]
			is_help = inv["is_help"]
			is_dry = inv["is_dry_run"]
			inner_calls = inv["inner_calls"]

			if is_help:
				help_calls += 1
				continue

			if is_dry:
				dry_run_calls += 1
				if outer:
					dry_run_subcommands.append(outer)
				if is_duplicate_cmd:
					duplicate_dry_runs += 1
				elif any(ic.get("subcommand") in READ_ONLY_COMMANDS for ic in inner_calls):
					readonly_dry_runs += 1
					redundant_calls += 1
				continue

			live_cli_calls += 1
			if outer:
				live_outer_subcommands.append(outer)
			for ic in inner_calls:
				sub = ic.get("subcommand")
				if sub:
					live_inner_subcommands.append(sub)
				ic_str = " ".join(ic.get("argv") or [])
				if scenario.id == "S10_quote_amend_approval" and sub == "amend-quote" and "--remove-indices" in ic_str:
					self_corrections += 1
				elif scenario.id in ("S12_multi_service_quote_estimate", "S13_missing_field_write_gate") and sub == "amend-quote":
					self_corrections += 1

			if rc != 0:
				is_expected_feedback = scenario.allows_server_feedback_retry and any(
					tok in summary for tok in ("conflict", "occupied", "ambiguous")
				)
				if not is_expected_feedback:
					unexpected_errors += 1
					self_corrections += 1

	excess_dry_runs = max(0, dry_run_calls - duplicate_dry_runs - readonly_dry_runs - scenario.allowed_dry_runs)
	redundant_calls += excess_dry_runs

	# Routing redundancy rules across all 15 scenarios (using unwrapped inner subcommands)
	if scenario.id in ("S06_direct_book_human_names", "S07_conflict_and_recovery"):
		pre_checks = [
			s for s in live_inner_subcommands
			if s in ("status", "slots", "estimate", "get-doc", "list-docs", "tech-overview", "unscheduled", "search-vehicles")
		]
		redundant_calls += len(pre_checks)
		remaining = len(live_inner_subcommands) - len(pre_checks)
		if remaining > 1:
			redundant_calls += remaining - 1
	elif scenario.id == "S08_composite_intake_slots":
		if "intake-slots" in live_inner_subcommands:
			if len(live_inner_subcommands) > 1:
				redundant_calls += len(live_inner_subcommands) - 1
		elif "quote" in live_inner_subcommands and "slots" in live_inner_subcommands:
			redundant_calls += 1
	elif scenario.id == "S10_quote_amend_approval":
		redundant_calls += sum(
			1 for s in live_inner_subcommands
			if s in ("status", "estimate", "get-doc", "list-docs", "slots", "search-vehicles", "vehicle-info", "decode-vin")
		)
		if (
			"amend-quote" in live_inner_subcommands
			and "send-approval" in live_inner_subcommands
			and "batch" not in live_outer_subcommands
		):
			redundant_calls += 1
	elif scenario.id == "S11_ambiguous_bay_recovery":
		redundant_calls += sum(
			1 for s in live_inner_subcommands
			if s in ("status", "estimate", "get-doc", "list-docs", "tech-overview", "unscheduled")
		)
		first_book_idx = live_inner_subcommands.index("book") if "book" in live_inner_subcommands else len(live_inner_subcommands)
		redundant_calls += sum(1 for s in live_inner_subcommands[:first_book_idx] if s == "slots")
	elif scenario.id == "S12_multi_service_quote_estimate":
		redundant_calls += sum(
			1 for s in live_inner_subcommands
			if s in ("amend-quote", "status", "get-doc", "list-docs", "slots", "tech-overview", "search-vehicles", "vehicle-info")
		)
	elif scenario.id in ("S13_missing_field_write_gate", "S14_standalone_procure_advance_pay"):
		if len(live_inner_subcommands) > 1:
			redundant_calls += len(live_inner_subcommands) - 1
	elif scenario.id == "S15_purchase_invoice_with_stock":
		unwanted = sum(1 for s in live_inner_subcommands if s not in ("list-docs", "purchase-invoice"))
		list_docs_count = sum(1 for s in live_inner_subcommands if s == "list-docs")
		pi_count = sum(1 for s in live_inner_subcommands if s == "purchase-invoice")
		redundant_calls += unwanted + max(0, list_docs_count - 1) + max(0, pi_count - 1)
	elif scenario.id == "S04_vin_decode_history":
		unwanted = sum(
			1 for s in live_inner_subcommands
			if s in ("search-vehicles", "status", "slots", "tech-overview", "list-docs", "get-doc")
		)
		redundant_calls += unwanted
		if "batch" not in live_outer_subcommands and len(live_outer_subcommands) > 1:
			redundant_calls += len(live_outer_subcommands) - 1
		else:
			clean_inner = len(live_inner_subcommands) - unwanted
			if clean_inner > 2:
				redundant_calls += clean_inner - 2
	elif scenario.id == "S09_batch_three_reads":
		unwanted = sum(1 for s in live_inner_subcommands if s not in ("status", "tech-overview", "unscheduled"))
		redundant_calls += unwanted
		if "batch" not in live_outer_subcommands and len(live_outer_subcommands) > 1:
			redundant_calls += len(live_outer_subcommands) - 1
		else:
			clean_inner = len(live_inner_subcommands) - unwanted
			if clean_inner > 3:
				redundant_calls += clean_inner - 3
	elif scenario.id in ("S01_shop_status_relative", "S02_tech_util_leave", "S03_slots_alignment_rack", "S05_unscheduled_queue"):
		if len(live_inner_subcommands) > 1:
			redundant_calls += len(live_inner_subcommands) - 1

	has_redundancy = (help_calls > 0) or (redundant_calls > 0)
	return {
		"subcommands": live_outer_subcommands,
		"inner_subcommands": live_inner_subcommands,
		"dry_run_subcommands": dry_run_subcommands,
		"total_cli_calls": cli_calls_count,
		"dry_run_calls": dry_run_calls,
		"live_cli_calls": live_cli_calls,
		"domain_cli_calls": live_cli_calls,
		"help_calls": help_calls,
		"self_corrections": self_corrections,
		"redundant_calls": redundant_calls,
		"has_redundancy": has_redundancy,
		"unexpected_errors": unexpected_errors,
	}


def score_round(
	client: Optional[IsmAgentClient],
	round_num: int,
	raw_entries: list[dict[str, Any]],
	target_date: str,
	skip_db: bool = False,
) -> dict[str, Any]:
	"""Verify DB state and compute Round <N> KPIs across all scenario entries."""
	scenarios_by_id = {s.id: s for s in get_scenarios()}
	scored_results: list[dict[str, Any]] = []

	for entry in raw_entries:
		sid = entry.get("id")
		scenario = scenarios_by_id.get(sid)
		if not scenario:
			continue

		cmd_metrics = _analyze_commands(scenario, entry)
		if skip_db or client is None:
			existing_db = entry.get("db_verification")
			if isinstance(existing_db, dict) and "db_verified" in existing_db:
				db_res = existing_db
			else:
				db_res = {"db_verified": True, "checks_passed": 0, "checks_total": 0, "claimed_ids": _extract_claimed_ids(entry), "failures": []}
		else:
			db_res = verify_scenario_db(client, scenario, entry, target_date)

		first_try = (
			db_res["db_verified"]
			and cmd_metrics["unexpected_errors"] == 0
			and cmd_metrics["self_corrections"] == 0
			and cmd_metrics["redundant_calls"] == 0
			and cmd_metrics["help_calls"] == 0
		)

		wall_ms = float(entry.get("wall_clock_ms") or 0.0)
		if wall_ms <= 0:
			wall_ms = sum(float(c.get("ms") or 0.0) for c in (entry.get("commands_run") or []))

		conflict_recovered: Optional[bool] = None
		if scenario.is_conflict_scenario:
			conflict_recovered = db_res["db_verified"] and cmd_metrics["unexpected_errors"] == 0

		scored_results.append({
			"id": scenario.id,
			"category": scenario.category,
			"nl_prompt": scenario.nl_prompt,
			"expected_commands": scenario.expected_commands,
			"optimal_cli_calls": scenario.optimal_cli_calls,
			"allowed_dry_runs": scenario.allowed_dry_runs,
			"commands_run": entry.get("commands_run") or [],
			"subcommands_executed": cmd_metrics["subcommands"],
			"inner_subcommands_executed": cmd_metrics["inner_subcommands"],
			"dry_run_subcommands": cmd_metrics["dry_run_subcommands"],
			"total_cli_calls": cmd_metrics["total_cli_calls"],
			"dry_run_calls": cmd_metrics["dry_run_calls"],
			"live_cli_calls": cmd_metrics["live_cli_calls"],
			"domain_cli_calls": cmd_metrics["domain_cli_calls"],
			"help_calls": cmd_metrics["help_calls"],
			"self_corrections": cmd_metrics["self_corrections"],
			"redundant_calls": cmd_metrics["redundant_calls"],
			"has_redundancy": cmd_metrics["has_redundancy"],
			"unexpected_errors": cmd_metrics["unexpected_errors"],
			"first_try_success": first_try,
			"conflict_recovered": conflict_recovered,
			"wall_clock_ms": round(wall_ms, 1),
			"final_answer": entry.get("final_answer") or "",
			"db_verification": db_res,
		})

	n = len(scored_results)
	total_cli_calls = sum(r["total_cli_calls"] for r in scored_results)
	total_redundant_incl_help = sum(r["redundant_calls"] + r["help_calls"] for r in scored_results)
	conflict_rows = [r for r in scored_results if r["conflict_recovered"] is not None]

	kpis = {
		"round": round_num,
		"target_date": target_date,
		"scenarios_total": n,
		"scenarios_db_verified": sum(1 for r in scored_results if r["db_verification"]["db_verified"]),
		"db_verification_rate": round(sum(1 for r in scored_results if r["db_verification"]["db_verified"]) / n, 4) if n else 0.0,
		"first_try_success": round(sum(1 for r in scored_results if r["first_try_success"]) / n, 4) if n else 0.0,
		"avg_cli_calls": round(total_cli_calls / n, 2) if n else 0.0,
		"avg_live_cli_calls": round(sum(r["live_cli_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_domain_cli_calls": round(sum(r["domain_cli_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_dry_run_calls": round(sum(r["dry_run_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_help_calls": round(sum(r["help_calls"] for r in scored_results) / n, 2) if n else 0.0,
		"avg_self_corrections": round(sum(r["self_corrections"] for r in scored_results) / n, 2) if n else 0.0,
		"redundant_call_rate": round(sum(1 for r in scored_results if r["has_redundancy"]) / n, 4) if n else 0.0,
		"call_redundancy_rate": round(total_redundant_incl_help / total_cli_calls, 4) if total_cli_calls else 0.0,
		"conflict_recovery_rate": (
			round(sum(1 for r in conflict_rows if r["conflict_recovered"]) / len(conflict_rows), 4)
			if conflict_rows
			else None
		),
		"total_wall_clock_ms": round(sum(r["wall_clock_ms"] for r in scored_results), 1),
	}
	return {"kpis": kpis, "results": scored_results}


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="ISM Agent OS Blinded NL Eval Loop")
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("scenarios", help="Print all 15 blinded NL eval scenarios as JSON")

	p_seed = sub.add_parser("seed", help="Reset eval artifacts and seed baseline shop fixtures")
	p_seed.add_argument("--target-date", help="Override target tomorrow date (YYYY-MM-DD)")
	p_seed.add_argument("--seed-s15-po", action="store_true", help="Also seed a standalone open PO for S15")

	p_vdb = sub.add_parser("verify-db", help="Verify sub-agent results against live Frappe DB")
	p_vdb.add_argument("--input", required=True, help="Path to raw sub-agent JSON array")
	p_vdb.add_argument("--target-date", help="Target tomorrow date (YYYY-MM-DD)")

	p_score = sub.add_parser("score", help="Verify DB state, compute KPIs, and save round_<N>.json")
	p_score.add_argument("--round", type=int, required=True, help="Round number (e.g. 0)")
	p_score.add_argument("--input", required=True, help="Path to raw sub-agent JSON array")
	p_score.add_argument("--target-date", help="Target tomorrow date (YYYY-MM-DD)")
	p_score.add_argument("--skip-db", action="store_true", help="Skip live DB verification (offline scoring)")
	p_score.add_argument("--output", help="Optional custom output path")

	return parser


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)

	if args.command == "scenarios":
		print(json.dumps([asdict(s) for s in get_scenarios()], indent=2))
		return 0

	client = IsmAgentClient(IsmConfig.from_env())
	try:
		target_date = getattr(args, "target_date", None) or _resolve_tomorrow_date(client)
		if args.command == "seed":
			res = run_seed(client, target_date=target_date, seed_s15_po=bool(getattr(args, "seed_s15_po", False)))
			print(json.dumps(res, indent=2))
			return 0 if res.get("success") else 1

		raw_path = Path(args.input)
		raw_entries = json.loads(raw_path.read_text(encoding="utf-8-sig"))
		if isinstance(raw_entries, dict) and "results" in raw_entries:
			raw_entries = raw_entries["results"]

		if args.command == "verify-db":
			scenarios_by_id = {s.id: s for s in get_scenarios()}
			out = []
			for entry in raw_entries:
				sc = scenarios_by_id.get(entry.get("id"))
				if sc:
					out.append({"id": sc.id, **verify_scenario_db(client, sc, entry, target_date)})
			print(json.dumps(out, indent=2))
			return 0 if all(item["db_verified"] for item in out) else 1

		if args.command == "score":
			report = score_round(
				client=None if args.skip_db else client,
				round_num=args.round,
				raw_entries=raw_entries,
				target_date=target_date,
				skip_db=args.skip_db,
			)
			_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
			out_path = Path(args.output) if args.output else (_RESULTS_DIR / f"round_{args.round}.json")
			out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
			print(json.dumps({"saved_to": str(out_path), "kpis": report["kpis"]}, indent=2))
			return 0
	finally:
		client._tx.close()
	return 1


if __name__ == "__main__":
	sys.exit(main())

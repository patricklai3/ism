# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Hybrid soft-assignment rebalancer, lock triggers, and leave/cancellation handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Optional, Union

import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from ism.ism.doctype.schedule_entry.schedule_entry import sync_repair_order_schedule_entries
from ism.scheduling.assignment import (
	_load_skill_scarcity_weights,
	_select_technician_with_context,
	select_technician_for_slot,
)
from ism.scheduling.capacity import NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.entry_queries import build_holiday_checker, fetch_blocking_entries
from ism.scheduling.fairness import (
	compute_entries_frt_hours,
	compute_entry_target_day_minutes,
	compute_fairness_ledger,
	fetch_leaves_with_period,
	load_active_technician_employee_ids,
)
from ism.scheduling.shifts import (
	apply_leave_to_shift_window,
	get_technician_shift_window,
	is_interval_within_shift,
)
from ism.scheduling.skills import (
	get_required_skills_for_schedule_entry,
	is_technician_qualified,
	load_active_technician_profiles,
)
from ism.scheduling.time_utils import (
	calculate_working_span,
	get_shop_settings,
	normalize_date,
	normalize_time,
	to_timedelta,
)

REBALANCEABLE_STATUSES: tuple[str, ...] = ("Draft", "Scheduled", "Confirmed")

LOCK_RO_STATUSES: tuple[str, ...] = (
	"Checked In",
	"Authorized - In Progress",
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
)

LOCK_RO_APPROVAL_STATUSES: tuple[str, ...] = ("Approved", "Partially Approved")


def _release_savepoint_safe(savepoint_name: str) -> None:
	if hasattr(frappe.db, "release_savepoint"):
		try:
			frappe.db.release_savepoint(savepoint_name)
		except Exception:
			pass


def _rollback_savepoint_safe(savepoint_name: str) -> None:
	try:
		frappe.db.rollback(save_point=savepoint_name)
	except Exception:
		pass


def _acquire_canonical_rebalance_locks(target_date: str, settings: dict[str, Any]) -> None:
	"""Acquire ISM canonical lock order: Schedule Entry -> Service Bay -> Employee (ORDER BY name ASC)."""
	# 1. Target Schedule Entry rows
	frappe.db.sql(
		"""
		SELECT `name`
		FROM `tabSchedule Entry`
		WHERE `scheduled_date` <= %s
		  AND (`completion_date` >= %s OR `completion_date` IS NULL)
		  AND `status` NOT IN %s
		ORDER BY `name` ASC
		FOR UPDATE
		""",
		(target_date, target_date, tuple(NON_BLOCKING_ENTRY_STATUSES)),
	)

	# 2. Active Service Bay rows
	if frappe.db.exists("DocType", "Service Bay"):
		sb = frappe.qb.DocType("Service Bay")
		(
			frappe.qb.from_(sb)
			.select(sb.name)
			.where(sb.is_active == 1)
			.orderby(sb.name)
			.for_update()
			.run()
		)

	# 3. Active technician Employee rows
	if frappe.db.exists("DocType", "Employee"):
		designation = (settings.get("technician_designation") or "Technician").strip()
		emp = frappe.qb.DocType("Employee")
		(
			frappe.qb.from_(emp)
			.select(emp.name)
			.where(emp.designation == designation)
			.where(emp.status == "Active")
			.orderby(emp.name)
			.for_update()
			.run()
		)


def _compute_intra_tier_spread_pct(
	ledger: Mapping[str, dict[str, Any]],
	profiles: Mapping[str, dict[str, Any]],
	on_duty_emp_ids: Sequence[str],
	policy_mode: str,
) -> float:
	"""Compute max daily utilization spread (%) among on-duty technicians (intra-tier for Waterfall)."""
	active_on_duty = [e for e in on_duty_emp_ids if e in ledger and e in profiles]
	if len(active_on_duty) < 2:
		return 0.0

	if policy_mode == "Equal Balance":
		utils = [flt(ledger[e]["target_day_util_pct"]) for e in active_on_duty]
		return round(max(utils) - min(utils), 2)

	if policy_mode == "Weighted Share":
		adj_utils = [
			flt(ledger[e]["target_day_util_pct"]) / max(0.01, flt(profiles[e].get("target_load_weight") or 1.0))
			for e in active_on_duty
		]
		return round(max(adj_utils) - min(adj_utils), 2)

	# Tiered Waterfall: max intra-tier spread across tiers with >= 2 on-duty technicians
	by_tier: dict[int, list[float]] = {}
	for emp_id in active_on_duty:
		tier = max(1, cint(profiles[emp_id].get("assignment_tier") or 1))
		by_tier.setdefault(tier, []).append(flt(ledger[emp_id]["target_day_util_pct"]))

	max_spread = 0.0
	for utils in by_tier.values():
		if len(utils) >= 2:
			spread = max(utils) - min(utils)
			if spread > max_spread:
				max_spread = spread
	return round(max_spread, 2)


def _sync_entry_assignment_to_related(
	se_name: str,
	ro_name: Optional[str],
	new_tech: str,
	service_bay: Optional[str],
	scheduled_date: str,
) -> None:
	"""Sync reassigned technician to linked Repair Order Service rows, RO schedule table, board thread, and realtime."""
	if ro_name:
		if (
			frappe.db.exists("DocType", "Repair Order Service")
			and frappe.db.has_column("Repair Order Service", "schedule_entry")
		):
			frappe.db.set_value(
				"Repair Order Service",
				{"parent": ro_name, "parenttype": "Repair Order", "schedule_entry": se_name},
				"assigned_technician",
				new_tech,
				update_modified=False,
			)
		sync_repair_order_schedule_entries(ro_name)

		if (
			frappe.db.exists("DocType", "Shop Board Thread")
			and frappe.db.has_column("Shop Board Thread", "assigned_technician")
		):
			thread_name = frappe.db.get_value("Shop Board Thread", {"repair_order": ro_name}, "name")
			if thread_name:
				frappe.db.set_value(
					"Shop Board Thread",
					thread_name,
					"assigned_technician",
					new_tech,
					update_modified=False,
				)

	try:
		frappe.publish_realtime(
			"ism_calendar_update",
			{"schedule_entry": se_name, "bay": service_bay, "date": scheduled_date},
		)
	except Exception:
		pass


def _upsert_rebalancer_action_item(
	*,
	dedup_key: str,
	title: str,
	details: str,
	reference_doctype: str = "Schedule Entry",
	reference_name: Optional[str] = None,
	priority: str = "High",
) -> Optional[str]:
	"""Create a deduplicated Shop Action Item (category='General', source='Agent') if no open item exists."""
	if not frappe.db.exists("DocType", "Shop Action Item"):
		return None

	existing = frappe.db.get_value(
		"Shop Action Item",
		{"dedup_key": dedup_key, "status": ["in", ["Open", "In Progress"]]},
		"name",
	)
	if existing:
		return str(existing)

	doc = frappe.get_doc({
		"doctype": "Shop Action Item",
		"title": title[:140],
		"category": "General",
		"priority": priority,
		"status": "Open",
		"source": "Agent",
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"dedup_key": dedup_key,
		"details": details,
	})
	doc.insert(ignore_permissions=True)
	return str(doc.name)


def _fetch_rebalance_day_entries(iso_date: str) -> list[dict[str, Any]]:
	"""Fetch all blocking Schedule Entry rows overlapping iso_date with full assignment metadata."""
	fields = [
		"name",
		"repair_order",
		"entry_type",
		"service_bay",
		"assigned_technician",
		"scheduled_date",
		"scheduled_time",
		"estimated_duration",
		"completion_date",
		"completion_time",
		"status",
		"waiter_preference",
		"booked_packages_json",
	]
	for opt_col in ("assignment_locked", "assignment_mode", "is_comeback", "required_skills_json"):
		if frappe.db.has_column("Schedule Entry", opt_col):
			fields.append(opt_col)

	return fetch_blocking_entries(iso_date, iso_date, fields=fields)


def _parse_entry_names(entry_names: Optional[Sequence[str]]) -> Optional[set[str]]:
	if entry_names is None:
		return None
	if isinstance(entry_names, str):
		text = entry_names.strip()
		if not text:
			return None
		try:
			parsed = json.loads(text)
			if isinstance(parsed, list):
				return {str(n).strip() for n in parsed if str(n or "").strip()}
		except Exception:
			pass
		return {n.strip() for n in text.split(",") if n.strip()}
	return {str(n).strip() for n in entry_names if str(n or "").strip()}


def preview_rebalance_day(target_date: Union[str, date, datetime]) -> dict[str, Any]:
	"""Dry-run rebalance of unlocked future/today entries without modifying service_bay or scheduled_time."""
	iso_date = normalize_date(target_date) or str(getdate(target_date))
	target_d = getdate(iso_date)
	today_d = getdate(today())

	if target_d < today_d:
		return {
			"date": iso_date,
			"changes": [],
			"before_spread_pct": 0.0,
			"after_spread_pct": 0.0,
			"locked_count": 0,
			"unlocked_count": 0,
		}

	shop = get_shop_settings()
	policy_mode = (shop.get("assignment_policy_mode") or "Tiered Waterfall").strip()
	active_emp_ids = load_active_technician_employee_ids(settings=shop)
	profiles = load_active_technician_profiles(active_emp_ids)
	is_holiday_fn = build_holiday_checker(shop.get("holiday_list"), min_date=iso_date)

	day_entries = _fetch_rebalance_day_entries(iso_date)
	before_ledger = compute_fairness_ledger(iso_date, employee_ids=sorted(profiles.keys()), settings=shop)
	on_duty_emp_ids = [
		e for e, row in before_ledger.items() if int(row.get("target_day_shift_minutes") or 0) > 0
	]
	before_spread = _compute_intra_tier_spread_pct(before_ledger, profiles, on_duty_emp_ids, policy_mode)

	unlocked_entries: list[dict[str, Any]] = []
	locked_entries: list[dict[str, Any]] = []
	fixed_blocking: list[dict[str, Any]] = []

	for entry in day_entries:
		sched_str = normalize_date(entry.get("scheduled_date")) or str(entry.get("scheduled_date") or "")
		is_same_day = sched_str == iso_date
		is_unlocked = (
			is_same_day
			and cint(entry.get("assignment_locked") or 0) == 0
			and (entry.get("status") or "").strip() in REBALANCEABLE_STATUSES
		)
		if is_unlocked:
			unlocked_entries.append(entry)
		else:
			fixed_blocking.append(entry)
			if is_same_day:
				locked_entries.append(entry)

	if not unlocked_entries or not profiles:
		return {
			"date": iso_date,
			"changes": [],
			"before_spread_pct": before_spread,
			"after_spread_pct": before_spread,
			"locked_count": len(locked_entries),
			"unlocked_count": len(unlocked_entries),
		}

	# Prepare simulated ledger by removing unlocked entries' FRT and target-day minutes
	unlocked_frt = compute_entries_frt_hours(unlocked_entries)
	unlocked_day_mins = {
		str(e["name"]): compute_entry_target_day_minutes(e, iso_date, is_holiday_fn=is_holiday_fn)
		for e in unlocked_entries
	}
	sim_ledger: dict[str, dict[str, Any]] = {emp_id: dict(row) for emp_id, row in before_ledger.items()}

	for entry in unlocked_entries:
		curr_tech = (entry.get("assigned_technician") or "").strip()
		if curr_tech in sim_ledger:
			se_name = str(entry["name"])
			row = sim_ledger[curr_tech]
			row["assigned_frt_hours"] = max(0.0, round(flt(row["assigned_frt_hours"]) - unlocked_frt.get(se_name, 0.0), 4))
			row["target_day_minutes"] = max(0, int(row["target_day_minutes"]) - unlocked_day_mins.get(se_name, 0))
			avail_h = flt(row["available_shift_hours"])
			weight = max(0.01, flt(row["target_load_weight"]))
			row["normalized_load"] = round(row["assigned_frt_hours"] / max(avail_h, 1.0), 6)
			row["weighted_load"] = round(row["assigned_frt_hours"] / max(avail_h * weight, 1.0), 6)
			shift_m = int(row["target_day_shift_minutes"])
			row["target_day_util_pct"] = (
				round((row["target_day_minutes"] / shift_m) * 100.0, 2)
				if shift_m > 0
				else (100.0 if row["target_day_minutes"] > 0 else 0.0)
			)

	# Precompute required skills & order unlocked entries: comebacks -> scarcest skill -> longest duration -> earliest
	entry_reqs: dict[str, list[tuple[str, int]]] = {}
	entry_qual_count: dict[str, int] = {}
	for entry in unlocked_entries:
		se_name = str(entry["name"])
		reqs = get_required_skills_for_schedule_entry(entry)
		entry_reqs[se_name] = reqs
		entry_qual_count[se_name] = sum(1 for p in profiles.values() if is_technician_qualified(p, reqs))

	ordered_unlocked = sorted(
		unlocked_entries,
		key=lambda e: (
			0 if cint(e.get("is_comeback") or 0) else 1,
			entry_qual_count[str(e["name"])],
			-flt(e.get("estimated_duration") or 0.0),
			normalize_time(e.get("scheduled_time")) or "00:00:00",
			str(e["name"]),
		),
	)

	# Preload leaves across max completion date of unlocked entries and skill weights once
	max_date_str = iso_date
	for entry in unlocked_entries:
		comp = normalize_date(entry.get("completion_date"))
		if comp and comp > max_date_str:
			max_date_str = comp
	leaves_by_emp = fetch_leaves_with_period(sorted(profiles.keys()), iso_date, max_date_str)
	skill_weights = _load_skill_scarcity_weights(profiles)

	sim_blocking = [dict(e) for e in fixed_blocking]
	changes: list[dict[str, Any]] = []

	for entry in ordered_unlocked:
		se_name = str(entry["name"])
		from_tech = (entry.get("assigned_technician") or "").strip() or None
		reqs = entry_reqs[se_name]

		decision = _select_technician_with_context(
			scheduled_date=iso_date,
			scheduled_time=entry.get("scheduled_time") or "08:00:00",
			duration_minutes=entry.get("estimated_duration") or 60,
			required_skills=reqs,
			repair_order=entry.get("repair_order"),
			entry_type=entry.get("entry_type"),
			waiter_preference=(entry.get("waiter_preference") or "Drop-Off"),
			is_comeback=bool(cint(entry.get("is_comeback") or 0)),
			exclude_entry=se_name,
			settings=shop,
			profiles_override=profiles,
			all_profiles_override=profiles,
			leaves_override=leaves_by_emp,
			blocking_entries_override=sim_blocking,
			ledger_override=sim_ledger,
			skill_weights_override=skill_weights,
			is_holiday_fn_override=is_holiday_fn,
			prefer_current_tech=from_tech,
		)

		to_tech = decision.get("assigned_technician")
		placed_tech = to_tech or from_tech

		if placed_tech and placed_tech in sim_ledger:
			row = sim_ledger[placed_tech]
			row["assigned_frt_hours"] = round(flt(row["assigned_frt_hours"]) + unlocked_frt.get(se_name, 0.0), 4)
			row["target_day_minutes"] = int(row["target_day_minutes"]) + unlocked_day_mins.get(se_name, 0)
			avail_h = flt(row["available_shift_hours"])
			weight = max(0.01, flt(row["target_load_weight"]))
			row["normalized_load"] = round(row["assigned_frt_hours"] / max(avail_h, 1.0), 6)
			row["weighted_load"] = round(row["assigned_frt_hours"] / max(avail_h * weight, 1.0), 6)
			shift_m = int(row["target_day_shift_minutes"])
			row["target_day_util_pct"] = (
				round((row["target_day_minutes"] / shift_m) * 100.0, 2)
				if shift_m > 0
				else (100.0 if row["target_day_minutes"] > 0 else 0.0)
			)

		placed_entry = dict(entry)
		placed_entry["assigned_technician"] = placed_tech
		sim_blocking.append(placed_entry)

		if to_tech and to_tech != from_tech:
			changes.append({
				"schedule_entry": se_name,
				"repair_order": entry.get("repair_order") or None,
				"from_tech": from_tech,
				"to_tech": to_tech,
				"reason": decision.get("assignment_reason") or "Rebalanced",
				"scheduled_time": normalize_time(entry.get("scheduled_time")) or str(entry.get("scheduled_time") or ""),
			})

	after_spread = _compute_intra_tier_spread_pct(sim_ledger, profiles, on_duty_emp_ids, policy_mode)
	changes.sort(key=lambda c: (c["scheduled_time"], c["schedule_entry"]))

	return {
		"date": iso_date,
		"changes": changes,
		"before_spread_pct": before_spread,
		"after_spread_pct": after_spread,
		"locked_count": len(locked_entries),
		"unlocked_count": len(unlocked_entries),
	}


def apply_rebalance_day(
	target_date: Union[str, date, datetime],
	entry_names: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
	"""Commit rebalanced technician assignments under canonical FOR UPDATE locks inside a SAVEPOINT."""
	iso_date = normalize_date(target_date) or str(getdate(target_date))
	if getdate(iso_date) < getdate(today()):
		return {
			"date": iso_date,
			"applied_count": 0,
			"changes": [],
			"before_spread_pct": 0.0,
			"after_spread_pct": 0.0,
			"locked_count": 0,
			"unlocked_count": 0,
		}

	allowed_names = _parse_entry_names(entry_names)
	shop = get_shop_settings()
	sp = f"ism_rebal_{frappe.generate_hash(length=8)}"
	prev_flag = getattr(frappe.flags, "ism_rebalancing", False)
	frappe.flags.ism_rebalancing = True

	try:
		frappe.db.savepoint(sp)
		_acquire_canonical_rebalance_locks(iso_date, shop)
		preview = preview_rebalance_day(iso_date)
		changes_to_apply = [
			c
			for c in preview["changes"]
			if allowed_names is None or c["schedule_entry"] in allowed_names
		]

		applied: list[dict[str, Any]] = []
		for change in changes_to_apply:
			se_name = change["schedule_entry"]
			row = frappe.db.get_value(
				"Schedule Entry",
				se_name,
				["status", "assignment_locked", "repair_order", "service_bay", "scheduled_date"],
				as_dict=True,
			)
			if not row:
				continue
			if cint(row.get("assignment_locked") or 0) != 0:
				continue
			if (row.get("status") or "").strip() not in REBALANCEABLE_STATUSES:
				continue

			update_vals: dict[str, Any] = {
				"assigned_technician": change["to_tech"],
				"assignment_mode": "Rebalanced",
				"assignment_reason": change["reason"],
			}
			frappe.db.set_value("Schedule Entry", se_name, update_vals)
			_sync_entry_assignment_to_related(
				se_name=se_name,
				ro_name=row.get("repair_order"),
				new_tech=change["to_tech"],
				service_bay=row.get("service_bay"),
				scheduled_date=str(row.get("scheduled_date") or iso_date),
			)
			applied.append(change)

		_release_savepoint_safe(sp)
		return {
			"date": iso_date,
			"applied_count": len(applied),
			"changes": applied,
			"before_spread_pct": preview["before_spread_pct"],
			"after_spread_pct": preview["after_spread_pct"],
			"locked_count": preview["locked_count"],
			"unlocked_count": preview["unlocked_count"],
		}
	except Exception:
		_rollback_savepoint_safe(sp)
		raise
	finally:
		frappe.flags.ism_rebalancing = prev_flag


def _entry_conflicts_with_shift(
	entry: dict[str, Any],
	date_str: str,
	eff_shift: Optional[dict[str, Any]],
	is_holiday_fn: Any,
) -> bool:
	"""Return True if any working segment of entry on date_str falls outside eff_shift."""
	if eff_shift is None:
		return True
	span = calculate_working_span(
		entry.get("scheduled_date"),
		entry.get("scheduled_time") or "08:00:00",
		entry.get("estimated_duration") or 60,
		is_holiday_fn=is_holiday_fn,
	)
	for seg in span.get("segments") or ():
		if seg.get("date") != date_str:
			continue
		if not is_interval_within_shift(
			to_timedelta(seg["start_time"]),
			to_timedelta(seg["end_time"]),
			eff_shift,
		):
			return True
	return False


def on_leave_application_change(doc: Any, method: Optional[str] = None) -> None:
	"""Reassign conflicting unlocked entries when leave is approved; alert on locked/unresolvable slots."""
	if not doc or getattr(frappe.flags, "ism_rebalancing", False):
		return

	employee = (getattr(doc, "employee", None) or "").strip()
	from_raw = getattr(doc, "from_date", None)
	to_raw = getattr(doc, "to_date", None)
	if not employee or not from_raw or not to_raw:
		return

	from_d = getdate(normalize_date(from_raw) or from_raw)
	to_d = getdate(normalize_date(to_raw) or to_raw)
	today_d = getdate(today())
	start_d = max(from_d, today_d)
	if to_d < start_d:
		return

	shop = get_shop_settings()
	docstatus = cint(getattr(doc, "docstatus", 0))
	status = (getattr(doc, "status", None) or "").strip()
	is_cancelled = docstatus == 2 or method == "on_cancel"

	if is_cancelled:
		threshold = flt(shop.get("auto_rebalance_skew_threshold_pct", 25.0))
		curr = start_d
		while curr <= to_d:
			d_str = str(curr)
			preview = preview_rebalance_day(d_str)
			if (
				preview["unlocked_count"] > 0
				and preview["before_spread_pct"] > threshold
				and preview["changes"]
				and preview["after_spread_pct"] < preview["before_spread_pct"]
			):
				apply_rebalance_day(d_str)
			curr = add_days(curr, 1)
		return

	if docstatus != 1 or status != "Approved":
		return

	profiles = load_active_technician_profiles([employee])
	emp_profile = profiles.get(employee)
	is_holiday_fn = build_holiday_checker(shop.get("holiday_list"), min_date=start_d)
	leaves_by_emp = fetch_leaves_with_period([employee], start_d, to_d)
	emp_leaves = leaves_by_emp.get(employee, [])

	# Ensure the triggering leave doc itself is represented even if queried mid-hook
	leave_name = str(getattr(doc, "name", "") or "leave")
	if not any(
		normalize_date(lv.get("from_date")) == str(from_d)
		and normalize_date(lv.get("to_date")) == str(to_d)
		and cint(lv.get("half_day") or 0) == cint(getattr(doc, "half_day", 0))
		for lv in emp_leaves
	):
		emp_leaves.append({
			"employee": employee,
			"from_date": str(from_d),
			"to_date": str(to_d),
			"half_day": cint(getattr(doc, "half_day", 0)),
			"half_day_date": getattr(doc, "half_day_date", None),
			"ism_half_day_period": getattr(doc, "ism_half_day_period", None),
		})

	se_fields = [
		"name",
		"repair_order",
		"entry_type",
		"service_bay",
		"assigned_technician",
		"scheduled_date",
		"scheduled_time",
		"estimated_duration",
		"completion_date",
		"completion_time",
		"status",
		"waiter_preference",
		"booked_packages_json",
	]
	for opt_col in ("assignment_locked", "assignment_mode", "is_comeback", "required_skills_json"):
		if frappe.db.has_column("Schedule Entry", opt_col):
			se_fields.append(opt_col)

	entries = fetch_blocking_entries(
		start_d,
		to_d,
		assigned_technician=employee,
		fields=se_fields,
	)
	if not entries:
		return

	conflicting_entries: list[dict[str, Any]] = []
	seen_se: set[str] = set()
	curr = start_d
	while curr <= to_d:
		d_str = str(curr)
		raw_shift = get_technician_shift_window(emp_profile, curr, settings=shop)
		eff_shift = apply_leave_to_shift_window(raw_shift, emp_leaves, curr)
		for entry in entries:
			se_name = str(entry["name"])
			if se_name in seen_se:
				continue
			sched_str = normalize_date(entry.get("scheduled_date")) or str(entry.get("scheduled_date") or "")
			comp_raw = entry.get("completion_date")
			comp_str = (normalize_date(comp_raw) or str(comp_raw)) if comp_raw else sched_str
			if not (sched_str <= d_str <= comp_str):
				continue
			if _entry_conflicts_with_shift(entry, d_str, eff_shift, is_holiday_fn):
				seen_se.add(se_name)
				conflicting_entries.append(entry)
		curr = add_days(curr, 1)

	if not conflicting_entries:
		return

	prev_flag = getattr(frappe.flags, "ism_rebalancing", False)
	frappe.flags.ism_rebalancing = True
	try:
		for entry in conflicting_entries:
			se_name = str(entry["name"])
			sched_str = normalize_date(entry.get("scheduled_date")) or str(entry.get("scheduled_date") or "")
			is_locked = cint(entry.get("assignment_locked") or 0) == 1
			se_status = (entry.get("status") or "").strip()

			if is_locked or se_status not in REBALANCEABLE_STATUSES:
				_upsert_rebalancer_action_item(
					dedup_key=f"leave_conflict:locked:{se_name}:{leave_name}",
					title=f"Locked assignment {se_name} conflicts with approved leave ({employee})",
					details=(
						f"Leave Application {leave_name} for {employee} ({from_d} to {to_d}) conflicts with "
						f"Schedule Entry {se_name} on {sched_str} at {entry.get('scheduled_time')} "
						f"(status={se_status}, assignment_locked={int(is_locked)}). Manual reassignment required."
					),
					reference_doctype="Schedule Entry",
					reference_name=se_name,
				)
				continue

			sp = f"ism_lv_{frappe.generate_hash(length=8)}"
			try:
				frappe.db.savepoint(sp)
				_acquire_canonical_rebalance_locks(sched_str, shop)
				reqs = get_required_skills_for_schedule_entry(entry)
				replacement_pool = [
					e for e in load_active_technician_employee_ids(settings=shop) if e != employee
				]
				decision = select_technician_for_slot(
					scheduled_date=sched_str,
					scheduled_time=entry.get("scheduled_time") or "08:00:00",
					duration_minutes=entry.get("estimated_duration") or 60,
					required_skills=reqs,
					repair_order=entry.get("repair_order"),
					entry_type=entry.get("entry_type"),
					waiter_preference=(entry.get("waiter_preference") or "Drop-Off"),
					is_comeback=bool(cint(entry.get("is_comeback") or 0)),
					exclude_entry=se_name,
					candidate_employee_ids=replacement_pool,
					settings=shop,
				)
				new_tech = decision.get("assigned_technician")
				if new_tech and new_tech != employee:
					reason = f"Rebalanced (Leave {leave_name}): {decision.get('assignment_reason') or ''}".strip()
					frappe.db.set_value(
						"Schedule Entry",
						se_name,
						{
							"assigned_technician": new_tech,
							"assignment_mode": "Rebalanced",
							"assignment_reason": reason,
						},
					)
					_sync_entry_assignment_to_related(
						se_name=se_name,
						ro_name=entry.get("repair_order"),
						new_tech=new_tech,
						service_bay=entry.get("service_bay"),
						scheduled_date=sched_str,
					)
					_release_savepoint_safe(sp)
				else:
					_release_savepoint_safe(sp)
					_upsert_rebalancer_action_item(
						dedup_key=f"leave_conflict:unresolvable:{se_name}:{leave_name}",
						title=f"No replacement tech for {se_name} during leave ({employee})",
						details=(
							f"Leave Application {leave_name} for {employee} ({from_d} to {to_d}) conflicts with "
							f"Schedule Entry {se_name} on {sched_str} at {entry.get('scheduled_time')}, "
							f"and no qualified available technician could cover the slot."
						),
						reference_doctype="Schedule Entry",
						reference_name=se_name,
					)
			except Exception:
				_rollback_savepoint_safe(sp)
				raise
	finally:
		frappe.flags.ism_rebalancing = prev_flag


def on_schedule_entry_change(doc: Any, method: Optional[str] = None) -> None:
	"""Rebalance unlocked entries on affected date(s) when cancellation, No Show, or reschedule skews load."""
	if not doc or getattr(frappe.flags, "ism_rebalancing", False):
		return

	status = (getattr(doc, "status", None) or "").strip()
	doc_before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	prev_status = (getattr(doc_before, "status", None) or "").strip() if doc_before else ""

	curr_date = normalize_date(getattr(doc, "scheduled_date", None))
	prev_date = normalize_date(getattr(doc_before, "scheduled_date", None)) if doc_before else None
	curr_time = normalize_time(getattr(doc, "scheduled_time", None))
	prev_time = normalize_time(getattr(doc_before, "scheduled_time", None)) if doc_before else None

	dates_to_check: list[str] = []
	if status in ("Cancelled", "No Show") and (not doc_before or prev_status != status):
		if curr_date:
			dates_to_check.append(curr_date)
	elif doc_before and ((prev_date and prev_date != curr_date) or (prev_time and prev_time != curr_time)):
		if prev_date:
			dates_to_check.append(prev_date)
		if curr_date and curr_date not in dates_to_check:
			dates_to_check.append(curr_date)

	if not dates_to_check:
		return

	shop = get_shop_settings()
	threshold = flt(shop.get("auto_rebalance_skew_threshold_pct", 25.0))
	today_d = getdate(today())

	for d_str in dates_to_check:
		if getdate(d_str) < today_d:
			continue
		preview = preview_rebalance_day(d_str)
		if (
			preview["unlocked_count"] > 0
			and preview["before_spread_pct"] > threshold
			and preview["changes"]
			and preview["after_spread_pct"] < preview["before_spread_pct"]
		):
			apply_rebalance_day(d_str)


def on_repair_order_lock_assignments(doc: Any, method: Optional[str] = None) -> None:
	"""Lock non-cancelled linked Schedule Entry rows when RO checks in, authorizes, or is picked up."""
	if not doc or not getattr(doc, "name", None):
		return
	if not frappe.db.has_column("Schedule Entry", "assignment_locked"):
		return

	status = (getattr(doc, "status", None) or "").strip()
	approval_status = (getattr(doc, "approval_status", None) or "").strip()
	picked_up_at = getattr(doc, "picked_up_at", None)

	should_lock = (
		status in LOCK_RO_STATUSES
		or approval_status in LOCK_RO_APPROVAL_STATUSES
		or bool(picked_up_at)
	)
	if not should_lock:
		return

	entry_names = frappe.get_all(
		"Schedule Entry",
		filters={
			"repair_order": doc.name,
			"status": ["not in", ["Cancelled", "No Show"]],
			"assignment_locked": 0,
		},
		pluck="name",
		ignore_permissions=True,
	)
	if not entry_names:
		return

	frappe.db.set_value(
		"Schedule Entry",
		{"name": ["in", entry_names]},
		"assignment_locked",
		1,
		update_modified=False,
	)


def run_morning_auto_rebalance() -> None:
	"""Daily scheduler hook gated by ISM Settings.enable_morning_auto_rebalance."""
	shop = get_shop_settings()
	if not cint(shop.get("enable_morning_auto_rebalance", 0)):
		return

	try:
		apply_rebalance_day(today())
	except Exception:
		frappe.log_error(
			title="Morning Auto-Rebalance Failed",
			message=frappe.get_traceback(),
		)

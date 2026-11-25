# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Rolling-window leave-normalized FRT fairness ledger for technician assignment."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Optional, Union

import frappe
from frappe.utils import add_days, cint, flt, getdate

from ism.scheduling.capacity import INACTIVE_WORK_STATUSES
from ism.scheduling.entry_queries import build_holiday_checker
from ism.scheduling.shifts import (
	apply_leave_to_shift_window,
	calculate_technician_available_hours,
	get_technician_shift_window,
)
from ism.scheduling.skills import load_active_technician_profiles
from ism.scheduling.time_utils import (
	calculate_working_span,
	get_shop_settings,
	normalize_date,
)


def load_active_technician_employee_ids(
	employee_ids: Optional[Sequence[str]] = None,
	settings: Optional[dict[str, Any]] = None,
) -> list[str]:
	"""Return active Employee IDs eligible as technicians.

	When employee_ids is provided, filters that explicit set to status='Active'.
	When omitted, queries active Employees matching ISM Settings.technician_designation.
	"""
	if not frappe.db.exists("DocType", "Employee"):
		return []

	filters: dict[str, Any] = {"status": "Active"}
	if employee_ids is not None:
		clean_ids = list(dict.fromkeys(str(e).strip() for e in employee_ids if str(e or "").strip()))
		if not clean_ids:
			return []
		filters["name"] = ["in", clean_ids]
	else:
		shop = settings if settings is not None else get_shop_settings()
		designation = (shop.get("technician_designation") or "Technician").strip()
		filters["designation"] = designation

	rows = frappe.get_all(
		"Employee",
		filters=filters,
		pluck="name",
		order_by="name asc",
		ignore_permissions=True,
	)
	return [str(name) for name in rows if name]


def fetch_leaves_with_period(
	employees: Sequence[str],
	min_date: Union[str, date, datetime],
	max_date: Union[str, date, datetime],
) -> dict[str, list[dict[str, Any]]]:
	"""Batch-fetch approved Leave Applications overlapping [min_date, max_date], including ism_half_day_period."""
	min_str = normalize_date(min_date) or str(min_date or "")
	max_str = normalize_date(max_date) or str(max_date or "")
	emp_list = list(dict.fromkeys(str(e).strip() for e in (employees or ()) if str(e or "").strip()))
	if not emp_list or not min_str or not max_str or not frappe.db.exists("DocType", "Leave Application"):
		return {}

	fields = ["employee", "from_date", "to_date", "half_day", "half_day_date"]
	if frappe.db.has_column("Leave Application", "ism_half_day_period"):
		fields.append("ism_half_day_period")

	rows = frappe.get_all(
		"Leave Application",
		filters=[
			["employee", "in", emp_list],
			["status", "=", "Approved"],
			["docstatus", "=", 1],
			["from_date", "<=", max_str],
			["to_date", ">=", min_str],
		],
		fields=fields,
		ignore_permissions=True,
	)

	leaves_by_emp: dict[str, list[dict[str, Any]]] = {}
	for row in rows:
		emp_id = row.get("employee")
		if emp_id:
			leaves_by_emp.setdefault(str(emp_id), []).append(dict(row))
	return leaves_by_emp


def compute_entries_frt_hours(entries: Sequence[dict[str, Any]]) -> dict[str, float]:
	"""Compute fairness-eligible FRT hours for each Schedule Entry.

	- Excludes Cancelled, No Show, and is_comeback == 1 (returns 0.0).
	- Sums non-declined linked Repair Order Service.labor_hours (where schedule_entry == se.name,
	  or all non-declined non-comeback RO services if there is only a single active entry on the RO).
	- Falls back to estimated_duration / 60.0 when no qualifying FRT rows exist.
	"""
	result: dict[str, float] = {}
	if not entries:
		return result

	ro_names: list[str] = []
	for se in entries:
		se_name = str(se.get("name") or "")
		if not se_name:
			continue
		status = (se.get("status") or "").strip()
		if status in INACTIVE_WORK_STATUSES or cint(se.get("is_comeback") or 0):
			result[se_name] = 0.0
			continue
		ro = (se.get("repair_order") or "").strip()
		if ro:
			ro_names.append(ro)

	unique_ros = list(dict.fromkeys(ro_names))
	ros_by_parent: dict[str, list[dict[str, Any]]] = {}
	ro_active_se_count: dict[str, int] = {}

	if unique_ros and frappe.db.exists("DocType", "Repair Order Service"):
		ros_fields = ["name", "parent", "status", "labor_hours"]
		if frappe.db.has_column("Repair Order Service", "schedule_entry"):
			ros_fields.append("schedule_entry")
		if frappe.db.has_column("Repair Order Service", "is_comeback"):
			ros_fields.append("is_comeback")

		ros_rows = frappe.get_all(
			"Repair Order Service",
			filters={"parent": ["in", unique_ros], "parenttype": "Repair Order"},
			fields=ros_fields,
			ignore_permissions=True,
		)
		for row in ros_rows:
			parent = row.get("parent")
			if parent:
				ros_by_parent.setdefault(str(parent), []).append(dict(row))

		se_rows = frappe.get_all(
			"Schedule Entry",
			filters={
				"repair_order": ["in", unique_ros],
				"status": ["not in", list(INACTIVE_WORK_STATUSES)],
			},
			fields=["name", "repair_order"],
			ignore_permissions=True,
		)
		for row in se_rows:
			ro = row.get("repair_order")
			if ro:
				ro_active_se_count[str(ro)] = ro_active_se_count.get(str(ro), 0) + 1

	for se in entries:
		se_name = str(se.get("name") or "")
		if not se_name or se_name in result:
			continue

		ro = (se.get("repair_order") or "").strip()
		fallback_hours = max(0.0, flt(se.get("estimated_duration") or 0.0) / 60.0)

		if not ro or ro not in ros_by_parent:
			result[se_name] = round(fallback_hours, 4)
			continue

		non_declined = [
			r for r in ros_by_parent[ro] if (r.get("status") or "").strip() != "Declined"
		]
		linked = [r for r in non_declined if (r.get("schedule_entry") or "").strip() == se_name]

		if linked:
			non_comeback = [r for r in linked if not cint(r.get("is_comeback") or 0)]
			if not non_comeback:
				result[se_name] = 0.0
				continue
			frt_sum = sum(max(0.0, flt(r.get("labor_hours") or 0.0)) for r in non_comeback)
			result[se_name] = round(frt_sum if frt_sum > 0 else fallback_hours, 4)
			continue

		if ro_active_se_count.get(ro, 0) == 1 and non_declined:
			non_comeback = [r for r in non_declined if not cint(r.get("is_comeback") or 0)]
			if not non_comeback:
				result[se_name] = 0.0
				continue
			frt_sum = sum(max(0.0, flt(r.get("labor_hours") or 0.0)) for r in non_comeback)
			result[se_name] = round(frt_sum if frt_sum > 0 else fallback_hours, 4)
			continue

		result[se_name] = round(fallback_hours, 4)

	return result


def compute_entry_target_day_minutes(
	entry: dict[str, Any],
	target_date_str: str,
	is_holiday_fn: Any = None,
) -> int:
	"""Return active working minutes that a Schedule Entry occupies on target_date_str."""
	sched_str = normalize_date(entry.get("scheduled_date")) or str(entry.get("scheduled_date") or "")
	comp_raw = entry.get("completion_date")
	comp_str = (normalize_date(comp_raw) or str(comp_raw)) if comp_raw else sched_str

	if not sched_str:
		return 0
	if comp_str < target_date_str or sched_str > target_date_str:
		return 0

	dur_mins = max(0.0, flt(entry.get("estimated_duration") or 0.0))
	if sched_str == target_date_str and comp_str == target_date_str:
		return int(round(dur_mins))

	span = calculate_working_span(
		sched_str,
		entry.get("scheduled_time") or "08:00:00",
		dur_mins or 60.0,
		is_holiday_fn=is_holiday_fn,
	)
	day_mins = sum(
		flt(seg.get("duration") or 0.0)
		for seg in (span.get("segments") or ())
		if seg.get("date") == target_date_str
	)
	return int(round(day_mins))


def compute_fairness_ledger(
	target_date: Union[str, date, datetime],
	*,
	window_days: Optional[int] = None,
	employee_ids: Optional[Sequence[str]] = None,
	exclude_entry: Optional[str] = None,
	settings: Optional[dict[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
	"""Compute per-employee rolling-window fairness & target-day utilization metrics.

	Window is inclusive: [target_date - (window_days - 1), target_date].
	"""
	shop = settings if settings is not None else get_shop_settings()
	t_date = getdate(normalize_date(target_date) or target_date)
	target_str = str(t_date)
	w_days = max(1, cint(window_days if window_days is not None else shop.get("fairness_window_days", 7)))
	start_date = add_days(t_date, -(w_days - 1))
	start_str = str(start_date)

	active_emp_ids = load_active_technician_employee_ids(employee_ids=employee_ids, settings=shop)
	if not active_emp_ids:
		return {}

	profiles = load_active_technician_profiles(active_emp_ids)
	if not profiles:
		return {}

	emp_list = sorted(profiles.keys())
	is_holiday_fn = build_holiday_checker(shop.get("holiday_list"), min_date=start_date)
	leaves_by_emp = fetch_leaves_with_period(emp_list, start_date, t_date)

	lookback_str = str(add_days(start_date, -14))
	se_fields = [
		"name",
		"repair_order",
		"assigned_technician",
		"scheduled_date",
		"scheduled_time",
		"estimated_duration",
		"completion_date",
		"completion_time",
		"status",
		"creation",
		"modified",
	]
	has_is_comeback = frappe.db.has_column("Schedule Entry", "is_comeback")
	if has_is_comeback:
		se_fields.append("is_comeback")

	se = frappe.qb.DocType("Schedule Entry")
	query = (
		frappe.qb.from_(se)
		.select(*(getattr(se, f) for f in se_fields))
		.where(se.assigned_technician.isin(emp_list))
		.where(se.status.notin(list(INACTIVE_WORK_STATUSES)))
		.where(se.scheduled_date <= target_str)
		.where(
			(se.completion_date >= start_str)
			| (se.completion_date.isnull() & (se.scheduled_date >= lookback_str))
		)
	)
	if exclude_entry:
		query = query.where(se.name != exclude_entry)

	entries = query.run(as_dict=True)
	frt_map = compute_entries_frt_hours(entries)

	frt_by_emp: dict[str, float] = {e: 0.0 for e in emp_list}
	day_mins_by_emp: dict[str, int] = {e: 0 for e in emp_list}
	last_assigned_by_emp: dict[str, Optional[str]] = {e: None for e in emp_list}

	for entry in entries:
		emp_id = str(entry.get("assigned_technician") or "")
		if emp_id not in profiles:
			continue
		se_name = str(entry.get("name") or "")
		sched_str = normalize_date(entry.get("scheduled_date")) or str(entry.get("scheduled_date") or "")

		if start_str <= sched_str <= target_str:
			frt_by_emp[emp_id] += frt_map.get(se_name, 0.0)
			ts = str(entry.get("modified") or entry.get("creation") or "")
			if ts and (last_assigned_by_emp[emp_id] is None or ts > last_assigned_by_emp[emp_id]):
				last_assigned_by_emp[emp_id] = ts

		day_mins = compute_entry_target_day_minutes(entry, target_str, is_holiday_fn=is_holiday_fn)
		if day_mins > 0:
			day_mins_by_emp[emp_id] += day_mins

	target_is_holiday = is_holiday_fn(t_date)
	ledger: dict[str, dict[str, Any]] = {}

	for emp_id in emp_list:
		profile = profiles[emp_id]
		emp_leaves = leaves_by_emp.get(emp_id, ())
		avail_hours = calculate_technician_available_hours(
			profile,
			emp_leaves,
			start_date,
			t_date,
			is_holiday_fn=is_holiday_fn,
			settings=shop,
		)

		if target_is_holiday:
			target_day_shift_minutes = 0
		else:
			shift = get_technician_shift_window(profile, t_date, settings=shop)
			eff_shift = apply_leave_to_shift_window(shift, emp_leaves, t_date)
			target_day_shift_minutes = int(eff_shift.get("net_minutes") or 0) if eff_shift else 0

		assigned_frt = round(frt_by_emp[emp_id], 4)
		weight = max(0.01, flt(profile.get("target_load_weight") or 1.0))
		norm_load = round(assigned_frt / max(avail_hours, 1.0), 6)
		weighted_load = round(assigned_frt / max(avail_hours * weight, 1.0), 6)

		day_mins = int(day_mins_by_emp[emp_id])
		if target_day_shift_minutes > 0:
			util_pct = round((day_mins / target_day_shift_minutes) * 100.0, 2)
		else:
			util_pct = 100.0 if day_mins > 0 else 0.0

		ledger[emp_id] = {
			"employee": emp_id,
			"assignment_tier": max(1, cint(profile.get("assignment_tier") or 1)),
			"target_load_weight": weight,
			"assigned_frt_hours": assigned_frt,
			"available_shift_hours": round(avail_hours, 4),
			"normalized_load": norm_load,
			"weighted_load": weighted_load,
			"target_day_minutes": day_mins,
			"target_day_shift_minutes": target_day_shift_minutes,
			"target_day_util_pct": util_pct,
			"last_assigned_at": last_assigned_by_emp[emp_id],
		}

	return ledger

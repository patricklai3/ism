# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Union

import frappe
from frappe.utils import add_days, getdate

from ism.scheduling.entry_queries import (
	build_holiday_checker,
	fetch_blocking_entries,
	fetch_leaves_by_employee as _get_leaves_by_employee,
	fetch_queue_entries_for_date,
	intervals_overlap,
	is_employee_on_leave_for_date as _is_employee_on_leave_for_date,
)
from ism.scheduling.shifts import (
	apply_leave_to_shift_window,
	get_technician_shift_window,
	is_interval_within_shift,
)
from ism.scheduling.skills import (
	is_technician_qualified,
	load_active_technician_profiles,
	normalize_skill_requirements,
)
from ism.scheduling.time_utils import (
	calculate_working_span,
	effective_end_time,
	format_timedelta,
	get_shop_settings,
	normalize_date,
	normalize_time,
	to_timedelta,
)


def _load_inactive_profile_ids(employee_ids: List[str]) -> set[str]:
	"""Return employee IDs that explicitly have an inactive Technician Profile (is_active = 0)."""
	if not employee_ids or not frappe.db.exists("DocType", "Technician Profile"):
		return set()
	rows = frappe.get_all(
		"Technician Profile",
		filters={"employee": ["in", employee_ids], "is_active": 0},
		pluck="employee",
		ignore_permissions=True,
	)
	return {str(emp) for emp in rows if emp}


@frappe.whitelist(methods=["GET", "POST"])
def get_active_technicians(date: Optional[Union[str, date, datetime]] = None) -> List[Dict[str, Any]]:
	"""
	Returns active employees matching ISM Settings -> technician_designation.
	If date is provided, includes leave status from approved Leave Applications.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	settings = get_shop_settings()
	designation = settings.get("technician_designation") or "Technician"

	employees = frappe.get_all(
		"Employee",
		filters={
			"designation": designation,
			"status": "Active",
		},
		fields=["name", "employee_name", "designation"],
		order_by="employee_name asc, name asc",
	)

	target_date = (normalize_date(date) or str(date)) if date else None
	leaves_by_emp: Dict[str, List[Dict[str, Any]]] = {}
	if target_date and employees:
		emp_ids = [emp["name"] for emp in employees]
		leaves_by_emp = _get_leaves_by_employee(emp_ids, target_date, target_date)

	results = []
	for emp in employees:
		emp_id = emp["name"]
		leave_res = (
			_is_employee_on_leave_for_date(leaves_by_emp.get(emp_id, ()), target_date)
			if target_date
			else {"on_leave": False, "half_day": False, "half_day_period": None}
		)
		results.append({
			"employee": emp_id,
			"employee_name": emp.get("employee_name") or emp_id,
			"designation": emp.get("designation") or designation,
			"on_leave": leave_res["on_leave"],
			"half_day": leave_res["half_day"],
			"half_day_period": leave_res.get("half_day_period"),
		})

	return results


def check_employee_leave(
	employee: str,
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
) -> Dict[str, Any]:
	"""
	Internal helper to check if an employee is on leave during any active working segment of a slot.
	"""
	target_date = (normalize_date(date) or str(date)) if date else None
	if not target_date or not employee or not frappe.db.exists("DocType", "Leave Application"):
		return {"on_leave": False, "half_day": False, "half_day_period": None}

	norm_start = normalize_time(start_time) if start_time is not None else None
	duration_val = float(duration_minutes or 0)
	if norm_start is not None and duration_val > 0:
		settings = get_shop_settings()
		holiday_fn = build_holiday_checker(settings.get("holiday_list"), target_date)
		span = calculate_working_span(target_date, norm_start, duration_val, is_holiday_fn=holiday_fn)
		seg_dates = list(dict.fromkeys([s["date"] for s in span["segments"]] or [target_date]))
	else:
		seg_dates = [target_date]

	emp_leaves = _get_leaves_by_employee([employee], min(seg_dates), max(seg_dates)).get(employee, [])
	if not emp_leaves:
		return {"on_leave": False, "half_day": False, "half_day_period": None}

	half_day_hit = False
	half_day_period: Optional[str] = None
	for seg_d in seg_dates:
		leave_res = _is_employee_on_leave_for_date(emp_leaves, seg_d)
		if leave_res["on_leave"]:
			if not leave_res["half_day"]:
				return {"on_leave": True, "half_day": False, "half_day_period": None}
			half_day_hit = True
			if half_day_period is None:
				half_day_period = leave_res.get("half_day_period")

	if half_day_hit:
		return {"on_leave": True, "half_day": True, "half_day_period": half_day_period}
	return {"on_leave": False, "half_day": False, "half_day_period": None}


@frappe.whitelist(methods=["GET", "POST"])
def get_technician_pool_availability(
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
	required_skills: Optional[Any] = None,
) -> Dict[str, Any]:
	"""
	Pool-level capacity gating function.
	Checks total, on-duty, occupied (assigned + unassigned), and available technicians
	across all working segments, enforcing per-tech weekly shifts, AM/PM half-day leaves,
	and optional skill+difficulty requirements.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(frappe._("Invalid exclude_entry identifier."), frappe.ValidationError)
	settings = get_shop_settings()
	if not settings.get("enable_technician_capacity"):
		return {
			"total_technicians": 999,
			"on_leave": 0,
			"on_duty": 999,
			"occupied": 0,
			"unassigned_occupied": 0,
			"available": 999,
			"is_available": True,
		}

	req_skills = normalize_skill_requirements(required_skills)
	target_date = normalize_date(date) or str(date)
	norm_start = normalize_time(start_time) or start_time
	technicians = get_active_technicians(date=target_date)
	total_techs = len(technicians)
	if total_techs == 0:
		return {
			"total_technicians": 0,
			"qualified_technicians": 0,
			"on_leave": 0,
			"on_duty": 0,
			"occupied": 0,
			"unassigned_occupied": 0,
			"available": 0,
			"available_technicians": [],
			"is_available": False,
		}

	all_tech_ids = [t["employee"] for t in technicians]
	profiles = load_active_technician_profiles(all_tech_ids)
	inactive_profile_ids = _load_inactive_profile_ids(all_tech_ids)

	eligible_tech_ids: List[str] = []
	for emp_id in all_tech_ids:
		if emp_id in inactive_profile_ids:
			continue
		if req_skills:
			profile = profiles.get(emp_id)
			if not profile or not is_technician_qualified(profile, req_skills):
				continue
		eligible_tech_ids.append(emp_id)

	if not eligible_tech_ids:
		return {
			"total_technicians": total_techs,
			"qualified_technicians": 0,
			"on_leave": 0,
			"on_duty": 0,
			"occupied": 0,
			"unassigned_occupied": 0,
			"available": 0,
			"available_technicians": [],
			"is_available": False,
		}

	holiday_fn = build_holiday_checker(
		settings.get("holiday_list"),
		add_days(getdate(target_date), -30),
	)
	span = calculate_working_span(target_date, norm_start, duration_minutes, is_holiday_fn=holiday_fn)
	seg_dates = [s["date"] for s in span["segments"]] or [target_date]

	leaves_by_emp = _get_leaves_by_employee(eligible_tech_ids, min(seg_dates), max(seg_dates))
	all_entries = fetch_blocking_entries(
		min(seg_dates),
		max(seg_dates),
		exclude_entry=exclude_entry,
		fields=["name", "assigned_technician", "scheduled_date", "scheduled_time", "estimated_duration"],
	)

	eligible_set = set(eligible_tech_ids)
	entry_segments_by_tech: Dict[str, List[Dict[str, Any]]] = {}
	unassigned_segments_by_date: Dict[str, List[Dict[str, Any]]] = {}
	for ent in all_entries:
		tid = (ent.get("assigned_technician") or "").strip()
		if tid and tid not in eligible_set:
			continue
		e_dur = ent.get("estimated_duration") or 0
		e_span = calculate_working_span(
			ent.get("scheduled_date"),
			ent.get("scheduled_time"),
			e_dur,
			is_holiday_fn=holiday_fn,
		)
		if tid:
			tech_segs = entry_segments_by_tech.setdefault(tid, [])
			for s in e_span["segments"]:
				tech_segs.append({
					"date": s["date"],
					"start": to_timedelta(s["start_time"]),
					"end": to_timedelta(s["end_time"]),
				})
		else:
			entry_name = ent.get("name") or ""
			for s in e_span["segments"]:
				unassigned_segments_by_date.setdefault(s["date"], []).append({
					"name": entry_name,
					"start": to_timedelta(s["start_time"]),
					"end": to_timedelta(s["end_time"]),
				})

	min_available = len(eligible_tech_ids)
	max_occupied = 0
	max_unassigned = 0
	max_on_leave = 0
	min_on_duty = len(eligible_tech_ids)
	available_tech_ids = list(eligible_tech_ids)

	for prop_seg in span["segments"]:
		seg_date = prop_seg["date"]
		seg_start = to_timedelta(prop_seg["start_time"])
		seg_end = to_timedelta(prop_seg["end_time"])

		on_duty_in_seg: List[str] = []
		on_leave_in_seg = 0
		for emp_id in eligible_tech_ids:
			profile = profiles.get(emp_id)
			base_shift = get_technician_shift_window(profile, str(seg_date), settings=settings)
			if not base_shift:
				continue
			eff_shift = apply_leave_to_shift_window(base_shift, leaves_by_emp.get(emp_id, ()), str(seg_date))
			if not eff_shift:
				on_leave_in_seg += 1
				continue
			if not is_interval_within_shift(seg_start, seg_end, eff_shift):
				if is_interval_within_shift(seg_start, seg_end, base_shift):
					on_leave_in_seg += 1
				continue
			on_duty_in_seg.append(emp_id)

		occupied_in_seg = set()
		for emp_id in on_duty_in_seg:
			for ts in entry_segments_by_tech.get(emp_id, ()):
				if ts["date"] == seg_date and intervals_overlap(seg_start, seg_end, ts["start"], ts["end"]):
					occupied_in_seg.add(emp_id)
					break

		unassigned_names = {
			u["name"]
			for u in unassigned_segments_by_date.get(seg_date, ())
			if intervals_overlap(seg_start, seg_end, u["start"], u["end"])
		}
		unassigned_in_seg = len(unassigned_names)

		free_in_seg = [emp_id for emp_id in on_duty_in_seg if emp_id not in occupied_in_seg]
		available_tech_ids = [emp_id for emp_id in available_tech_ids if emp_id in free_in_seg]
		avail_in_seg = max(0, len(free_in_seg) - unassigned_in_seg)
		total_occupied_in_seg = len(occupied_in_seg) + unassigned_in_seg

		if avail_in_seg < min_available:
			min_available = avail_in_seg
		if total_occupied_in_seg > max_occupied:
			max_occupied = total_occupied_in_seg
		if unassigned_in_seg > max_unassigned:
			max_unassigned = unassigned_in_seg
		if on_leave_in_seg > max_on_leave:
			max_on_leave = on_leave_in_seg
		if len(on_duty_in_seg) < min_on_duty:
			min_on_duty = len(on_duty_in_seg)

	return {
		"total_technicians": total_techs,
		"qualified_technicians": len(eligible_tech_ids),
		"on_leave": max_on_leave,
		"on_duty": min_on_duty,
		"occupied": max_occupied,
		"unassigned_occupied": max_unassigned,
		"available": min_available,
		"available_technicians": available_tech_ids if min_available > 0 else [],
		"is_available": min_available > 0,
	}


@frappe.whitelist(methods=["GET", "POST"])
def check_technician_availability(
	employee: str,
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
	required_skills: Optional[Any] = None,
) -> Dict[str, Any]:
	"""
	Checks availability of a specific technician for a given date/time window.
	Enforces active profile, optional skill qualification, per-tech weekly shift,
	AM/PM half-day leave, and assigned entry overlaps across active working segments.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if not employee or not isinstance(employee, str) or not date:
		return {
			"is_available": False,
			"reason": "invalid_parameters",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(frappe._("Invalid exclude_entry identifier."), frappe.ValidationError)

	settings = get_shop_settings()
	designation = settings.get("technician_designation") or "Technician"

	emp_doc = frappe.db.get_value(
		"Employee",
		employee,
		["status", "designation"],
		as_dict=True,
	)

	if not emp_doc or emp_doc.status != "Active":
		return {
			"is_available": False,
			"reason": "inactive",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	if emp_doc.designation != designation:
		return {
			"is_available": False,
			"reason": "designation_mismatch",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	inactive_profile_ids = _load_inactive_profile_ids([employee])
	if employee in inactive_profile_ids:
		return {
			"is_available": False,
			"reason": "inactive",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	profile = load_active_technician_profiles([employee]).get(employee)
	req_skills = normalize_skill_requirements(required_skills)
	if req_skills and (not profile or not is_technician_qualified(profile, req_skills)):
		return {
			"is_available": False,
			"reason": "unqualified",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	target_date = normalize_date(date) or str(date)
	norm_start = normalize_time(start_time) or start_time
	holiday_fn = build_holiday_checker(
		settings.get("holiday_list"),
		add_days(getdate(target_date), -30),
	)
	span = calculate_working_span(target_date, norm_start, duration_minutes, is_holiday_fn=holiday_fn)
	seg_dates = [s["date"] for s in span["segments"]] or [target_date]
	emp_leaves = _get_leaves_by_employee([employee], min(seg_dates), max(seg_dates)).get(employee, [])

	if span["segments"]:
		for prop_seg in span["segments"]:
			seg_d = str(prop_seg["date"])
			seg_start = to_timedelta(prop_seg["start_time"])
			seg_end = to_timedelta(prop_seg["end_time"])
			base_shift = get_technician_shift_window(profile, seg_d, settings=settings)
			if not base_shift:
				return {
					"is_available": False,
					"reason": "off_shift",
					"on_leave": False,
					"half_day": False,
					"conflicts": [],
				}
			eff_shift = apply_leave_to_shift_window(base_shift, emp_leaves, seg_d)
			if not eff_shift:
				leave_res = _is_employee_on_leave_for_date(emp_leaves, seg_d)
				return {
					"is_available": False,
					"reason": "on_leave",
					"on_leave": True,
					"half_day": bool(leave_res.get("half_day")),
					"half_day_period": leave_res.get("half_day_period"),
					"conflicts": [],
				}
			if not is_interval_within_shift(seg_start, seg_end, eff_shift):
				if is_interval_within_shift(seg_start, seg_end, base_shift):
					leave_res = _is_employee_on_leave_for_date(emp_leaves, seg_d)
					return {
						"is_available": False,
						"reason": "on_leave",
						"on_leave": True,
						"half_day": bool(leave_res.get("half_day")),
						"half_day_period": leave_res.get("half_day_period"),
						"conflicts": [],
					}
				return {
					"is_available": False,
					"reason": "off_shift",
					"on_leave": False,
					"half_day": False,
					"conflicts": [],
				}
	else:
		for seg_d in dict.fromkeys(seg_dates):
			base_shift = get_technician_shift_window(profile, str(seg_d), settings=settings)
			if not base_shift:
				return {
					"is_available": False,
					"reason": "off_shift",
					"on_leave": False,
					"half_day": False,
					"conflicts": [],
				}
			eff_shift = apply_leave_to_shift_window(base_shift, emp_leaves, str(seg_d))
			if not eff_shift:
				leave_res = _is_employee_on_leave_for_date(emp_leaves, str(seg_d))
				return {
					"is_available": False,
					"reason": "on_leave",
					"on_leave": True,
					"half_day": bool(leave_res.get("half_day")),
					"half_day_period": leave_res.get("half_day_period"),
					"conflicts": [],
				}

	existing_entries = fetch_blocking_entries(
		min(seg_dates),
		max(seg_dates),
		assigned_technician=employee,
		exclude_entry=exclude_entry,
		fields=[
			"name",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
			"customer",
			"service_bay",
			"repair_order",
		],
	)

	conflicts = []
	for entry in existing_entries:
		ex_dur = entry.get("estimated_duration") or 0
		ex_span = calculate_working_span(
			entry.get("scheduled_date"),
			entry.get("scheduled_time"),
			ex_dur,
			is_holiday_fn=holiday_fn,
		)
		conflict_found = False
		for prop_seg in span["segments"]:
			p_date = prop_seg["date"]
			p_start = to_timedelta(prop_seg["start_time"])
			p_end = to_timedelta(prop_seg["end_time"])
			for ex_seg in ex_span["segments"]:
				if ex_seg["date"] == p_date:
					e_start = to_timedelta(ex_seg["start_time"])
					e_end = to_timedelta(ex_seg["end_time"])
					if intervals_overlap(p_start, p_end, e_start, e_end):
						conflicts.append({
							"name": entry.get("name"),
							"scheduled_date": str(entry.get("scheduled_date")),
							"scheduled_time": format_timedelta(to_timedelta(entry.get("scheduled_time"))),
							"estimated_duration": ex_dur,
							"effective_end_time": format_timedelta(to_timedelta(ex_span["completion_time"])),
							"customer": entry.get("customer"),
							"service_bay": entry.get("service_bay"),
							"repair_order": entry.get("repair_order"),
						})
						conflict_found = True
						break
			if conflict_found:
				break

	if conflicts:
		return {
			"is_available": False,
			"reason": "overlap",
			"on_leave": False,
			"half_day": False,
			"conflicts": conflicts,
		}

	return {
		"is_available": True,
		"reason": None,
		"on_leave": False,
		"half_day": False,
		"conflicts": [],
	}


def _build_queue_from_entries(
	employee: str,
	emp_name: str,
	on_leave: bool,
	entries: List[Dict[str, Any]],
	op_start: timedelta,
	op_end: timedelta,
	bs_td: Optional[timedelta],
	be_td: Optional[timedelta],
) -> Dict[str, Any]:
	"""Aggregate timeline metrics and utilization gaps from sorted Schedule Entry rows."""
	formatted_entries = []
	total_minutes = 0
	completed_minutes = 0
	remaining_minutes = 0
	active_job_count = 0
	gaps = []
	last_end_td = op_start

	for entry in entries:
		st_td = to_timedelta(entry.get("scheduled_time"))
		dur = entry.get("estimated_duration") or 0
		eff_end_td = (
			to_timedelta(entry["_day_end_time"])
			if entry.get("_day_end_time")
			else effective_end_time(st_td, dur, bs_td, be_td)
		)

		total_minutes += dur
		if entry.get("status") == "Completed":
			completed_minutes += dur
		else:
			remaining_minutes += dur
			active_job_count += 1

		if st_td > last_end_td:
			gap_dur = int((st_td - last_end_td).total_seconds() // 60)
			if bs_td and be_td and last_end_td <= bs_td and st_td >= be_td:
				gap_dur -= int((be_td - bs_td).total_seconds() // 60)
			if gap_dur > 0:
				gaps.append({
					"after": formatted_entries[-1]["name"] if formatted_entries else "start_of_day",
					"duration_minutes": gap_dur,
					"start": format_timedelta(last_end_td),
				})

		formatted_entries.append({
			"name": entry["name"],
			"scheduled_time": format_timedelta(st_td),
			"estimated_duration": dur,
			"effective_end_time": format_timedelta(eff_end_td),
			"service_bay": entry.get("service_bay"),
			"customer": entry.get("customer"),
			"repair_vehicle": entry.get("repair_vehicle"),
			"items_summary": entry.get("items_summary"),
			"status": entry.get("status"),
			"repair_order": entry.get("repair_order"),
		})

		last_end_td = max(last_end_td, eff_end_td)

	shop_op_minutes = int((op_end - op_start).total_seconds() // 60)
	if bs_td and be_td and be_td > bs_td:
		shop_op_minutes -= int((be_td - bs_td).total_seconds() // 60)

	shop_op_minutes = max(1, shop_op_minutes)
	utilization_pct = round((total_minutes / shop_op_minutes) * 100, 1)

	return {
		"employee": employee,
		"employee_name": emp_name,
		"on_leave": on_leave,
		"timeline": formatted_entries,
		"entries": formatted_entries,
		"total_jobs": len(formatted_entries),
		"active_job_count": active_job_count,
		"total_minutes": total_minutes,
		"completed_minutes": completed_minutes,
		"remaining_minutes": remaining_minutes,
		"utilization_percent": utilization_pct,
		"utilization_pct": utilization_pct,
		"gaps": gaps,
	}


@frappe.whitelist()
def get_technician_queue(employee: str, date: Union[str, date, datetime]) -> Dict[str, Any]:
	"""
	Returns personal work queue for a technician on a specific date.
	Calculates gaps between entries and utilization percentage.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if not employee or not isinstance(employee, str):
		frappe.throw(frappe._("Valid technician identifier is required."), frappe.ValidationError)
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")

	target_date = normalize_date(date) or str(date)
	emp_name = frappe.db.get_value("Employee", employee, "employee_name") or employee
	leave_res = check_employee_leave(employee, target_date, op_start, 0)
	holiday_fn = build_holiday_checker(
		settings.get("holiday_list"),
		add_days(getdate(target_date), -30),
	)
	entries = fetch_queue_entries_for_date(
		target_date,
		employee=employee,
		is_holiday_fn=holiday_fn,
	)

	return _build_queue_from_entries(
		employee=employee,
		emp_name=emp_name,
		on_leave=leave_res["on_leave"],
		entries=entries,
		op_start=op_start,
		op_end=op_end,
		bs_td=bs_td,
		be_td=be_td,
	)


@frappe.whitelist()
def get_daily_technician_overview(date: Union[str, date, datetime]) -> Dict[str, Any]:
	"""
	Shop-wide technician dashboard data for managers and overview reporting.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")

	target_date = normalize_date(date) or str(date)

	operating_minutes = int((op_end - op_start).total_seconds() // 60)
	if bs_td and be_td and be_td > bs_td:
		operating_minutes -= int((be_td - bs_td).total_seconds() // 60)
	operating_minutes = max(1, operating_minutes)

	active_techs = get_active_technicians(date=target_date)
	holiday_fn = build_holiday_checker(
		settings.get("holiday_list"),
		add_days(getdate(target_date), -30),
	)
	day_entries = fetch_queue_entries_for_date(target_date, is_holiday_fn=holiday_fn)

	entries_by_tech: Dict[str, List[Dict[str, Any]]] = {}
	unassigned = []
	for entry in day_entries:
		tech_id = entry.get("assigned_technician")
		if tech_id:
			entries_by_tech.setdefault(tech_id, []).append(entry)
		else:
			unassigned.append(entry)

	tech_summaries = []
	total_scheduled_minutes = 0
	total_remaining_minutes = 0
	total_completed_minutes = 0

	for tech in active_techs:
		emp_id = tech["employee"]
		queue = _build_queue_from_entries(
			employee=emp_id,
			emp_name=tech["employee_name"],
			on_leave=tech["on_leave"],
			entries=entries_by_tech.get(emp_id, ()),
			op_start=op_start,
			op_end=op_end,
			bs_td=bs_td,
			be_td=be_td,
		)

		tech_summaries.append({
			"employee": emp_id,
			"employee_name": tech["employee_name"],
			"on_leave": tech["on_leave"],
			"job_count": queue["total_jobs"],
			"active_job_count": queue["active_job_count"],
			"total_minutes": queue["total_minutes"],
			"completed_minutes": queue["completed_minutes"],
			"remaining_minutes": queue["remaining_minutes"],
			"utilization_percent": queue["utilization_percent"],
			"utilization_pct": queue["utilization_pct"],
		})
		total_scheduled_minutes += queue["total_minutes"]
		total_remaining_minutes += queue["remaining_minutes"]
		total_completed_minutes += queue["completed_minutes"]

	unassigned_formatted = []
	for u in unassigned:
		st_td = to_timedelta(u.get("scheduled_time"))
		unassigned_formatted.append({
			"name": u["name"],
			"scheduled_time": format_timedelta(st_td),
			"customer": u.get("customer"),
			"repair_vehicle": u.get("repair_vehicle"),
			"service_bay": u.get("service_bay"),
			"estimated_duration": u.get("estimated_duration") or 0,
			"repair_order": u.get("repair_order"),
		})

	total_bays = frappe.db.count("Service Bay", {"is_active": 1})
	on_duty_count = sum(1 for t in tech_summaries if not t["on_leave"])
	on_leave_count = sum(1 for t in tech_summaries if t["on_leave"])

	bottleneck = "technician" if on_duty_count < total_bays else ("bay" if total_bays < on_duty_count else None)
	total_hours = round(total_scheduled_minutes / 60.0, 1)
	active_hours = round(total_remaining_minutes / 60.0, 1)
	completed_hours = round(total_completed_minutes / 60.0, 1)

	shop_summary = {
		"total_technicians": len(active_techs),
		"on_duty": on_duty_count,
		"on_leave": on_leave_count,
		"total_bays": total_bays,
		"bottleneck": bottleneck,
		"total_scheduled_hours": total_hours,
		"active_scheduled_hours": active_hours,
		"completed_hours": completed_hours,
		"unassigned_count": len(unassigned_formatted),
	}

	return {
		"date": target_date,
		"operating_minutes": operating_minutes,
		"total_scheduled_hours": total_hours,
		"bottleneck": bottleneck,
		"technicians": tech_summaries,
		"unassigned_entries": unassigned_formatted,
		"shop_summary": shop_summary,
	}

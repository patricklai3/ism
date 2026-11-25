# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted REST endpoints for the Technician Assignment & Scheduling Matrix."""

from __future__ import annotations

import json
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt, today

from ism.api._validation import optional_str, require_int, require_str
from ism.permissions import is_manager_or_advisor
from ism.scheduling.fairness import (
	compute_fairness_ledger,
	load_active_technician_employee_ids,
)
from ism.scheduling.rebalancer import apply_rebalance_day, preview_rebalance_day
from ism.scheduling.shifts import DAY_NAMES
from ism.scheduling.skills import load_active_technician_profiles
from ism.scheduling.time_utils import (
	get_shop_settings,
	invalidate_shop_settings_cache,
	normalize_date,
	normalize_time,
)

_MATRIX_WRITE_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager"})
_VALID_POLICY_MODES: tuple[str, ...] = ("Tiered Waterfall", "Equal Balance", "Weighted Share")
_VALID_SKILL_ROLES: tuple[str, ...] = ("Preferred", "Standard", "Backup")
_VALID_DAYS_OF_WEEK: tuple[str, ...] = DAY_NAMES


def _assert_manager_or_advisor() -> None:
	"""Allow Administrator, System Manager, ISM Manager, or ISM Service Advisor; deny Guest & Technicians."""
	user = (getattr(frappe.session, "user", None) or "").strip()
	if not user or user == "Guest":
		frappe.throw(_("Authentication is required."), frappe.PermissionError)
	if not is_manager_or_advisor(user):
		frappe.throw(
			_("You do not have permission to access the Technician Assignment Matrix."),
			frappe.PermissionError,
		)


def _assert_matrix_manager() -> None:
	"""Allow Administrator, System Manager, or ISM Manager for matrix/policy mutations."""
	user = (getattr(frappe.session, "user", None) or "").strip()
	if not user or user == "Guest":
		frappe.throw(_("Authentication is required."), frappe.PermissionError)
	if user == "Administrator":
		return
	roles = set(frappe.get_roles(user) or [])
	if not (roles & _MATRIX_WRITE_ROLES):
		frappe.throw(
			_("Only ISM Manager or System Manager may modify the Technician Assignment Matrix or policy."),
			frappe.PermissionError,
		)


def _extract_policy_dict(shop: Optional[dict[str, Any]] = None) -> dict[str, Any]:
	"""Return all 9 frozen assignment policy settings keys."""
	s = shop if shop is not None else get_shop_settings()
	return {
		"assignment_policy_mode": str(s.get("assignment_policy_mode") or "Tiered Waterfall"),
		"fairness_window_days": cint(s.get("fairness_window_days") or 7),
		"tier_spillover_threshold_pct": flt(s.get("tier_spillover_threshold_pct", 85.0)),
		"fairness_weight": flt(s.get("fairness_weight", 0.55)),
		"skill_conservation_weight": flt(s.get("skill_conservation_weight", 0.25)),
		"skill_preference_weight": flt(s.get("skill_preference_weight", 0.12)),
		"schedule_packing_weight": flt(s.get("schedule_packing_weight", 0.08)),
		"enable_morning_auto_rebalance": 1 if cint(s.get("enable_morning_auto_rebalance", 0)) else 0,
		"auto_rebalance_skew_threshold_pct": flt(s.get("auto_rebalance_skew_threshold_pct", 25.0)),
	}


def _fmt_time_or_none(val: Any) -> Optional[str]:
	if val is None or str(val).strip() == "":
		return None
	return normalize_time(val)


def _format_weekly_schedule_rows(raw_schedule: Any) -> list[dict[str, Any]]:
	"""Serialize weekly_schedule map or child rows in canonical Monday..Sunday order."""
	by_day: dict[str, dict[str, Any]] = {}
	if isinstance(raw_schedule, dict):
		for day, row in raw_schedule.items():
			if isinstance(row, dict):
				by_day[str(day)] = row
	elif isinstance(raw_schedule, (list, tuple)):
		for row in raw_schedule:
			day = getattr(row, "day_of_week", None) if not isinstance(row, dict) else row.get("day_of_week")
			if day:
				if isinstance(row, dict):
					by_day[str(day)] = row
				else:
					by_day[str(day)] = {
						"day_of_week": row.day_of_week,
						"is_working_day": cint(getattr(row, "is_working_day", 1)),
						"shift_start": getattr(row, "shift_start", None),
						"shift_end": getattr(row, "shift_end", None),
						"break_start": getattr(row, "break_start", None),
						"break_end": getattr(row, "break_end", None),
					}

	ordered: list[dict[str, Any]] = []
	for day in _VALID_DAYS_OF_WEEK:
		row = by_day.get(day)
		if not row:
			continue
		ordered.append({
			"day_of_week": day,
			"is_working_day": 1 if cint(row.get("is_working_day", 1)) else 0,
			"shift_start": _fmt_time_or_none(row.get("shift_start")),
			"shift_end": _fmt_time_or_none(row.get("shift_end")),
			"break_start": _fmt_time_or_none(row.get("break_start")),
			"break_end": _fmt_time_or_none(row.get("break_end")),
		})
	return ordered


def _serialize_profile_summary(doc: Any) -> dict[str, Any]:
	raw_cap = getattr(doc, "daily_soft_cap_pct", None)
	return {
		"employee": doc.employee,
		"employee_name": doc.employee_name or doc.employee,
		"profile_name": doc.name,
		"is_active": 1 if cint(getattr(doc, "is_active", 1)) else 0,
		"assignment_tier": max(1, cint(getattr(doc, "assignment_tier", 1) or 1)),
		"target_load_weight": flt(getattr(doc, "target_load_weight", 1.0) or 1.0),
		"daily_soft_cap_pct": flt(raw_cap) if raw_cap not in (None, "") else None,
		"weekly_schedule": _format_weekly_schedule_rows(getattr(doc, "weekly_schedule", None) or []),
	}


def _parse_float_strict(value: Any, label: str) -> float:
	if isinstance(value, bool):
		frappe.throw(_("{0} must be a valid number.").format(label), frappe.ValidationError)
	try:
		return float(value)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be a valid number.").format(label), frappe.ValidationError)


def _get_or_init_profile(emp_id: str) -> tuple[Any, bool]:
	if not frappe.db.exists("Employee", emp_id):
		frappe.throw(_("Employee '{0}' does not exist.").format(emp_id), frappe.DoesNotExistError)

	if frappe.db.exists("Technician Profile", emp_id):
		return frappe.get_doc("Technician Profile", emp_id), False

	doc = frappe.get_doc({
		"doctype": "Technician Profile",
		"employee": emp_id,
		"is_active": 1,
		"assignment_tier": 1,
		"target_load_weight": 1.0,
	})
	return doc, True


@frappe.whitelist(methods=["GET"])
def get_assignment_matrix(target_date: Optional[str] = None) -> dict[str, Any]:
	"""Return the full Technician Assignment Matrix, policy settings, skills, and rolling fairness metrics."""
	_assert_manager_or_advisor()

	raw_date = optional_str(target_date, "target_date")
	iso_date = normalize_date(raw_date) if raw_date else today()
	shop = get_shop_settings()

	skills_raw = (
		frappe.get_all(
			"Technician Skill",
			filters={"is_active": 1},
			fields=["name", "scarcity_weight", "description"],
			order_by="name asc",
			ignore_permissions=True,
		)
		if frappe.db.exists("DocType", "Technician Skill")
		else []
	)
	skills = [
		{
			"name": row["name"],
			"scarcity_weight": flt(row.get("scarcity_weight") or 1.0),
			"description": row.get("description") or "",
		}
		for row in skills_raw
	]

	emp_ids = load_active_technician_employee_ids(settings=shop)
	emp_name_map: dict[str, str] = {}
	if emp_ids and frappe.db.exists("DocType", "Employee"):
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", emp_ids]},
			fields=["name", "employee_name"],
			ignore_permissions=True,
		):
			emp_name_map[str(row["name"])] = str(row.get("employee_name") or row["name"])

	profiles = load_active_technician_profiles(emp_ids) if emp_ids else {}
	ledger = compute_fairness_ledger(iso_date, employee_ids=emp_ids, settings=shop) if emp_ids else {}

	technicians: list[dict[str, Any]] = []
	for emp_id in emp_ids:
		profile = profiles.get(emp_id)
		f_row = ledger.get(emp_id) or {}
		emp_name = (profile.get("employee_name") if profile else None) or emp_name_map.get(emp_id) or emp_id

		cells: dict[str, dict[str, Any]] = {}
		if profile:
			for skill_name, comp in (profile.get("skills") or {}).items():
				cells[skill_name] = {
					"max_difficulty": cint(comp.get("max_difficulty") or 0),
					"skill_role": str(comp.get("skill_role") or "Standard"),
				}

		weekly_schedule = _format_weekly_schedule_rows(profile.get("weekly_schedule") if profile else {})

		technicians.append({
			"employee": emp_id,
			"employee_name": emp_name,
			"profile_name": profile["name"] if profile else None,
			"is_active": cint(profile.get("is_active", 1)) if profile else 1,
			"assignment_tier": cint(profile.get("assignment_tier") or 1) if profile else 1,
			"target_load_weight": flt(profile.get("target_load_weight") or 1.0) if profile else 1.0,
			"daily_soft_cap_pct": profile.get("daily_soft_cap_pct") if profile else None,
			"cells": cells,
			"weekly_schedule": weekly_schedule,
			"fairness": {
				"assigned_frt_hours": round(flt(f_row.get("assigned_frt_hours") or 0.0), 2),
				"available_shift_hours": round(flt(f_row.get("available_shift_hours") or 0.0), 2),
				"normalized_load": round(flt(f_row.get("normalized_load") or 0.0), 4),
				"target_day_util_pct": round(flt(f_row.get("target_day_util_pct") or 0.0), 1),
			},
		})

	technicians.sort(key=lambda t: (int(t["assignment_tier"]), str(t["employee_name"]).lower(), str(t["employee"])))

	return {
		"ok": True,
		"date": iso_date,
		"policy": _extract_policy_dict(shop),
		"skills": skills,
		"technicians": technicians,
	}


@frappe.whitelist(methods=["POST"])
def save_matrix_cell(
	employee: str,
	skill: str,
	max_difficulty: int,
	skill_role: str = "Standard",
) -> dict[str, Any]:
	"""Create or update a single (employee, skill) competency cell on Technician Profile."""
	_assert_matrix_manager()

	emp_id = require_str(employee, "employee")
	skill_name = require_str(skill, "skill")
	if not frappe.db.exists("Technician Skill", skill_name):
		frappe.throw(_("Technician Skill '{0}' does not exist.").format(skill_name), frappe.DoesNotExistError)

	diff = require_int(max_difficulty, "max_difficulty")
	if diff not in (0, 1, 2, 3):
		frappe.throw(
			_("max_difficulty must be an integer between 0 and 3."),
			frappe.ValidationError,
		)

	role = (optional_str(skill_role, "skill_role") or "Standard").strip()
	if role not in _VALID_SKILL_ROLES:
		frappe.throw(
			_("Invalid skill_role '{0}'. Expected one of: {1}.").format(role, ", ".join(_VALID_SKILL_ROLES)),
			frappe.ValidationError,
		)

	doc, is_new = _get_or_init_profile(emp_id)
	updated_existing = False
	for row in doc.skills or []:
		if (row.skill or "").strip() == skill_name:
			row.max_difficulty = diff
			row.skill_role = role
			updated_existing = True
			break

	if not updated_existing:
		doc.append("skills", {"skill": skill_name, "max_difficulty": diff, "skill_role": role})

	if is_new:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	return {
		"ok": True,
		"employee": emp_id,
		"profile_name": doc.name,
		"skill": skill_name,
		"cell": {
			"max_difficulty": diff,
			"skill_role": role,
		},
		"profile": _serialize_profile_summary(doc),
	}


@frappe.whitelist(methods=["POST"])
def update_technician_profile(
	employee: str,
	assignment_tier: Optional[int] = None,
	target_load_weight: Optional[float] = None,
	daily_soft_cap_pct: Optional[float] = None,
	weekly_schedule: Optional[Any] = None,
) -> dict[str, Any]:
	"""Update a technician's assignment tier, load weight, daily soft cap, and/or weekly shift schedule."""
	_assert_matrix_manager()

	emp_id = require_str(employee, "employee")
	doc, is_new = _get_or_init_profile(emp_id)

	if assignment_tier is not None and str(assignment_tier).strip() != "":
		tier = require_int(assignment_tier, "assignment_tier")
		if tier < 1:
			frappe.throw(_("assignment_tier must be 1 or greater."), frappe.ValidationError)
		doc.assignment_tier = tier

	if target_load_weight is not None and str(target_load_weight).strip() != "":
		weight = _parse_float_strict(target_load_weight, "target_load_weight")
		if weight <= 0:
			frappe.throw(_("target_load_weight must be greater than 0."), frappe.ValidationError)
		doc.target_load_weight = weight

	if daily_soft_cap_pct is not None:
		raw_cap_str = str(daily_soft_cap_pct).strip()
		if raw_cap_str == "" or raw_cap_str.lower() == "null":
			doc.daily_soft_cap_pct = None
		else:
			cap = _parse_float_strict(daily_soft_cap_pct, "daily_soft_cap_pct")
			if cap < 0 or cap > 100:
				frappe.throw(_("daily_soft_cap_pct must be between 0 and 100."), frappe.ValidationError)
			doc.daily_soft_cap_pct = cap

	if weekly_schedule is not None:
		parsed_schedule = weekly_schedule
		if isinstance(weekly_schedule, str):
			text = weekly_schedule.strip()
			if not text:
				parsed_schedule = []
			else:
				try:
					parsed_schedule = json.loads(text)
				except (TypeError, ValueError):
					frappe.throw(_("weekly_schedule must be valid JSON."), frappe.ValidationError)
		if not isinstance(parsed_schedule, list):
			frappe.throw(_("weekly_schedule must be a list of day schedule objects."), frappe.ValidationError)

		normalized_rows: list[dict[str, Any]] = []
		for idx, item in enumerate(parsed_schedule, start=1):
			if not isinstance(item, dict):
				frappe.throw(
					_("weekly_schedule entry #{0} must be an object.").format(idx),
					frappe.ValidationError,
				)
			day = str(item.get("day_of_week") or "").strip()
			if day not in _VALID_DAYS_OF_WEEK:
				frappe.throw(
					_("Invalid day_of_week '{0}' at weekly_schedule entry #{1}.").format(day, idx),
					frappe.ValidationError,
				)
			is_working = 1 if cint(item.get("is_working_day", 1)) else 0
			normalized_rows.append({
				"day_of_week": day,
				"is_working_day": is_working,
				"shift_start": _fmt_time_or_none(item.get("shift_start")) if is_working else None,
				"shift_end": _fmt_time_or_none(item.get("shift_end")) if is_working else None,
				"break_start": _fmt_time_or_none(item.get("break_start")) if is_working else None,
				"break_end": _fmt_time_or_none(item.get("break_end")) if is_working else None,
			})
		doc.set("weekly_schedule", normalized_rows)

	if is_new:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	return {
		"ok": True,
		"employee": emp_id,
		"profile": _serialize_profile_summary(doc),
	}


@frappe.whitelist(methods=["POST"])
def save_assignment_policy(
	assignment_policy_mode: Optional[str] = None,
	fairness_window_days: Optional[int] = None,
	tier_spillover_threshold_pct: Optional[float] = None,
	fairness_weight: Optional[float] = None,
	skill_conservation_weight: Optional[float] = None,
	skill_preference_weight: Optional[float] = None,
	schedule_packing_weight: Optional[float] = None,
	enable_morning_auto_rebalance: Optional[int] = None,
	auto_rebalance_skew_threshold_pct: Optional[float] = None,
) -> dict[str, Any]:
	"""Validate and persist Technician Assignment Policy fields onto ISM Settings."""
	_assert_matrix_manager()

	doc = frappe.get_single("ISM Settings")

	if assignment_policy_mode is not None and str(assignment_policy_mode).strip() != "":
		mode = str(assignment_policy_mode).strip()
		if mode not in _VALID_POLICY_MODES:
			frappe.throw(
				_("Invalid assignment_policy_mode '{0}'. Expected one of: {1}.").format(
					mode, ", ".join(_VALID_POLICY_MODES)
				),
				frappe.ValidationError,
			)
		doc.assignment_policy_mode = mode

	if fairness_window_days is not None and str(fairness_window_days).strip() != "":
		window = require_int(fairness_window_days, "fairness_window_days")
		if window < 1 or window > 30:
			frappe.throw(_("fairness_window_days must be between 1 and 30."), frappe.ValidationError)
		doc.fairness_window_days = window

	if tier_spillover_threshold_pct is not None and str(tier_spillover_threshold_pct).strip() != "":
		spillover = _parse_float_strict(tier_spillover_threshold_pct, "tier_spillover_threshold_pct")
		if spillover < 0 or spillover > 100:
			frappe.throw(_("tier_spillover_threshold_pct must be between 0 and 100."), frappe.ValidationError)
		doc.tier_spillover_threshold_pct = spillover

	for field_name, raw_val in (
		("fairness_weight", fairness_weight),
		("skill_conservation_weight", skill_conservation_weight),
		("skill_preference_weight", skill_preference_weight),
		("schedule_packing_weight", schedule_packing_weight),
	):
		if raw_val is not None and str(raw_val).strip() != "":
			w_val = _parse_float_strict(raw_val, field_name)
			if w_val < 0:
				frappe.throw(_("{0} must be 0 or greater.").format(field_name), frappe.ValidationError)
			setattr(doc, field_name, w_val)

	if enable_morning_auto_rebalance is not None and str(enable_morning_auto_rebalance).strip() != "":
		if isinstance(enable_morning_auto_rebalance, bool):
			rebal_flag = 1 if enable_morning_auto_rebalance else 0
		else:
			rebal_flag = require_int(enable_morning_auto_rebalance, "enable_morning_auto_rebalance")
			if rebal_flag not in (0, 1):
				frappe.throw(_("enable_morning_auto_rebalance must be 0 or 1."), frappe.ValidationError)
		doc.enable_morning_auto_rebalance = rebal_flag

	if auto_rebalance_skew_threshold_pct is not None and str(auto_rebalance_skew_threshold_pct).strip() != "":
		skew = _parse_float_strict(auto_rebalance_skew_threshold_pct, "auto_rebalance_skew_threshold_pct")
		if skew < 0 or skew > 100:
			frappe.throw(
				_("auto_rebalance_skew_threshold_pct must be between 0 and 100."),
				frappe.ValidationError,
			)
		doc.auto_rebalance_skew_threshold_pct = skew

	doc.save(ignore_permissions=True)
	invalidate_shop_settings_cache()

	return {
		"ok": True,
		"policy": _extract_policy_dict(),
	}


@frappe.whitelist(methods=["GET"])
def preview_rebalance(target_date: str) -> dict[str, Any]:
	"""Dry-run rebalance of unlocked future/today Schedule Entries on target_date."""
	_assert_manager_or_advisor()

	iso_date = normalize_date(require_str(target_date, "target_date"))
	preview = preview_rebalance_day(iso_date)
	return {"ok": True, **preview}


@frappe.whitelist(methods=["POST"])
def apply_rebalance(
	target_date: str,
	entry_names: Optional[Any] = None,
) -> dict[str, Any]:
	"""Apply rebalanced technician assignments on target_date and broadcast calendar refresh."""
	_assert_manager_or_advisor()

	iso_date = normalize_date(require_str(target_date, "target_date"))
	parsed_names: Optional[list[str]] = None
	if entry_names is not None:
		if isinstance(entry_names, str):
			text = entry_names.strip()
			if text:
				try:
					decoded = json.loads(text)
					if isinstance(decoded, list):
						parsed_names = [str(n).strip() for n in decoded if str(n or "").strip()]
					else:
						frappe.throw(_("entry_names JSON must be a list."), frappe.ValidationError)
				except (TypeError, ValueError):
					parsed_names = [n.strip() for n in text.split(",") if n.strip()]
		elif isinstance(entry_names, (list, tuple, set)):
			parsed_names = [str(n).strip() for n in entry_names if str(n or "").strip()]
		else:
			frappe.throw(_("entry_names must be a list of Schedule Entry names."), frappe.ValidationError)

	result = apply_rebalance_day(iso_date, entry_names=parsed_names)

	try:
		frappe.publish_realtime(
			"ism_calendar_update",
			{
				"date": iso_date,
				"rebalanced": True,
				"applied_count": result.get("applied_count", 0),
			},
		)
	except Exception:
		pass

	return {"ok": True, **result}


@frappe.whitelist(methods=["POST"])
def toggle_assignment_lock(schedule_entry: str, locked: int) -> dict[str, Any]:
	"""Toggle Schedule Entry.assignment_locked (0 or 1) for a Manager or Service Advisor."""
	_assert_manager_or_advisor()

	se_name = require_str(schedule_entry, "schedule_entry")
	if not frappe.db.exists("Schedule Entry", se_name):
		frappe.throw(_("Schedule Entry '{0}' does not exist.").format(se_name), frappe.DoesNotExistError)

	if isinstance(locked, bool):
		locked_int = 1 if locked else 0
	else:
		locked_int = require_int(locked, "locked")
		if locked_int not in (0, 1):
			frappe.throw(_("locked must be 0 or 1."), frappe.ValidationError)

	doc = frappe.get_doc("Schedule Entry", se_name)
	doc.assignment_locked = locked_int
	doc.flags.ignore_validate = True
	doc.save(ignore_permissions=True)

	try:
		frappe.publish_realtime(
			"ism_calendar_update",
			{
				"schedule_entry": se_name,
				"bay": doc.service_bay,
				"date": str(doc.scheduled_date) if doc.scheduled_date else None,
			},
		)
	except Exception:
		pass

	return {
		"ok": True,
		"schedule_entry": se_name,
		"assignment_locked": locked_int,
	}

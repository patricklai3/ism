# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Skill requirement resolution and technician qualification helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Optional

import frappe
from frappe.utils import cint, flt

SkillRequirement = tuple[str, int]

_ROLE_RANK: dict[str, int] = {
	"Preferred": 0,
	"Standard": 1,
	"Backup": 2,
}


def _clamp_difficulty(raw_val: Any, default: int = 1) -> int:
	if raw_val is None or raw_val == "":
		return default
	val = cint(raw_val)
	if val < 1:
		return 1
	if val > 3:
		return 3
	return val


def _extract_attr(obj: Any, fieldname: str) -> Any:
	if isinstance(obj, Mapping):
		return obj.get(fieldname)
	return getattr(obj, fieldname, None)


def normalize_skill_requirements(raw: Any) -> list[tuple[str, int]]:
	"""Normalize skill requirements into a sorted, deduplicated list of (skill_name, difficulty_1_to_3).

	Duplicate skills are merged by taking max(difficulty_level).
	"""
	if raw is None or raw == "":
		return []

	if isinstance(raw, str):
		text = raw.strip()
		if not text:
			return []
		if text[0] in ("[", "{"):
			try:
				parsed = json.loads(text)
			except Exception:
				return []
			return normalize_skill_requirements(parsed)
		return [(text, 1)]

	merged: dict[str, int] = {}

	def _record(skill_name: Any, diff: Any = 1) -> None:
		name = str(skill_name or "").strip()
		if not name:
			return
		level = _clamp_difficulty(diff, default=1)
		prev = merged.get(name, 0)
		if level > prev:
			merged[name] = level

	if isinstance(raw, Mapping):
		if "skill" in raw:
			_record(
				raw.get("skill"),
				raw.get("difficulty_level")
				if raw.get("difficulty_level") is not None
				else raw.get("difficulty", raw.get("level", 1)),
			)
		else:
			for k, v in raw.items():
				if isinstance(v, Mapping):
					_record(k, v.get("difficulty_level", v.get("difficulty", 1)))
				else:
					_record(k, v)
		return sorted(merged.items(), key=lambda pair: pair[0])

	if isinstance(raw, (list, tuple, set)):
		for item in raw:
			if item is None:
				continue
			if isinstance(item, str):
				_record(item, 1)
			elif isinstance(item, (list, tuple)):
				if len(item) >= 2:
					_record(item[0], item[1])
				elif len(item) == 1:
					_record(item[0], 1)
			elif isinstance(item, Mapping):
				skill_val = item.get("skill") or item.get("required_skill") or item.get("skill_name")
				diff_val = (
					item.get("difficulty_level")
					if item.get("difficulty_level") is not None
					else item.get("difficulty", item.get("level", 1))
				)
				_record(skill_val, diff_val)
			else:
				skill_val = (
					getattr(item, "skill", None)
					or getattr(item, "required_skill", None)
					or getattr(item, "skill_name", None)
				)
				diff_val = getattr(item, "difficulty_level", None)
				if diff_val is None:
					diff_val = getattr(item, "difficulty", 1)
				_record(skill_val, diff_val)
		return sorted(merged.items(), key=lambda pair: pair[0])

	skill_val = getattr(raw, "skill", None) or getattr(raw, "required_skill", None)
	if skill_val:
		diff_val = getattr(raw, "difficulty_level", 1)
		_record(skill_val, diff_val)
		return sorted(merged.items(), key=lambda pair: pair[0])

	return []


def load_package_skills_map(package_names: Sequence[str]) -> dict[str, list[tuple[str, int]]]:
	"""Batch-load required skills for the given Service Package names."""
	unique = list(dict.fromkeys(str(p).strip() for p in (package_names or ()) if str(p or "").strip()))
	result: dict[str, list[tuple[str, int]]] = {pkg: [] for pkg in unique}
	if not unique or not frappe.db.exists("DocType", "Service Package Skill"):
		return result

	rows = frappe.get_all(
		"Service Package Skill",
		filters={
			"parent": ["in", unique],
			"parenttype": "Service Package",
		},
		fields=["parent", "skill", "difficulty_level"],
		ignore_permissions=True,
	)

	grouped: dict[str, list[tuple[str, int]]] = {pkg: [] for pkg in unique}
	for row in rows:
		parent = row.get("parent")
		skill = (row.get("skill") or "").strip()
		if parent and skill:
			grouped.setdefault(parent, []).append((skill, _clamp_difficulty(row.get("difficulty_level"), 1)))

	for pkg, pairs in grouped.items():
		result[pkg] = normalize_skill_requirements(pairs)

	return result


def get_required_skills_for_packages(package_names: Sequence[str]) -> list[tuple[str, int]]:
	"""Return the union of required skills across Service Packages, taking max difficulty per skill."""
	pkg_map = load_package_skills_map(package_names)
	combined: list[tuple[str, int]] = []
	for pairs in pkg_map.values():
		combined.extend(pairs)
	return normalize_skill_requirements(combined)


def _extract_ro_service_rows(
	repair_order: Any,
	selected_service_names: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
	if not repair_order:
		return []

	selected_set = (
		{str(n).strip() for n in selected_service_names if str(n or "").strip()}
		if selected_service_names is not None
		else None
	)

	if isinstance(repair_order, str):
		ro_name = repair_order.strip()
		if not ro_name or not frappe.db.exists("DocType", "Repair Order Service"):
			return []
		fields = ["name", "service_package", "status"]
		if frappe.db.has_column("Repair Order Service", "required_skill"):
			fields.append("required_skill")
		if frappe.db.has_column("Repair Order Service", "difficulty_level"):
			fields.append("difficulty_level")
		rows = frappe.get_all(
			"Repair Order Service",
			filters={"parent": ro_name, "parenttype": "Repair Order"},
			fields=fields,
			ignore_permissions=True,
		)
	else:
		raw_services = _extract_attr(repair_order, "services") or []
		rows = [
			{
				"name": _extract_attr(r, "name"),
				"service_package": _extract_attr(r, "service_package"),
				"status": _extract_attr(r, "status"),
				"required_skill": _extract_attr(r, "required_skill"),
				"difficulty_level": _extract_attr(r, "difficulty_level"),
			}
			for r in raw_services
		]

	filtered: list[dict[str, Any]] = []
	for row in rows:
		if (row.get("status") or "").strip() == "Declined":
			continue
		if selected_set is not None and row.get("name") not in selected_set:
			continue
		filtered.append(row)
	return filtered


def get_required_skills_for_repair_order(
	repair_order: Any,
	selected_service_names: Optional[Sequence[str]] = None,
) -> list[tuple[str, int]]:
	"""Resolve required (skill, difficulty) union for a Repair Order (or selected service rows)."""
	rows = _extract_ro_service_rows(repair_order, selected_service_names=selected_service_names)
	if not rows:
		return []

	collected: list[tuple[str, int]] = []
	fallback_packages: list[str] = []
	for row in rows:
		req_skill = (row.get("required_skill") or "").strip()
		if req_skill:
			collected.append((req_skill, _clamp_difficulty(row.get("difficulty_level"), 1)))
		elif row.get("service_package"):
			fallback_packages.append(str(row["service_package"]).strip())

	if fallback_packages:
		pkg_map = load_package_skills_map(fallback_packages)
		for pkg in fallback_packages:
			collected.extend(pkg_map.get(pkg, ()))

	return normalize_skill_requirements(collected)


def _extract_package_names_from_json(raw_json: Any) -> list[str]:
	if not raw_json:
		return []
	parsed = raw_json
	if isinstance(raw_json, str):
		text = raw_json.strip()
		if not text:
			return []
		try:
			parsed = json.loads(text)
		except Exception:
			return []
	if not isinstance(parsed, list):
		return []
	names: list[str] = []
	for item in parsed:
		if isinstance(item, str) and item.strip():
			names.append(item.strip())
		elif isinstance(item, Mapping):
			pkg = item.get("name") or item.get("service_package") or item.get("package_name")
			if pkg and str(pkg).strip():
				names.append(str(pkg).strip())
	return names


def get_required_skills_for_schedule_entry(entry: Any) -> list[tuple[str, int]]:
	"""Resolve required (skill, difficulty) union for a Schedule Entry doc, dict, or name."""
	if not entry:
		return []

	if isinstance(entry, str):
		entry_name = entry.strip()
		if not entry_name or not frappe.db.exists("Schedule Entry", entry_name):
			return []
		fields = ["name", "entry_type", "repair_order", "booked_packages_json"]
		if frappe.db.has_column("Schedule Entry", "required_skills_json"):
			fields.append("required_skills_json")
		entry = frappe.db.get_value("Schedule Entry", entry_name, fields, as_dict=True) or {}

	raw_snapshot = _extract_attr(entry, "required_skills_json")
	if raw_snapshot:
		snapshot_reqs = normalize_skill_requirements(raw_snapshot)
		if snapshot_reqs:
			return snapshot_reqs

	ro_name = (_extract_attr(entry, "repair_order") or "").strip()
	se_name = (_extract_attr(entry, "name") or "").strip()
	if ro_name:
		linked_service_names: Optional[list[str]] = None
		if (
			se_name
			and frappe.db.exists("DocType", "Repair Order Service")
			and frappe.db.has_column("Repair Order Service", "schedule_entry")
		):
			linked_rows = frappe.get_all(
				"Repair Order Service",
				filters={"parent": ro_name, "parenttype": "Repair Order", "schedule_entry": se_name},
				pluck="name",
				ignore_permissions=True,
			)
			if linked_rows:
				linked_service_names = linked_rows
		ro_reqs = get_required_skills_for_repair_order(ro_name, selected_service_names=linked_service_names)
		if ro_reqs:
			return ro_reqs

	booked_packages = _extract_package_names_from_json(_extract_attr(entry, "booked_packages_json"))
	if booked_packages:
		pkg_reqs = get_required_skills_for_packages(booked_packages)
		if pkg_reqs:
			return pkg_reqs

	entry_type = (_extract_attr(entry, "entry_type") or "").strip()
	if (
		entry_type
		and frappe.db.exists("DocType", "Schedule Entry Type")
		and frappe.db.has_column("Schedule Entry Type", "default_skill")
	):
		et_row = frappe.db.get_value(
			"Schedule Entry Type",
			entry_type,
			["default_skill", "default_difficulty_level"],
			as_dict=True,
		)
		if et_row and (et_row.get("default_skill") or "").strip():
			return normalize_skill_requirements(
				[(et_row["default_skill"], _clamp_difficulty(et_row.get("default_difficulty_level"), 1))]
			)

	return []


def load_active_technician_profiles(employee_ids: Optional[Sequence[str]] = None) -> dict[str, dict[str, Any]]:
	"""Batch-load active Technician Profile records with skills and weekly_schedule maps."""
	if not frappe.db.exists("DocType", "Technician Profile"):
		return {}

	filters: dict[str, Any] = {"is_active": 1}
	if employee_ids is not None:
		emp_list = list(dict.fromkeys(str(e).strip() for e in employee_ids if str(e or "").strip()))
		if not emp_list:
			return {}
		filters["employee"] = ["in", emp_list]

	profile_rows = frappe.get_all(
		"Technician Profile",
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"is_active",
			"assignment_tier",
			"target_load_weight",
			"daily_soft_cap_pct",
		],
		ignore_permissions=True,
	)
	if not profile_rows:
		return {}

	profile_names = [row["name"] for row in profile_rows]
	parent_to_emp = {row["name"]: row["employee"] for row in profile_rows}

	profiles: dict[str, dict[str, Any]] = {}
	for row in profile_rows:
		emp_id = row["employee"]
		raw_cap = row.get("daily_soft_cap_pct")
		cap_val = flt(raw_cap) if raw_cap not in (None, "") else None
		profiles[emp_id] = {
			"name": row["name"],
			"employee": emp_id,
			"employee_name": row.get("employee_name") or "",
			"is_active": cint(row.get("is_active", 1)),
			"assignment_tier": max(1, cint(row.get("assignment_tier") or 1)),
			"target_load_weight": max(0.01, flt(row.get("target_load_weight") or 1.0)),
			"daily_soft_cap_pct": cap_val,
			"skills": {},
			"weekly_schedule": {},
		}

	if frappe.db.exists("DocType", "Technician Skill Competency"):
		comp_rows = frappe.get_all(
			"Technician Skill Competency",
			filters={"parent": ["in", profile_names], "parenttype": "Technician Profile"},
			fields=["parent", "skill", "max_difficulty", "skill_role"],
			ignore_permissions=True,
		)
		for comp in comp_rows:
			emp_id = parent_to_emp.get(comp.get("parent"))
			skill_name = (comp.get("skill") or "").strip()
			if emp_id and skill_name and emp_id in profiles:
				profiles[emp_id]["skills"][skill_name] = {
					"skill": skill_name,
					"max_difficulty": cint(comp.get("max_difficulty") or 0),
					"skill_role": (comp.get("skill_role") or "Standard").strip() or "Standard",
				}

	if frappe.db.exists("DocType", "Technician Shift Day"):
		shift_rows = frappe.get_all(
			"Technician Shift Day",
			filters={"parent": ["in", profile_names], "parenttype": "Technician Profile"},
			fields=[
				"parent",
				"day_of_week",
				"is_working_day",
				"shift_start",
				"shift_end",
				"break_start",
				"break_end",
			],
			ignore_permissions=True,
		)
		for shift in shift_rows:
			emp_id = parent_to_emp.get(shift.get("parent"))
			day = (shift.get("day_of_week") or "").strip()
			if emp_id and day and emp_id in profiles:
				profiles[emp_id]["weekly_schedule"][day] = {
					"day_of_week": day,
					"is_working_day": cint(shift.get("is_working_day", 1)),
					"shift_start": shift.get("shift_start"),
					"shift_end": shift.get("shift_end"),
					"break_start": shift.get("break_start"),
					"break_end": shift.get("break_end"),
				}

	return profiles


def _get_profile_skills_map(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
	raw_skills = profile.get("skills")
	if not raw_skills:
		return {}
	if isinstance(raw_skills, Mapping):
		return {str(k).strip(): dict(v) for k, v in raw_skills.items() if str(k or "").strip()}
	result: dict[str, dict[str, Any]] = {}
	for row in raw_skills:
		skill_name = (_extract_attr(row, "skill") or "").strip()
		if skill_name:
			result[skill_name] = {
				"skill": skill_name,
				"max_difficulty": cint(_extract_attr(row, "max_difficulty") or 0),
				"skill_role": (_extract_attr(row, "skill_role") or "Standard").strip() or "Standard",
			}
	return result


def is_technician_qualified(profile: dict[str, Any], requirements: Sequence[tuple[str, int]]) -> bool:
	"""Return True iff the technician profile is active and satisfies every (skill, difficulty) requirement."""
	if not profile or not cint(profile.get("is_active", 1)):
		return False

	reqs = normalize_skill_requirements(requirements)
	if not reqs:
		return True

	skills_map = _get_profile_skills_map(profile)
	for skill_name, req_diff in reqs:
		comp = skills_map.get(skill_name)
		if not comp:
			return False
		max_diff = cint(comp.get("max_difficulty") or 0)
		if max_diff <= 0 or max_diff < req_diff:
			return False
	return True


def worst_skill_role(profile: dict[str, Any], requirements: Sequence[tuple[str, int]]) -> Optional[str]:
	"""Return the worst skill_role ('Preferred' < 'Standard' < 'Backup') across requirements, or None if unqualified."""
	if not is_technician_qualified(profile, requirements):
		return None

	reqs = normalize_skill_requirements(requirements)
	if not reqs:
		return "Standard"

	skills_map = _get_profile_skills_map(profile)
	worst_role = "Preferred"
	worst_rank = -1
	for skill_name, _req_diff in reqs:
		comp = skills_map.get(skill_name) or {}
		role = (comp.get("skill_role") or "Standard").strip()
		if role not in _ROLE_RANK:
			role = "Standard"
		rank = _ROLE_RANK[role]
		if rank > worst_rank:
			worst_rank = rank
			worst_role = role
	return worst_role

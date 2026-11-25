# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""4-stage deterministic technician assignment engine and multi-service RO splitter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from itertools import combinations
from typing import Any, Optional, Union

import frappe
from frappe.utils import cint, flt, getdate

from ism.scheduling.capacity import INACTIVE_WORK_STATUSES, NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.entry_queries import (
	build_holiday_checker,
	entry_datetimes,
	fetch_blocking_entries,
	intervals_overlap,
	span_datetimes,
)
from ism.scheduling.estimation_light import DEFAULT_SIGMA, estimate_duration, get_sigma_for_item
from ism.scheduling.fairness import (
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
	get_required_skills_for_repair_order,
	get_required_skills_for_schedule_entry,
	is_technician_qualified,
	load_active_technician_profiles,
	load_package_skills_map,
	normalize_skill_requirements,
	worst_skill_role,
)
from ism.scheduling.time_utils import (
	calculate_working_span,
	effective_end_time,
	get_shop_settings,
	normalize_date,
	normalize_time,
	to_timedelta,
)

_TIER_LABELS: dict[int, str] = {
	1: "Workhorse",
	2: "Buffer",
	3: "Reserve",
}


def _tier_label(tier: int) -> str:
	return _TIER_LABELS.get(tier, f"Tier {tier}")


def _load_skill_scarcity_weights(all_profiles: Mapping[str, dict[str, Any]]) -> dict[str, float]:
	"""Combine Technician Skill.scarcity_weight with dynamic inverse-holder scarcity across active profiles."""
	db_weights: dict[str, float] = {}
	if frappe.db.exists("DocType", "Technician Skill"):
		rows = frappe.get_all(
			"Technician Skill",
			fields=["name", "scarcity_weight"],
			ignore_permissions=True,
		)
		for row in rows:
			name = (row.get("name") or "").strip()
			if name:
				db_weights[name] = max(0.1, flt(row.get("scarcity_weight") or 1.0))

	holder_counts: dict[str, int] = {}
	for prof in all_profiles.values():
		for skill_name, comp in (prof.get("skills") or {}).items():
			if cint(comp.get("max_difficulty") or 0) > 0:
				holder_counts[skill_name] = holder_counts.get(skill_name, 0) + 1

	total_techs = max(1, len(all_profiles))
	all_skill_names = set(db_weights.keys()) | set(holder_counts.keys())
	weights: dict[str, float] = {}
	for skill_name in all_skill_names:
		base = db_weights.get(skill_name, 1.0)
		dynamic = total_techs / max(1, holder_counts.get(skill_name, 0))
		weights[skill_name] = round(base * dynamic, 4)
	return weights


def _resolve_sticky_candidates(
	*,
	repair_order: Optional[str],
	is_comeback: bool,
	original_technician: Optional[str],
	exclude_entry: Optional[str],
) -> list[tuple[str, str, str]]:
	"""Return ordered sticky continuity candidates as [(sticky_type, label, employee_id), ...].

	Priority:
	1. Comeback / Rework (is_comeback + original_technician)
	2. Parts-Hold Resume (RO on/after Waiting on Parts or prior non-Diagnostic repair work)
	3. Diagnostic -> Repair follow-up (prior Completed/In Progress Diagnostic Schedule Entry)
	"""
	candidates: list[tuple[str, str, str]] = []
	seen_techs: set[str] = set()

	def _add(sticky_type: str, label: str, emp_id: Optional[str]) -> None:
		clean = (emp_id or "").strip()
		if clean and clean not in seen_techs:
			seen_techs.add(clean)
			candidates.append((sticky_type, label, clean))

	if is_comeback and original_technician:
		_add("comeback", "Comeback Rework", original_technician)

	ro_name = (repair_order or "").strip()
	if not ro_name or not frappe.db.exists("Repair Order", ro_name):
		return candidates

	ro_fields = ["name", "status"]
	if frappe.db.has_column("Repair Order", "manual_parts_hold"):
		ro_fields.append("manual_parts_hold")
	ro_meta = frappe.db.get_value("Repair Order", ro_name, ro_fields, as_dict=True) or {}

	ros_rows: list[dict[str, Any]] = []
	if frappe.db.exists("DocType", "Repair Order Service"):
		ros_fields = ["name", "status", "assigned_technician"]
		if frappe.db.has_column("Repair Order Service", "is_comeback"):
			ros_fields.append("is_comeback")
		if frappe.db.has_column("Repair Order Service", "original_technician"):
			ros_fields.append("original_technician")
		if frappe.db.has_column("Repair Order Service", "schedule_entry"):
			ros_fields.append("schedule_entry")
		ros_rows = frappe.get_all(
			"Repair Order Service",
			filters={"parent": ro_name, "parenttype": "Repair Order"},
			fields=ros_fields,
			order_by="idx asc",
			ignore_permissions=True,
		)
		ros_rows = [r for r in ros_rows if (r.get("status") or "").strip() != "Declined"]

	# (a) Comeback from RO service rows if not explicitly passed
	for row in ros_rows:
		if cint(row.get("is_comeback") or 0) and (row.get("original_technician") or "").strip():
			_add("comeback", "Comeback Rework", row.get("original_technician"))

	se_filters: dict[str, Any] = {
		"repair_order": ro_name,
		"status": ["not in", list(INACTIVE_WORK_STATUSES)],
	}
	if exclude_entry:
		se_filters["name"] = ["!=", exclude_entry]
	prior_entries = frappe.get_all(
		"Schedule Entry",
		filters=se_filters,
		fields=["name", "entry_type", "status", "assigned_technician", "scheduled_date", "scheduled_time", "modified"],
		order_by="scheduled_date desc, scheduled_time desc, modified desc",
		ignore_permissions=True,
	)

	ro_status = (ro_meta.get("status") or "").strip()
	is_parts_hold = ro_status == "Waiting on Parts" or cint(ro_meta.get("manual_parts_hold") or 0) == 1

	prior_diag_entries = [
		e
		for e in prior_entries
		if (e.get("entry_type") or "").strip() == "Diagnostic"
		and (e.get("status") or "").strip() in ("Completed", "In Progress")
		and (e.get("assigned_technician") or "").strip()
	]
	prior_non_diag_work = [
		e
		for e in prior_entries
		if (e.get("entry_type") or "").strip() != "Diagnostic"
		and (e.get("status") or "").strip() in ("Completed", "In Progress")
		and (e.get("assigned_technician") or "").strip()
	]
	ros_techs = [
		(r.get("assigned_technician") or "").strip()
		for r in ros_rows
		if (r.get("assigned_technician") or "").strip()
	]

	# (b) Parts-Hold Resume / prior repair work continuity
	if is_parts_hold:
		for e in prior_entries:
			if (e.get("assigned_technician") or "").strip():
				_add("parts_hold_resume", "Parts-Hold Resume", e.get("assigned_technician"))
				break
		if ros_techs:
			_add("parts_hold_resume", "Parts-Hold Resume", ros_techs[0])
	elif prior_non_diag_work:
		_add("parts_hold_resume", "Parts-Hold Resume", prior_non_diag_work[0].get("assigned_technician"))
	elif ros_techs and not prior_diag_entries:
		_add("parts_hold_resume", "Parts-Hold Resume", ros_techs[0])

	# (c) Diagnostic -> Repair follow-up
	if prior_diag_entries:
		_add("diagnostic_followup", "Diagnostic -> Repair", prior_diag_entries[0].get("assigned_technician"))

	return candidates


def _compute_overqualification_raw(
	profile: dict[str, Any],
	reqs: Sequence[tuple[str, int]],
	skill_weights: Mapping[str, float],
) -> float:
	"""Measure capability headroom: required-skill difficulty surplus + unused scarce high-level skills."""
	skills_map = profile.get("skills") or {}
	req_map = dict(reqs)
	score = 0.0

	for skill_name, req_diff in reqs:
		comp = skills_map.get(skill_name) or {}
		max_diff = cint(comp.get("max_difficulty") or 0)
		surplus = max(0, max_diff - req_diff)
		if surplus > 0:
			score += surplus * flt(skill_weights.get(skill_name, 1.0))

	for skill_name, comp in skills_map.items():
		if skill_name in req_map:
			continue
		max_diff = cint(comp.get("max_difficulty") or 0)
		if max_diff > 0:
			# Weight higher difficulty levels nonlinearly so L3 specialists are conserved over generalists.
			score += (max_diff ** 1.35) * flt(skill_weights.get(skill_name, 1.0))

	return round(score, 6)


def _compute_preference_cost(
	profile: dict[str, Any],
	reqs: Sequence[tuple[str, int]],
) -> float:
	"""Return 0.0 if all required skills are Preferred, 0.5 if mixed, 1.0 if all Standard/Backup."""
	if not reqs:
		return 0.0
	skills_map = profile.get("skills") or {}
	roles = [((skills_map.get(skill) or {}).get("skill_role") or "Standard").strip() for skill, _ in reqs]
	if all(r == "Preferred" for r in roles):
		return 0.0
	if any(r == "Preferred" for r in roles):
		return 0.5
	return 1.0


def _net_gap_minutes(
	gap_start: timedelta,
	gap_end: timedelta,
	break_start: Optional[timedelta],
	break_end: Optional[timedelta],
) -> float:
	"""Gap duration in minutes between two intervals, excluding any embedded break window."""
	if gap_end <= gap_start:
		return 0.0
	gap_secs = (gap_end - gap_start).total_seconds()
	if break_start is not None and break_end is not None and break_end > break_start:
		ov_start = max(gap_start, break_start)
		ov_end = min(gap_end, break_end)
		if ov_end > ov_start:
			gap_secs -= (ov_end - ov_start).total_seconds()
	return max(0.0, gap_secs / 60.0)


def _compute_packing_and_waiter_cost(
	*,
	iso_date: str,
	cand_start_td: timedelta,
	cand_end_td: timedelta,
	eff_shift: Optional[dict[str, Any]],
	tech_day_entries: Sequence[dict[str, Any]],
	waiter_preference: str,
) -> tuple[float, bool]:
	"""Return (pack_cost in [0.0, 1.0], waiter_spillover_risk).

	Penalizes 15-30m orphan gaps adjacent to existing blocks, and when waiter_preference == 'Waiter',
	strongly penalizes slots immediately following an entry whose P80 end time overlaps start_time.
	"""
	if not tech_day_entries:
		return 0.0, False

	bs = eff_shift.get("break_start") if eff_shift else None
	be = eff_shift.get("break_end") if eff_shift else None
	target_d = getdate(iso_date)

	prev_entry: Optional[dict[str, Any]] = None
	prev_end_td: Optional[timedelta] = None
	next_start_td: Optional[timedelta] = None

	for item in tech_day_entries:
		ex_start_dt: datetime = item["start_dt"]
		ex_end_dt: datetime = item["end_dt"]

		# Project entry bounds onto iso_date
		if ex_end_dt.date() == target_d:
			item_end_td = timedelta(
				hours=ex_end_dt.hour, minutes=ex_end_dt.minute, seconds=ex_end_dt.second
			)
			if item_end_td <= cand_start_td:
				if prev_end_td is None or item_end_td > prev_end_td:
					prev_end_td = item_end_td
					prev_entry = item

		if ex_start_dt.date() == target_d:
			item_start_td = timedelta(
				hours=ex_start_dt.hour, minutes=ex_start_dt.minute, seconds=ex_start_dt.second
			)
			if item_start_td >= cand_end_td:
				if next_start_td is None or item_start_td < next_start_td:
					next_start_td = item_start_td

	gap_penalty = 0.0
	if prev_end_td is not None:
		gap_before = _net_gap_minutes(prev_end_td, cand_start_td, bs, be)
		if 15.0 <= gap_before <= 30.0:
			gap_penalty += 0.35
		elif 0.0 < gap_before < 15.0:
			gap_penalty += 0.25

	if next_start_td is not None:
		gap_after = _net_gap_minutes(cand_end_td, next_start_td, bs, be)
		if 15.0 <= gap_after <= 30.0:
			gap_penalty += 0.35
		elif 0.0 < gap_after < 15.0:
			gap_penalty += 0.25

	gap_penalty = min(0.5, gap_penalty)

	waiter_risk = False
	waiter_penalty = 0.0
	if (waiter_preference or "").strip() == "Waiter" and prev_entry is not None and prev_end_td is not None:
		entry_dict = prev_entry["entry"]
		base_mins = max(1.0, flt(entry_dict.get("estimated_duration") or 60.0))
		sigma = get_sigma_for_item(entry_dict.get("entry_type"), default_sigma=DEFAULT_SIGMA)
		p80_mins = estimate_duration(base_mins, sigma=sigma, percentile=0.80)

		ex_start_dt = prev_entry["start_dt"]
		if ex_start_dt.date() == target_d:
			prev_start_td = timedelta(
				hours=ex_start_dt.hour, minutes=ex_start_dt.minute, seconds=ex_start_dt.second
			)
			p80_end_td = effective_end_time(prev_start_td, p80_mins, break_start=bs, break_end=be)
		else:
			overrun_mins = max(0.0, p80_mins - base_mins)
			p80_end_td = effective_end_time(prev_end_td, overrun_mins, break_start=bs, break_end=be)

		if p80_end_td > cand_start_td:
			waiter_risk = True
			waiter_penalty = 1.0

	return round(min(1.0, gap_penalty + waiter_penalty), 6), waiter_risk


def _extract_ro_service_items(
	repair_order: Any,
	selected_service_names: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
	"""Load non-declined Repair Order Service rows with resolved per-service required skills."""
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
		fields = ["name", "service_package", "service_title", "status", "labor_hours"]
		if frappe.db.has_column("Repair Order Service", "required_skill"):
			fields.append("required_skill")
		if frappe.db.has_column("Repair Order Service", "difficulty_level"):
			fields.append("difficulty_level")
		raw_rows = frappe.get_all(
			"Repair Order Service",
			filters={"parent": ro_name, "parenttype": "Repair Order"},
			fields=fields,
			order_by="idx asc",
			ignore_permissions=True,
		)
	else:
		raw_services = (
			repair_order.get("services")
			if isinstance(repair_order, Mapping)
			else getattr(repair_order, "services", None)
		) or []
		raw_rows = []
		for idx, r in enumerate(raw_services):
			if isinstance(r, Mapping):
				raw_rows.append(dict(r))
			else:
				raw_rows.append({
					"name": getattr(r, "name", None) or f"row_{idx}",
					"service_package": getattr(r, "service_package", None),
					"service_title": getattr(r, "service_title", None),
					"status": getattr(r, "status", None),
					"labor_hours": getattr(r, "labor_hours", None),
					"required_skill": getattr(r, "required_skill", None),
					"difficulty_level": getattr(r, "difficulty_level", None),
				})

	filtered = [
		row
		for row in raw_rows
		if (row.get("status") or "").strip() != "Declined"
		and (selected_set is None or str(row.get("name") or "") in selected_set)
	]
	if not filtered:
		return []

	pkg_names = [
		str(row["service_package"]).strip()
		for row in filtered
		if not (row.get("required_skill") or "").strip() and row.get("service_package")
	]
	pkg_map = load_package_skills_map(pkg_names) if pkg_names else {}

	items: list[dict[str, Any]] = []
	for idx, row in enumerate(filtered):
		req_skill = (row.get("required_skill") or "").strip()
		if req_skill:
			row_reqs = normalize_skill_requirements([(req_skill, row.get("difficulty_level") or 1)])
		elif row.get("service_package"):
			row_reqs = list(pkg_map.get(str(row["service_package"]).strip(), ()))
		else:
			row_reqs = []

		labor_hrs = flt(row.get("labor_hours") or 0.0)
		est_mins = max(15, int(round(labor_hrs * 60.0))) if labor_hrs > 0 else 60

		items.append({
			"idx": idx,
			"name": str(row.get("name") or f"row_{idx}"),
			"service_package": row.get("service_package"),
			"service_title": row.get("service_title") or row.get("service_package") or "",
			"required_skills": row_reqs,
			"estimated_minutes": est_mins,
		})
	return items


def plan_multi_service_split(
	repair_order: str,
	selected_service_names: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
	"""Partition RO services into minimal skill-compatible groups (each satisfiable by >= 1 active tech)."""
	items = _extract_ro_service_items(repair_order, selected_service_names=selected_service_names)
	if not items:
		return []

	shop = get_shop_settings()
	active_emp_ids = load_active_technician_employee_ids(settings=shop)
	profiles = load_active_technician_profiles(active_emp_ids)
	emp_ids = sorted(profiles.keys())

	def _qualified_for(reqs: Sequence[tuple[str, int]]) -> list[str]:
		return [e for e in emp_ids if is_technician_qualified(profiles[e], reqs)]

	def _build_group(group_items: list[dict[str, Any]], group_index: int) -> dict[str, Any]:
		ordered = sorted(group_items, key=lambda x: x["idx"])
		combined: list[tuple[str, int]] = []
		for it in ordered:
			combined.extend(it["required_skills"])
		union_reqs = normalize_skill_requirements(combined)
		qual = _qualified_for(union_reqs)
		return {
			"group_index": group_index,
			"service_names": [it["name"] for it in ordered],
			"required_skills": union_reqs,
			"estimated_minutes": sum(int(it["estimated_minutes"]) for it in ordered),
			"qualified_technicians": qual,
			"assignable": bool(qual),
		}

	# Check if a single technician can handle all services together
	all_reqs = normalize_skill_requirements([pair for it in items for pair in it["required_skills"]])
	if _qualified_for(all_reqs):
		return [_build_group(items, 1)]

	# Separate unassignable items (no active tech qualifies even individually)
	assignable_items: list[dict[str, Any]] = []
	unassignable_items: list[dict[str, Any]] = []
	item_qual: dict[int, set[str]] = {}
	for it in items:
		qual = set(_qualified_for(it["required_skills"]))
		if qual:
			assignable_items.append(it)
			item_qual[it["idx"]] = qual
		else:
			unassignable_items.append(it)

	groups_raw: list[list[dict[str, Any]]] = []

	if assignable_items:
		assignable_indices = {it["idx"] for it in assignable_items}
		by_idx = {it["idx"]: it for it in assignable_items}

		# Coverage map: tech -> set of assignable item indices tech can perform
		tech_cover: dict[str, set[int]] = {}
		for e in emp_ids:
			covered = {idx for idx in assignable_indices if e in item_qual[idx]}
			if covered:
				tech_cover[e] = covered

		# Exact check for 2-tech minimal cover (optimal for all 2-way splits)
		best_pair: Optional[tuple[str, str]] = None
		for t1, t2 in combinations(sorted(tech_cover.keys()), 2):
			if (tech_cover[t1] | tech_cover[t2]) == assignable_indices:
				best_pair = (t1, t2)
				break

		if best_pair is not None:
			t1, t2 = best_pair
			g1_idxs = sorted(tech_cover[t1])
			g2_idxs = sorted(assignable_indices - tech_cover[t1])
			if g1_idxs:
				groups_raw.append([by_idx[i] for i in g1_idxs])
			if g2_idxs:
				groups_raw.append([by_idx[i] for i in g2_idxs])
		else:
			# Deterministic greedy set cover by largest remaining coverage
			uncovered = set(assignable_indices)
			while uncovered:
				best_tech = max(
					sorted(tech_cover.keys()),
					key=lambda e: (len(tech_cover[e] & uncovered), -profiles[e]["assignment_tier"]),
				)
				chunk = sorted(tech_cover[best_tech] & uncovered)
				if not chunk:
					break
				groups_raw.append([by_idx[i] for i in chunk])
				uncovered -= set(chunk)

	for it in unassignable_items:
		groups_raw.append([it])

	# Order groups deterministically by earliest service idx
	groups_raw.sort(key=lambda grp: min(it["idx"] for it in grp))
	return [_build_group(grp, idx) for idx, grp in enumerate(groups_raw, start=1)]


def _select_technician_with_context(
	*,
	scheduled_date: Union[str, date, datetime],
	scheduled_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	required_skills: Optional[Sequence[tuple[str, int]]] = None,
	repair_order: Optional[str] = None,
	entry_type: Optional[str] = None,
	waiter_preference: str = "Drop-Off",
	is_comeback: bool = False,
	original_technician: Optional[str] = None,
	exclude_entry: Optional[str] = None,
	candidate_employee_ids: Optional[Sequence[str]] = None,
	settings: Optional[dict[str, Any]] = None,
	profiles_override: Optional[dict[str, dict[str, Any]]] = None,
	all_profiles_override: Optional[dict[str, dict[str, Any]]] = None,
	leaves_override: Optional[dict[str, list[dict[str, Any]]]] = None,
	blocking_entries_override: Optional[list[dict[str, Any]]] = None,
	ledger_override: Optional[dict[str, dict[str, Any]]] = None,
	skill_weights_override: Optional[dict[str, float]] = None,
	is_holiday_fn_override: Any = None,
	prefer_current_tech: Optional[str] = None,
) -> dict[str, Any]:
	"""Core 4-stage assignment pipeline with optional preloaded/simulated context for rebalancing."""
	shop = settings if settings is not None else get_shop_settings()
	policy_mode = (shop.get("assignment_policy_mode") or "Tiered Waterfall").strip()
	iso_date = normalize_date(scheduled_date) or str(getdate(scheduled_date))
	norm_time = normalize_time(scheduled_time) or "08:00:00"
	dur_mins = max(1.0, flt(duration_minutes or shop.get("default_diagnostic_duration") or 60.0))

	# Resolve required skills
	if required_skills is not None:
		reqs = normalize_skill_requirements(required_skills)
	else:
		reqs = get_required_skills_for_repair_order(repair_order) if repair_order else []
		if not reqs and entry_type:
			reqs = get_required_skills_for_schedule_entry({"entry_type": entry_type})

	is_holiday_fn = (
		is_holiday_fn_override
		if is_holiday_fn_override is not None
		else build_holiday_checker(shop.get("holiday_list"), min_date=iso_date)
	)
	span = calculate_working_span(iso_date, norm_time, dur_mins, is_holiday_fn=is_holiday_fn)
	segments = span.get("segments") or []
	prop_start_dt, prop_end_dt = span_datetimes(span)
	span_dates = sorted({seg["date"] for seg in segments}) if segments else [iso_date]
	min_span_date, max_span_date = span_dates[0], span_dates[-1]

	# Load active technician profiles
	if all_profiles_override is not None:
		all_active_profiles = all_profiles_override
	else:
		all_emp_ids = load_active_technician_employee_ids(settings=shop)
		all_active_profiles = load_active_technician_profiles(all_emp_ids)

	if profiles_override is not None:
		profiles = profiles_override
	elif candidate_employee_ids is not None:
		cand_emp_ids = load_active_technician_employee_ids(employee_ids=candidate_employee_ids, settings=shop)
		profiles = load_active_technician_profiles(cand_emp_ids)
	else:
		profiles = all_active_profiles

	emp_list = sorted(profiles.keys())

	# Detect needs_split when RO has multiple services whose union no single active tech holds
	needs_split = False
	if repair_order and reqs:
		if not any(is_technician_qualified(p, reqs) for p in all_active_profiles.values()):
			ro_items = _extract_ro_service_items(repair_order)
			if len(ro_items) > 1:
				needs_split = True

	if not emp_list:
		return {
			"assigned_technician": None,
			"assignment_mode": "Unassigned",
			"assignment_reason": "Unassigned: no active technician profiles available",
			"reason_details": {
				"stage": "hard_gate",
				"policy_mode": policy_mode,
				"required_skills": reqs,
				"feasible_count": 0,
				"excluded": {},
				"needs_split": needs_split,
			},
			"needs_split": needs_split,
		}

	leaves_by_emp = (
		leaves_override
		if leaves_override is not None
		else fetch_leaves_with_period(emp_list, min_span_date, max_span_date)
	)

	if blocking_entries_override is not None:
		blocking_entries = [
			e
			for e in blocking_entries_override
			if (not exclude_entry or str(e.get("name") or "") != exclude_entry)
			and (e.get("status") or "").strip() not in NON_BLOCKING_ENTRY_STATUSES
		]
	else:
		blocking_entries = fetch_blocking_entries(
			min_span_date,
			max_span_date,
			assigned_technicians=emp_list,
			exclude_entry=exclude_entry,
			fields=(
				"name",
				"repair_order",
				"entry_type",
				"assigned_technician",
				"scheduled_date",
				"scheduled_time",
				"estimated_duration",
				"completion_date",
				"completion_time",
				"status",
			),
		)

	entries_by_emp: dict[str, list[dict[str, Any]]] = {e: [] for e in emp_list}
	for entry in blocking_entries:
		tech = str(entry.get("assigned_technician") or "")
		if tech not in entries_by_emp:
			continue
		ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=is_holiday_fn)
		entries_by_emp[tech].append({
			"entry": entry,
			"start_dt": ex_start_dt,
			"end_dt": ex_end_dt,
		})

	# --- STAGE 1: Hard Feasibility Gate ---
	feasible_ids: list[str] = []
	excluded_reasons: dict[str, str] = {}
	target_eff_shifts: dict[str, Optional[dict[str, Any]]] = {}

	for emp_id in emp_list:
		profile = profiles[emp_id]
		if not is_technician_qualified(profile, reqs):
			excluded_reasons[emp_id] = "unqualified"
			continue

		emp_leaves = leaves_by_emp.get(emp_id, ())
		shift_ok = True
		shift_reason = ""
		for seg in segments:
			seg_date = seg["date"]
			if is_holiday_fn(seg_date):
				shift_ok = False
				shift_reason = "holiday"
				break
			raw_shift = get_technician_shift_window(profile, seg_date, settings=shop)
			eff_shift = apply_leave_to_shift_window(raw_shift, emp_leaves, seg_date)
			if seg_date == iso_date:
				target_eff_shifts[emp_id] = eff_shift
			if not is_interval_within_shift(
				to_timedelta(seg["start_time"]),
				to_timedelta(seg["end_time"]),
				eff_shift,
			):
				shift_ok = False
				shift_reason = "shift_or_leave"
				break

		if not shift_ok:
			excluded_reasons[emp_id] = shift_reason
			continue

		if iso_date not in {seg["date"] for seg in segments}:
			raw_shift = get_technician_shift_window(profile, iso_date, settings=shop)
			target_eff_shifts[emp_id] = apply_leave_to_shift_window(raw_shift, emp_leaves, iso_date)

		has_overlap = any(
			intervals_overlap(prop_start_dt, prop_end_dt, item["start_dt"], item["end_dt"])
			for item in entries_by_emp[emp_id]
		)
		if has_overlap:
			excluded_reasons[emp_id] = "schedule_overlap"
			continue

		feasible_ids.append(emp_id)

	if not feasible_ids:
		reason_msg = (
			"Unassigned: multi-service RO requires split across technicians"
			if needs_split
			else "Unassigned: no qualified technician available for slot"
		)
		return {
			"assigned_technician": None,
			"assignment_mode": "Unassigned",
			"assignment_reason": reason_msg,
			"reason_details": {
				"stage": "hard_gate",
				"policy_mode": policy_mode,
				"required_skills": reqs,
				"feasible_count": 0,
				"excluded": excluded_reasons,
				"needs_split": needs_split,
			},
			"needs_split": needs_split,
		}

	# --- STAGE 2: Sticky Continuity Check ---
	sticky_candidates = _resolve_sticky_candidates(
		repair_order=repair_order,
		is_comeback=bool(is_comeback),
		original_technician=original_technician,
		exclude_entry=exclude_entry,
	)
	sticky_bypassed: list[dict[str, str]] = []
	feasible_set = set(feasible_ids)

	for sticky_type, sticky_label, sticky_tech in sticky_candidates:
		if sticky_tech in feasible_set:
			tech_name = profiles[sticky_tech].get("employee_name") or sticky_tech
			return {
				"assigned_technician": sticky_tech,
				"assignment_mode": "Sticky Continuity",
				"assignment_reason": f"Sticky Continuity ({sticky_label}) -> {tech_name}",
				"reason_details": {
					"stage": "sticky_continuity",
					"policy_mode": policy_mode,
					"sticky_type": sticky_type,
					"sticky_label": sticky_label,
					"sticky_tech": sticky_tech,
					"sticky_bypassed": sticky_bypassed,
					"required_skills": reqs,
					"feasible_count": len(feasible_ids),
					"needs_split": needs_split,
				},
				"needs_split": needs_split,
			}
		sticky_bypassed.append({
			"sticky_type": sticky_type,
			"sticky_tech": sticky_tech,
			"reason": excluded_reasons.get(sticky_tech, "not_in_candidate_pool"),
		})

	# --- STAGE 3: Role Gate + Policy Stratification ---
	if ledger_override is not None:
		ledger = {e: ledger_override[e] for e in feasible_ids if e in ledger_override}
		missing = [e for e in feasible_ids if e not in ledger]
		if missing:
			ledger.update(
				compute_fairness_ledger(iso_date, employee_ids=missing, exclude_entry=exclude_entry, settings=shop)
			)
	else:
		ledger = compute_fairness_ledger(
			iso_date,
			employee_ids=feasible_ids,
			exclude_entry=exclude_entry,
			settings=shop,
		)

	primary_ids: list[str] = []
	backup_ids: list[str] = []
	for emp_id in feasible_ids:
		w_role = worst_skill_role(profiles[emp_id], reqs)
		if w_role in ("Preferred", "Standard"):
			primary_ids.append(emp_id)
		else:
			backup_ids.append(emp_id)

	shop_cap = flt(shop.get("tier_spillover_threshold_pct", 85.0))

	def _under_soft_cap(emp_id: str) -> bool:
		prof = profiles[emp_id]
		raw_cap = prof.get("daily_soft_cap_pct")
		cap = flt(raw_cap) if (raw_cap is not None and flt(raw_cap) > 0.0) else shop_cap
		util = flt(ledger[emp_id]["target_day_util_pct"])
		return util < cap

	active_pool: list[str] = []
	role_group = "Primary"
	selected_tier: Optional[int] = None
	spillover_used = False
	cap_bypassed = False

	if policy_mode == "Tiered Waterfall":
		if primary_ids:
			primary_tiers = sorted({profiles[e]["assignment_tier"] for e in primary_ids})
			min_primary_tier = primary_tiers[0]
			for tier in primary_tiers:
				tier_cands = [e for e in primary_ids if profiles[e]["assignment_tier"] == tier and _under_soft_cap(e)]
				if tier_cands:
					active_pool = tier_cands
					selected_tier = tier
					role_group = "Primary"
					spillover_used = tier > min_primary_tier
					break
			if not active_pool:
				for tier in primary_tiers:
					tier_cands = [e for e in primary_ids if profiles[e]["assignment_tier"] == tier]
					if tier_cands:
						active_pool = tier_cands
						selected_tier = tier
						role_group = "Primary"
						cap_bypassed = True
						break
		if not active_pool and backup_ids:
			role_group = "Backup"
			backup_tiers = sorted({profiles[e]["assignment_tier"] for e in backup_ids})
			for tier in backup_tiers:
				tier_cands = [e for e in backup_ids if profiles[e]["assignment_tier"] == tier and _under_soft_cap(e)]
				if tier_cands:
					active_pool = tier_cands
					selected_tier = tier
					break
			if not active_pool:
				for tier in backup_tiers:
					tier_cands = [e for e in backup_ids if profiles[e]["assignment_tier"] == tier]
					if tier_cands:
						active_pool = tier_cands
						selected_tier = tier
						cap_bypassed = True
						break
	else:
		# Equal Balance or Weighted Share: Primary group competes across all tiers; Backup only if Primary empty
		if primary_ids:
			active_pool = list(primary_ids)
			role_group = "Primary"
		else:
			active_pool = list(backup_ids)
			role_group = "Backup"

	# --- STAGE 4: Blended Deterministic Scoring (Lower Cost Wins) ---
	w_f = max(0.0, flt(shop.get("fairness_weight", 0.55)))
	w_c = max(0.0, flt(shop.get("skill_conservation_weight", 0.25)))
	w_p = max(0.0, flt(shop.get("skill_preference_weight", 0.12)))
	w_k = max(0.0, flt(shop.get("schedule_packing_weight", 0.08)))

	load_metric_key = "weighted_load" if policy_mode == "Weighted Share" else "normalized_load"
	raw_loads = {e: flt(ledger[e][load_metric_key]) for e in active_pool}
	min_load = min(raw_loads.values())
	max_load = max(raw_loads.values())
	load_span = max_load - min_load

	skill_weights = (
		skill_weights_override
		if skill_weights_override is not None
		else _load_skill_scarcity_weights(all_active_profiles)
	)
	raw_overquals = {e: _compute_overqualification_raw(profiles[e], reqs, skill_weights) for e in active_pool}
	min_oq = min(raw_overquals.values())
	max_oq = max(raw_overquals.values())
	oq_span = max_oq - min_oq

	day_segs = [seg for seg in segments if seg["date"] == iso_date]
	if day_segs:
		cand_start_td = to_timedelta(day_segs[0]["start_time"])
		cand_end_td = to_timedelta(day_segs[-1]["end_time"])
	else:
		cand_start_td = to_timedelta(norm_time)
		cand_end_td = cand_start_td + timedelta(minutes=dur_mins)

	scored_candidates: list[dict[str, Any]] = []
	for emp_id in active_pool:
		norm_load = 0.0 if load_span <= 1e-9 else (raw_loads[emp_id] - min_load) / load_span
		overqual_cost = 0.0 if oq_span <= 1e-9 else (raw_overquals[emp_id] - min_oq) / oq_span
		pref_cost = _compute_preference_cost(profiles[emp_id], reqs)
		pack_cost, waiter_risk = _compute_packing_and_waiter_cost(
			iso_date=iso_date,
			cand_start_td=cand_start_td,
			cand_end_td=cand_end_td,
			eff_shift=target_eff_shifts.get(emp_id),
			tech_day_entries=entries_by_emp[emp_id],
			waiter_preference=waiter_preference,
		)

		total_cost = w_f * norm_load + w_c * overqual_cost + w_p * pref_cost + w_k * pack_cost
		scored_candidates.append({
			"employee": emp_id,
			"employee_name": profiles[emp_id].get("employee_name") or emp_id,
			"assignment_tier": profiles[emp_id]["assignment_tier"],
			"role_group": role_group,
			"raw_load": round(raw_loads[emp_id], 6),
			"norm_load": round(norm_load, 6),
			"overqual_raw": round(raw_overquals[emp_id], 4),
			"overqual_cost": round(overqual_cost, 6),
			"pref_cost": round(pref_cost, 6),
			"pack_cost": round(pack_cost, 6),
			"waiter_spillover_risk": waiter_risk,
			"total_cost": round(total_cost, 6),
			"assigned_frt_hours": ledger[emp_id]["assigned_frt_hours"],
			"available_shift_hours": ledger[emp_id]["available_shift_hours"],
			"target_day_util_pct": ledger[emp_id]["target_day_util_pct"],
			"last_assigned_at": ledger[emp_id]["last_assigned_at"],
		})

	def _sort_key(cand: dict[str, Any]) -> tuple[Any, ...]:
		if prefer_current_tech:
			return (
				round(cand["total_cost"], 6),
				0 if cand["employee"] == prefer_current_tech else 1,
				cand["last_assigned_at"] or "",
				cand["employee"],
			)
		return (
			round(cand["total_cost"], 6),
			cand["last_assigned_at"] or "",
			cand["employee"],
		)

	scored_candidates.sort(key=_sort_key)
	winner = scored_candidates[0]
	winner_id = winner["employee"]
	winner_tier = winner["assignment_tier"]
	load_pct = round(flt(winner["raw_load"]) * 100.0, 1)

	tags: list[str] = [role_group]
	if spillover_used:
		tags.append("Tier Spillover")
	if cap_bypassed:
		tags.append("Over Cap")
	if (waiter_preference or "").strip() == "Waiter" and not winner["waiter_spillover_risk"]:
		tags.append("Waiter Safe")

	reason_str = (
		f"Tier {winner_tier} {_tier_label(winner_tier)} ({policy_mode}) · "
		f"load {load_pct}% ({winner['assigned_frt_hours']:.1f}h/{winner['available_shift_hours']:.1f}h) · "
		f"cost {winner['total_cost']:.3f} [{', '.join(tags)}]"
	)

	return {
		"assigned_technician": winner_id,
		"assignment_mode": "Auto",
		"assignment_reason": reason_str,
		"reason_details": {
			"stage": "scored",
			"policy_mode": policy_mode,
			"role_group": role_group,
			"selected_tier": selected_tier if selected_tier is not None else winner_tier,
			"spillover_used": spillover_used,
			"cap_bypassed": cap_bypassed,
			"required_skills": reqs,
			"sticky_bypassed": sticky_bypassed,
			"feasible_count": len(feasible_ids),
			"winner": winner,
			"candidates": scored_candidates,
			"needs_split": needs_split,
		},
		"needs_split": needs_split,
	}


def select_technician_for_slot(
	*,
	scheduled_date: Union[str, date, datetime],
	scheduled_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	required_skills: Optional[Sequence[tuple[str, int]]] = None,
	repair_order: Optional[str] = None,
	entry_type: Optional[str] = None,
	waiter_preference: str = "Drop-Off",
	is_comeback: bool = False,
	original_technician: Optional[str] = None,
	exclude_entry: Optional[str] = None,
	candidate_employee_ids: Optional[Sequence[str]] = None,
	settings: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
	"""Run 4-stage technician assignment pipeline for a candidate slot."""
	return _select_technician_with_context(
		scheduled_date=scheduled_date,
		scheduled_time=scheduled_time,
		duration_minutes=duration_minutes,
		required_skills=required_skills,
		repair_order=repair_order,
		entry_type=entry_type,
		waiter_preference=waiter_preference,
		is_comeback=is_comeback,
		original_technician=original_technician,
		exclude_entry=exclude_entry,
		candidate_employee_ids=candidate_employee_ids,
		settings=settings,
	)

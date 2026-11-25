# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent per-Repair Order Service Time Observation allocator."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Sequence

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from ism.ism.doctype.repair_order.pricing import _get_default_labor_rate
from ism.scheduling.time_utils import get_shop_settings, to_timedelta
from ism.time_calibration.contracts import (
	ALLOC_RO_WIDE,
	ALLOC_SINGLE_JOB,
	ALLOC_SUPPLEMENT_POST_APPROVAL,
	ALLOC_TECH_SCOPED,
	ALPHA_CLAMP_MAX,
	MIN_VALID_SESSION_HOURS,
	REVIEW_CLEAN,
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_NEEDS_REVIEW,
	REVIEW_VOIDED,
	SESSION_CLOSED,
	SESSION_OPEN,
	CalibrationSettings,
	derive_overlap_group_info,
	get_calibration_settings,
	is_diagnostic_service,
)
from ism.time_calibration.overlap import (
	ensure_overlap_group,
	get_effective_overlap_factors,
	resolve_service_overlap_bucket,
)

COMPLETED_RO_STATUSES = (
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
)

HOLD_RO_STATUSES = (
	"Waiting on Parts",
	"Pending Authorization",
)


# ---------------------------------------------------------------------------
# Net Working Session & Interval Helpers (uses Wave 2A time_utils if present,
# with a clean local fallback so Wave 2B works standalone before Wave 3 merge)
# ---------------------------------------------------------------------------


def _local_subtract_intervals(
	base_start: datetime,
	base_end: datetime,
	exclude_intervals: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
	"""Subtract excluded datetime windows from `[base_start, base_end]`."""
	if base_end <= base_start:
		return []
	if not exclude_intervals:
		return [(base_start, base_end)]

	clipped: list[tuple[datetime, datetime]] = []
	for ex_s, ex_e in exclude_intervals:
		if ex_s is None or ex_e is None or ex_e <= ex_s:
			continue
		s = max(base_start, ex_s)
		e = min(base_end, ex_e)
		if e > s:
			clipped.append((s, e))

	if not clipped:
		return [(base_start, base_end)]

	clipped.sort(key=lambda pair: (pair[0], pair[1]))
	merged: list[tuple[datetime, datetime]] = [clipped[0]]
	for s, e in clipped[1:]:
		prev_s, prev_e = merged[-1]
		if s <= prev_e:
			merged[-1] = (prev_s, max(prev_e, e))
		else:
			merged.append((s, e))

	result: list[tuple[datetime, datetime]] = []
	cursor = base_start
	for ex_s, ex_e in merged:
		if ex_s > cursor:
			result.append((cursor, ex_s))
		cursor = max(cursor, ex_e)
	if cursor < base_end:
		result.append((cursor, base_end))
	return result


def _local_calculate_net_working_session(
	start_dt: datetime,
	end_dt: datetime,
	*,
	settings: dict[str, Any] | None = None,
	is_holiday_fn: Callable[[date], bool] | None = None,
	max_single_session_hours: float = 8.5,
) -> dict[str, Any]:
	"""Fallback net working session calculator matching Wave 2A contract."""
	s_dt = get_datetime(start_dt)
	e_dt = get_datetime(end_dt)
	if not s_dt or not e_dt or e_dt <= s_dt:
		return {
			"raw_hours": 0.0,
			"net_hours": 0.0,
			"spans_overnight": False,
			"needs_review": False,
			"flag_reason": "",
		}

	raw_hours = (e_dt - s_dt).total_seconds() / 3600.0
	shop = settings if settings is not None else get_shop_settings()
	op_start = to_timedelta(shop.get("operating_hours_start") or timedelta(hours=8, minutes=30))
	op_end = to_timedelta(shop.get("operating_hours_end") or timedelta(hours=17, minutes=0))
	break_start = to_timedelta(shop["break_start"]) if shop.get("break_start") is not None else None
	break_end = to_timedelta(shop["break_end"]) if shop.get("break_end") is not None else None

	def _day_lunch_overlap(seg_s: datetime, seg_e: datetime, day_date: date) -> float:
		if break_start is None or break_end is None or break_end <= break_start:
			return 0.0
		day_midnight = datetime.combine(day_date, time.min)
		b_s = day_midnight + break_start
		b_e = day_midnight + break_end
		ov_s = max(seg_s, b_s)
		ov_e = min(seg_e, b_e)
		return max(0.0, (ov_e - ov_s).total_seconds() / 3600.0)

	spans_overnight = s_dt.date() != e_dt.date()
	needs_review = False
	flag_reason = ""

	if not spans_overnight:
		# Same-day: subtract configured lunch break only (preserve early/late/weekend overtime)
		lunch_h = _day_lunch_overlap(s_dt, e_dt, s_dt.date())
		net_hours = max(0.0, raw_hours - lunch_h)
	else:
		# Multi-day: clip to operating hours minus lunch, excluding holidays
		needs_review = True
		flag_reason = "Overnight Session"
		net_hours = 0.0
		cur_date = s_dt.date()
		end_date = e_dt.date()
		while cur_date <= end_date:
			if is_holiday_fn is None or not is_holiday_fn(cur_date):
				day_midnight = datetime.combine(cur_date, time.min)
				win_s = max(s_dt, day_midnight + op_start)
				win_e = min(e_dt, day_midnight + op_end)
				if win_e > win_s:
					seg_h = (win_e - win_s).total_seconds() / 3600.0
					seg_h -= _day_lunch_overlap(win_s, win_e, cur_date)
					net_hours += max(0.0, seg_h)
			cur_date += timedelta(days=1)

	if net_hours > max_single_session_hours:
		needs_review = True
		if not flag_reason:
			flag_reason = "Oversized Session"

	return {
		"raw_hours": flt(raw_hours, 4),
		"net_hours": flt(net_hours, 4),
		"spans_overnight": spans_overnight,
		"needs_review": needs_review,
		"flag_reason": flag_reason,
	}


def _subtract_intervals(
	base_start: datetime,
	base_end: datetime,
	exclude_intervals: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
	try:
		from ism.scheduling.time_utils import subtract_intervals

		return subtract_intervals(base_start, base_end, exclude_intervals)
	except ImportError:
		return _local_subtract_intervals(base_start, base_end, exclude_intervals)


def _calculate_net_working_session(
	start_dt: datetime,
	end_dt: datetime,
	*,
	settings: dict[str, Any] | None = None,
	max_single_session_hours: float = 8.5,
) -> dict[str, Any]:
	try:
		from ism.scheduling.time_utils import calculate_net_working_session

		return calculate_net_working_session(
			start_dt,
			end_dt,
			settings=settings,
			max_single_session_hours=max_single_session_hours,
		)
	except ImportError:
		return _local_calculate_net_working_session(
			start_dt,
			end_dt,
			settings=settings,
			max_single_session_hours=max_single_session_hours,
		)


# ---------------------------------------------------------------------------
# Internal Data Helpers
# ---------------------------------------------------------------------------


def _load_ro(ro_or_name: Any):
	if ro_or_name is None:
		return None
	if hasattr(ro_or_name, "doctype") and ro_or_name.doctype == "Repair Order":
		return ro_or_name
	name = str(ro_or_name or "").strip()
	if not name or not frappe.db.exists("Repair Order", name):
		return None
	return frappe.get_doc("Repair Order", name)


def _prefetch_packages(services: Sequence[Any]) -> dict[str, dict[str, Any]]:
	pkg_names = sorted(
		{
			str(getattr(s, "service_package", "") or "").strip()
			for s in services
			if getattr(s, "service_package", None)
		}
	)
	if not pkg_names:
		return {}
	rows = frappe.get_all(
		"Service Package",
		filters={"name": ["in", pkg_names]},
		fields=[
			"name",
			"package_name",
			"correction_code",
			"standard_labor_hours",
			"overlap_group",
			"exclude_from_time_calibration",
			"calibrated_median_minutes",
			"calibrated_sigma",
			"is_symmetrical",
		],
	)
	return {row["name"]: row for row in rows}


def _get_vehicle_context(ro: Any) -> tuple[str, str]:
	vehicle = getattr(ro, "vehicle", None)
	if not vehicle:
		return ("", "")
	veh = frappe.db.get_value(
		"Repair Vehicle",
		vehicle,
		["model", "vehicle_generation"],
		as_dict=True,
	)
	if not veh:
		return ("", "")
	return (str(veh.get("model") or ""), str(veh.get("vehicle_generation") or ""))


def _extract_closed_sessions(ro: Any) -> list[dict[str, Any]]:
	sessions: list[dict[str, Any]] = []
	for tl in getattr(ro, "time_logs", None) or []:
		status = str(getattr(tl, "session_status", SESSION_CLOSED) or SESSION_CLOSED).strip()
		if status == SESSION_OPEN:
			continue
		f_dt = get_datetime(getattr(tl, "from_time", None)) if getattr(tl, "from_time", None) else None
		t_dt = get_datetime(getattr(tl, "to_time", None)) if getattr(tl, "to_time", None) else None
		if f_dt and t_dt and t_dt > f_dt:
			sessions.append(
				{
					"from_time": f_dt,
					"to_time": t_dt,
					"time_in_hours": flt(getattr(tl, "time_in_hours", 0)),
					"technician": getattr(tl, "technician", None) or None,
					"needs_review": cint(getattr(tl, "needs_review", 0)),
					"spans_overnight": cint(getattr(tl, "spans_overnight", 0)),
					"flag_reason": str(getattr(tl, "flag_reason", "") or "").strip(),
				}
			)

	# Legacy fallback for ROs completed before time_logs existed
	if not sessions and flt(getattr(ro, "total_bay_hours", 0)) > 0:
		p_up = get_datetime(getattr(ro, "picked_up_at", None)) if getattr(ro, "picked_up_at", None) else None
		p_dk = get_datetime(getattr(ro, "parked_at", None)) if getattr(ro, "parked_at", None) else None
		if p_up and p_dk and p_dk > p_up:
			sessions.append(
				{
					"from_time": p_up,
					"to_time": p_dk,
					"time_in_hours": flt(ro.total_bay_hours),
					"technician": None,
					"needs_review": 0,
					"spans_overnight": 0,
					"flag_reason": "",
				}
			)

	sessions.sort(key=lambda s: (s["from_time"], s["to_time"]))
	return sessions


def _get_cross_ro_tech_overlaps(
	ro_name: str,
	technician: str | None,
	window_start: datetime,
	window_end: datetime,
) -> list[tuple[datetime, datetime]]:
	if not technician or window_end <= window_start:
		return []
	rows = frappe.get_all(
		"Repair Order Time Log",
		filters={
			"parent": ["!=", ro_name],
			"parenttype": "Repair Order",
			"technician": technician,
			"from_time": ["<", window_end],
			"to_time": [">", window_start],
		},
		fields=["from_time", "to_time", "session_status"],
	)
	overlaps: list[tuple[datetime, datetime]] = []
	for row in rows:
		if row.get("session_status") == SESSION_OPEN:
			continue
		f_dt = get_datetime(row.get("from_time")) if row.get("from_time") else None
		t_dt = get_datetime(row.get("to_time")) if row.get("to_time") else None
		if f_dt and t_dt and t_dt > f_dt:
			overlaps.append((f_dt, t_dt))
	return overlaps


def _resolve_supplement_approved_at(
	ro: Any,
	concrete_supplements: Sequence[Any],
	earliest_pickup: datetime | None,
) -> datetime | None:
	auth_times = [
		get_datetime(getattr(s, "authorized_at", None))
		for s in concrete_supplements
		if getattr(s, "authorized_at", None)
	]
	if auth_times:
		return min(auth_times)

	# Fallback: earliest approval_logs.approval_datetime after pickup
	log_times: list[datetime] = []
	for log in getattr(ro, "approval_logs", None) or []:
		dt = get_datetime(getattr(log, "approval_datetime", None)) if getattr(log, "approval_datetime", None) else None
		if dt is None:
			continue
		if earliest_pickup is None or dt > earliest_pickup:
			log_times.append(dt)
	if log_times:
		return min(log_times)

	return None


def _detect_mid_job_supplement_mixed(
	ro: Any,
	pre_sup_concrete: Sequence[Any],
	concrete_supplements: Sequence[Any],
	sessions: Sequence[dict[str, Any]],
) -> bool:
	"""Return True if an open bay session bridged a mid-job supplement without a Temporary Park."""
	if not pre_sup_concrete or not concrete_supplements or not sessions:
		return False

	anchors: list[datetime] = []
	for sup in concrete_supplements:
		auth_dt = get_datetime(getattr(sup, "authorized_at", None)) if getattr(sup, "authorized_at", None) else None
		if auth_dt is not None:
			anchors.append(auth_dt)
		else:
			cr_dt = get_datetime(getattr(sup, "creation", None)) if getattr(sup, "creation", None) else None
			if cr_dt is not None:
				anchors.append(cr_dt)

	earliest_pickup = sessions[0]["from_time"]
	for log in getattr(ro, "approval_logs", None) or []:
		dt = get_datetime(getattr(log, "approval_datetime", None)) if getattr(log, "approval_datetime", None) else None
		if dt is not None and dt > earliest_pickup:
			anchors.append(dt)

	for sess in sessions:
		f_dt = sess["from_time"]
		t_dt = sess["to_time"]
		for ts in anchors:
			if f_dt < ts < t_dt:
				return True

	return False


def _detect_mid_bay_hold(
	ro: Any,
	services: Sequence[Any],
	sessions: Sequence[dict[str, Any]],
	*,
	is_diag_to_supplement: bool,
) -> bool:
	"""Detect if a Parts / Authorization hold overlapped an open bay session without a Temporary Park."""
	if is_diag_to_supplement or not sessions:
		return False

	# 1. Explicit manual parts hold flag still active on RO
	if cint(getattr(ro, "manual_parts_hold", 0)) == 1:
		return True

	# 2. Unapproved supplement pending while a bay session stayed open
	for s in services:
		if cint(getattr(s, "is_supplement", 0)) == 1 and getattr(s, "status", None) == "Pending Approval":
			cr_dt = get_datetime(getattr(s, "creation", None)) if getattr(s, "creation", None) else None
			if cr_dt is None:
				return True
			for sess in sessions:
				if sess["from_time"] < cr_dt < sess["to_time"]:
					return True

	# 3. Version history showing status transitioned into Waiting on Parts / Pending Authorization
	# or manual_parts_hold toggled to 1 during an open bay session
	if not getattr(ro, "name", None):
		return False

	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": "Repair Order", "docname": ro.name},
		fields=["creation", "data"],
		order_by="creation asc",
	)
	if not versions:
		return False

	hold_intervals: list[tuple[datetime, datetime]] = []
	active_hold_start: datetime | None = None
	for v in versions:
		v_dt = get_datetime(v.get("creation")) if v.get("creation") else None
		if not v_dt:
			continue
		raw_data = v.get("data")
		if not raw_data:
			continue
		try:
			parsed = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
		except Exception:
			continue
		for change in parsed.get("changed") or []:
			if not isinstance(change, (list, tuple)) or len(change) < 3:
				continue
			fieldname, old_val, new_val = change[0], change[1], change[2]
			if fieldname == "status":
				if new_val in HOLD_RO_STATUSES and active_hold_start is None:
					active_hold_start = v_dt
				elif old_val in HOLD_RO_STATUSES and new_val not in HOLD_RO_STATUSES and active_hold_start is not None:
					hold_intervals.append((active_hold_start, v_dt))
					active_hold_start = None
			elif fieldname == "manual_parts_hold":
				if cint(new_val) == 1 and active_hold_start is None:
					active_hold_start = v_dt
				elif cint(new_val) == 0 and active_hold_start is not None:
					hold_intervals.append((active_hold_start, v_dt))
					active_hold_start = None

	if active_hold_start is not None:
		fallback_end = get_datetime(getattr(ro, "modified", None)) or sessions[-1]["to_time"]
		hold_intervals.append((active_hold_start, max(active_hold_start, fallback_end)))

	for h_s, h_e in hold_intervals:
		for sess in sessions:
			f_dt = sess["from_time"]
			t_dt = sess["to_time"]
			if f_dt < h_s < t_dt:
				return True
			if h_e > h_s and f_dt < h_e and h_s < t_dt:
				return True

	return False


def _evaluate_sessions_net_hours(
	ro_name: str,
	sessions: Sequence[dict[str, Any]],
	*,
	clip_start_at: datetime | None,
	cal_settings: CalibrationSettings,
) -> tuple[list[dict[str, Any]], bool, str]:
	"""Evaluate net active hours per session, optionally clipping to `clip_start_at`."""
	shop_settings = get_shop_settings()
	evaluated: list[dict[str, Any]] = []
	any_needs_review = False
	first_flag_reason = ""

	for sess in sessions:
		f_dt = sess["from_time"]
		t_dt = sess["to_time"]

		if clip_start_at is not None:
			eff_start = max(f_dt, clip_start_at)
			if t_dt <= eff_start:
				continue

			overlaps = _get_cross_ro_tech_overlaps(ro_name, sess["technician"], eff_start, t_dt)
			segments = _subtract_intervals(eff_start, t_dt, overlaps)
			net_h = 0.0
			sess_needs_review = bool(sess["needs_review"] or sess["spans_overnight"])
			sess_flag = sess["flag_reason"]
			for seg_s, seg_e in segments:
				res = _calculate_net_working_session(
					seg_s,
					seg_e,
					settings=shop_settings,
					max_single_session_hours=cal_settings.max_single_session_hours,
				)
				net_h += flt(res.get("net_hours", 0))
				if res.get("needs_review"):
					sess_needs_review = True
					if not sess_flag and res.get("flag_reason"):
						sess_flag = str(res["flag_reason"])
			if net_h > cal_settings.max_single_session_hours:
				sess_needs_review = True
				if not sess_flag:
					sess_flag = "Oversized Session"
		else:
			if flt(sess["time_in_hours"]) > 0:
				net_h = flt(sess["time_in_hours"])
				sess_needs_review = bool(sess["needs_review"] or sess["spans_overnight"])
				sess_flag = sess["flag_reason"]
				if net_h > cal_settings.max_single_session_hours:
					sess_needs_review = True
					if not sess_flag:
						sess_flag = "Oversized Session"
			else:
				overlaps = _get_cross_ro_tech_overlaps(ro_name, sess["technician"], f_dt, t_dt)
				segments = _subtract_intervals(f_dt, t_dt, overlaps)
				net_h = 0.0
				sess_needs_review = bool(sess["needs_review"] or sess["spans_overnight"])
				sess_flag = sess["flag_reason"]
				for seg_s, seg_e in segments:
					res = _calculate_net_working_session(
						seg_s,
						seg_e,
						settings=shop_settings,
						max_single_session_hours=cal_settings.max_single_session_hours,
					)
					net_h += flt(res.get("net_hours", 0))
					if res.get("needs_review"):
						sess_needs_review = True
						if not sess_flag and res.get("flag_reason"):
							sess_flag = str(res["flag_reason"])

		if sess_needs_review:
			any_needs_review = True
			if not first_flag_reason:
				first_flag_reason = sess_flag or "Session Needs Review"

		evaluated.append(
			{
				"from_time": max(f_dt, clip_start_at) if clip_start_at else f_dt,
				"to_time": t_dt,
				"net_hours": flt(net_h, 4),
				"technician": sess["technician"],
				"needs_review": sess_needs_review,
				"flag_reason": sess_flag,
			}
		)

	return (evaluated, any_needs_review, first_flag_reason)


def _compute_base_weight_and_frt(
	service_row: Any,
	pkg_meta: dict[str, Any] | None,
	default_labor_rate: float,
) -> tuple[float, float]:
	"""Return `(base_weight_hours, frt_hours)` strictly billing-independent when linked to Service Package."""
	pkg_cal_min = flt(pkg_meta.get("calibrated_median_minutes")) if pkg_meta else 0.0
	pkg_frt_h = flt(pkg_meta.get("standard_labor_hours")) if pkg_meta else 0.0
	row_frt_h = flt(getattr(service_row, "labor_hours", 0))
	row_labor_amt = flt(getattr(service_row, "labor_amount", 0))
	imputed_h = (row_labor_amt / default_labor_rate) if (row_labor_amt > 0 and default_labor_rate > 0) else 0.0

	if pkg_cal_min > 0:
		base_weight = pkg_cal_min / 60.0
	elif pkg_frt_h > 0:
		base_weight = pkg_frt_h
	elif row_frt_h > 0:
		base_weight = row_frt_h
	elif imputed_h > 0:
		base_weight = imputed_h
	else:
		base_weight = 0.0

	if pkg_frt_h > 0:
		frt_hours = pkg_frt_h
	elif row_frt_h > 0:
		frt_hours = row_frt_h
	elif imputed_h > 0:
		frt_hours = imputed_h
	else:
		frt_hours = 0.0

	return (base_weight, frt_hours)


def _void_existing_observations(ro_name: str) -> list[str]:
	rows = frappe.get_all(
		"Service Time Observation",
		filters={"repair_order": ro_name},
		fields=["name", "review_status", "exclude_from_calibration"],
	)
	names: list[str] = []
	for row in rows:
		name = row["name"]
		names.append(name)
		if row.get("review_status") != REVIEW_VOIDED or cint(row.get("exclude_from_calibration")) != 1:
			frappe.db.set_value(
				"Service Time Observation",
				name,
				{
					"review_status": REVIEW_VOIDED,
					"exclude_from_calibration": 1,
				},
			)
	return names


def _delete_stale_observations(ro_name: str, active_service_row_names: set[str]) -> None:
	existing = frappe.get_all(
		"Service Time Observation",
		filters={"repair_order": ro_name},
		fields=["name", "repair_order_service"],
	)
	for row in existing:
		if row.get("repair_order_service") not in active_service_row_names:
			frappe.delete_doc(
				"Service Time Observation",
				row["name"],
				force=True,
				ignore_permissions=True,
			)


# ---------------------------------------------------------------------------
# Public Entrypoint
# ---------------------------------------------------------------------------


def recompute_observations_for_ro(ro_or_name: Any) -> list[str]:
	"""Idempotently compute and persist Service Time Observation records for a Repair Order.

	Matches `contracts.recompute_ro_observations`.
	"""
	ro = _load_ro(ro_or_name)
	if ro is None or not getattr(ro, "name", None):
		return []

	if ro.status == "Cancelled":
		return _void_existing_observations(ro.name)

	if ro.status not in COMPLETED_RO_STATUSES:
		return []

	settings = get_calibration_settings()
	all_services = list(getattr(ro, "services", None) or [])
	pkg_map = _prefetch_packages(all_services)

	approved_completed = [
		s
		for s in all_services
		if getattr(s, "status", None) == "Approved" and cint(getattr(s, "is_completed", 0)) == 1
	]
	concrete_services = [
		s for s in approved_completed if not is_diagnostic_service(s, pkg_map.get(getattr(s, "service_package", None)))
	]

	if not concrete_services:
		_delete_stale_observations(ro.name, set())
		return []

	# Pre-supplement vs supplement breakdown across all approved services
	pre_sup_approved = [
		s
		for s in all_services
		if getattr(s, "status", None) == "Approved" and cint(getattr(s, "is_supplement", 0)) == 0
	]
	pre_sup_concrete = [
		s for s in pre_sup_approved if not is_diagnostic_service(s, pkg_map.get(getattr(s, "service_package", None)))
	]
	concrete_supplements = [s for s in concrete_services if cint(getattr(s, "is_supplement", 0)) == 1]

	raw_sessions = _extract_closed_sessions(ro)
	earliest_pickup = raw_sessions[0]["from_time"] if raw_sessions else None

	# Case A: Diagnostic -> Supplement RO
	is_diag_to_supplement = False
	clip_start_at: datetime | None = None
	if concrete_supplements and not pre_sup_concrete:
		sup_approved_at = _resolve_supplement_approved_at(ro, concrete_supplements, earliest_pickup)
		if sup_approved_at is not None:
			is_diag_to_supplement = True
			clip_start_at = sup_approved_at

	# Evaluate net active hours across sessions
	eval_sessions, sess_needs_review, sess_flag_reason = _evaluate_sessions_net_hours(
		ro.name,
		raw_sessions,
		clip_start_at=clip_start_at,
		cal_settings=settings,
	)
	ro_net_active_hours = flt(sum(flt(s["net_hours"]) for s in eval_sessions), 4)

	# RO-level quality flags
	ro_needs_review = False
	ro_flag_reason = ""

	# Case B: Concrete Repair + Mid-Job Supplement without Temporary Park
	if _detect_mid_job_supplement_mixed(ro, pre_sup_concrete, concrete_supplements, raw_sessions):
		ro_needs_review = True
		ro_flag_reason = "Mid-Job Supplement Mixed"

	# Case C: Mid-Bay Hold Detection
	if not ro_needs_review and _detect_mid_bay_hold(
		ro, all_services, raw_sessions, is_diag_to_supplement=is_diag_to_supplement
	):
		ro_needs_review = True
		ro_flag_reason = "Mid-Bay Hold Detected"

	# Session quality & minimum duration check
	if not ro_needs_review and sess_needs_review:
		ro_needs_review = True
		ro_flag_reason = sess_flag_reason or "Session Needs Review"

	if ro_net_active_hours < MIN_VALID_SESSION_HOURS:
		ro_needs_review = True
		if not ro_flag_reason:
			ro_flag_reason = "Session Too Short"

	# Detect paired symmetrical packages (same service_package with both LH and RH completed)
	pkg_positions: dict[str, set[str]] = {}
	for s in concrete_services:
		pkg_name = getattr(s, "service_package", None)
		pos = str(getattr(s, "service_position", "None") or "None").strip() or "None"
		if pkg_name and pos in ("LH", "RH"):
			pkg_positions.setdefault(pkg_name, set()).add(pos)
	paired_packages = {pkg for pkg, positions in pkg_positions.items() if "LH" in positions and "RH" in positions}

	# Resolve base weights, FRT hours, and overlap buckets
	default_labor_rate = _get_default_labor_rate()
	service_meta: list[dict[str, Any]] = []
	unique_group_codes: set[str] = set()
	has_zero_weight = False

	for s in concrete_services:
		pkg_name = getattr(s, "service_package", None)
		pkg_meta = pkg_map.get(pkg_name) if pkg_name else None
		base_weight, frt_hours = _compute_base_weight_and_frt(s, pkg_meta, default_labor_rate)
		if base_weight <= 0:
			has_zero_weight = True

		group_code, bucket_key = resolve_service_overlap_bucket(s, pkg_meta)
		if group_code:
			explicit_group = (pkg_meta or {}).get("overlap_group")
			corr_code = getattr(s, "correction_code", None) or (pkg_meta or {}).get("correction_code")
			_g_code, g_label, g_auto = derive_overlap_group_info(corr_code, explicit_group)
			ensure_overlap_group(group_code, g_label, is_auto_derived=g_auto)
			unique_group_codes.add(group_code)

		service_meta.append(
			{
				"row": s,
				"pkg_meta": pkg_meta,
				"base_weight": base_weight,
				"frt_hours": frt_hours,
				"overlap_group": group_code,
				"overlap_bucket_key": bucket_key,
			}
		)

	if has_zero_weight:
		ro_needs_review = True
		if not ro_flag_reason:
			ro_flag_reason = "Zero-Weight Line Present"

	overlap_factors = get_effective_overlap_factors(sorted(unique_group_codes))

	# Multi-tech scoping check
	contributing_sessions = [s for s in eval_sessions if flt(s["net_hours"]) > 0]
	log_techs = {s["technician"] for s in contributing_sessions}
	svc_techs = {getattr(m["row"], "assigned_technician", None) or None for m in service_meta}
	can_scope_per_tech = (
		bool(contributing_sessions)
		and None not in log_techs
		and None not in svc_techs
		and len(svc_techs) >= 2
		and svc_techs == log_techs
	)

	pools: list[dict[str, Any]] = []
	if can_scope_per_tech:
		tech_hours: dict[str, float] = {}
		for sess in contributing_sessions:
			tech = sess["technician"]
			tech_hours[tech] = tech_hours.get(tech, 0.0) + flt(sess["net_hours"])
		tech_items: dict[str, list[dict[str, Any]]] = {}
		for m in service_meta:
			tech = getattr(m["row"], "assigned_technician", None)
			tech_items.setdefault(tech, []).append(m)
		for tech, items in tech_items.items():
			method = ALLOC_SUPPLEMENT_POST_APPROVAL if is_diag_to_supplement else ALLOC_TECH_SCOPED
			pools.append(
				{
					"technician": tech,
					"pool_hours": flt(tech_hours.get(tech, 0.0), 4),
					"items": items,
					"method": method,
				}
			)
	else:
		if is_diag_to_supplement:
			method = ALLOC_SUPPLEMENT_POST_APPROVAL
		elif len(service_meta) == 1:
			method = ALLOC_SINGLE_JOB
		else:
			method = ALLOC_RO_WIDE

		single_log_tech = next(iter(log_techs)) if len(log_techs) == 1 and None not in log_techs else None
		pools.append(
			{
				"technician": single_log_tech,
				"pool_hours": ro_net_active_hours,
				"items": service_meta,
				"method": method,
			}
		)

	# Compute per-service allocations within each pool
	observed_at = (
		max(s["to_time"] for s in eval_sessions)
		if eval_sessions
		else (
			get_datetime(getattr(ro, "parked_at", None))
			or get_datetime(getattr(ro, "modified", None))
			or now_datetime()
		)
	)
	vehicle_model, vehicle_generation = _get_vehicle_context(ro)
	ro_service_count = len(concrete_services)

	computed_rows: list[dict[str, Any]] = []
	for pool in pools:
		items = pool["items"]
		pool_hours = flt(pool["pool_hours"], 4)
		pool_size = len(items)
		confidence = 1.0 if pool_size == 1 else (1.0 / float(pool_size))

		# Group items by same-corner overlap bucket within the pool
		bucket_map: dict[str, list[int]] = {}
		for idx, item in enumerate(items):
			bkey = item["overlap_bucket_key"]
			if bkey:
				bucket_map.setdefault(bkey, []).append(idx)

		item_weights: list[float] = [0.0] * pool_size
		item_primary: list[int] = [1] * pool_size
		item_factor: list[float] = [ALPHA_CLAMP_MAX] * pool_size
		item_bucket_count: list[int] = [1] * pool_size

		for idx, item in enumerate(items):
			bkey = item["overlap_bucket_key"]
			if not bkey or len(bucket_map.get(bkey, ())) < 2:
				item_weights[idx] = item["base_weight"]
				item_primary[idx] = 1
				item_factor[idx] = ALPHA_CLAMP_MAX
				item_bucket_count[idx] = 1

		for bkey, indices in bucket_map.items():
			if len(indices) < 2:
				continue
			group_code = items[indices[0]]["overlap_group"]
			alpha = flt(overlap_factors.get(group_code, ALPHA_CLAMP_MAX))
			if alpha <= 0 or alpha > ALPHA_CLAMP_MAX:
				alpha = ALPHA_CLAMP_MAX
			# Longest job in bucket is primary (tie-break by original order)
			primary_idx = max(indices, key=lambda i: (items[i]["base_weight"], -i))
			for i in indices:
				item_bucket_count[i] = len(indices)
				if i == primary_idx:
					item_primary[i] = 1
					item_factor[i] = ALPHA_CLAMP_MAX
					item_weights[i] = items[i]["base_weight"]
				else:
					item_primary[i] = 0
					item_factor[i] = alpha
					item_weights[i] = items[i]["base_weight"] * alpha

		total_weight = sum(item_weights)
		for idx, item in enumerate(items):
			s = item["row"]
			weight = item_weights[idx]
			if total_weight > 0:
				alloc_h = pool_hours * (weight / total_weight)
			else:
				alloc_h = (pool_hours / float(pool_size)) if pool_size > 0 else 0.0

			alloc_h_rounded = flt(alloc_h, 3)
			alloc_min_rounded = flt(alloc_h * 60.0, 1)
			frt_h = flt(item["frt_hours"], 3)

			frt_ratio = flt(alloc_h / frt_h, 3) if (frt_h > 0 and alloc_h > 0) else 0.0
			is_outlier = 0
			row_needs_review = ro_needs_review
			row_flag_reason = ro_flag_reason

			if frt_h > 0 and alloc_h > 0:
				if frt_ratio < settings.min_ratio or frt_ratio > settings.max_ratio:
					is_outlier = 1
					row_needs_review = True
					if not row_flag_reason:
						row_flag_reason = "Outlier Ratio"

			review_status = REVIEW_NEEDS_REVIEW if row_needs_review else REVIEW_CLEAN
			exclude_flag = 1 if row_needs_review else 0

			pos = str(getattr(s, "service_position", "None") or "None").strip() or "None"
			pkg_name = getattr(s, "service_package", None) or None
			tech = getattr(s, "assigned_technician", None) or pool["technician"] or None

			computed_rows.append(
				{
					"row": s,
					"values": {
						"repair_order": ro.name,
						"repair_order_service": s.name,
						"service_package": pkg_name,
						"service_title": getattr(s, "service_title", "") or "",
						"technician": tech,
						"vehicle_model": vehicle_model,
						"vehicle_generation": vehicle_generation,
						"service_position": pos,
						"is_paired_symmetrical": 1 if (pkg_name and pkg_name in paired_packages) else 0,
						"is_supplement": cint(getattr(s, "is_supplement", 0)),
						"observed_at": observed_at,
						"overlap_group": item["overlap_group"],
						"overlap_bucket_key": item["overlap_bucket_key"] or "",
						"same_group_job_count": item_bucket_count[idx],
						"is_overlap_primary": item_primary[idx],
						"applied_overlap_factor": flt(item_factor[idx], 3),
						"frt_hours": frt_h,
						"allocation_weight": flt(weight, 3),
						"allocated_hours": alloc_h_rounded,
						"allocated_minutes": alloc_min_rounded,
						"ro_net_active_hours": flt(ro_net_active_hours, 3),
						"ro_service_count": ro_service_count,
						"allocation_method": pool["method"],
						"confidence_score": flt(confidence, 3),
						"frt_ratio": frt_ratio,
						"review_status": review_status,
						"flag_reason": row_flag_reason,
						"is_outlier": is_outlier,
						"exclude_from_calibration": exclude_flag,
					},
				}
			)

	# Remove stale observations for services no longer completed/present
	active_row_names = {entry["row"].name for entry in computed_rows if getattr(entry["row"], "name", None)}
	_delete_stale_observations(ro.name, active_row_names)

	# Idempotent upsert keyed by (repair_order, repair_order_service)
	existing_rows = frappe.get_all(
		"Service Time Observation",
		filters={"repair_order": ro.name},
		fields=["name", "repair_order_service", "allocated_hours", "review_status", "exclude_from_calibration"],
	)
	existing_by_service = {r["repair_order_service"]: r for r in existing_rows}

	obs_names: list[str] = []
	for entry in computed_rows:
		s = entry["row"]
		vals = entry["values"]
		existing = existing_by_service.get(s.name)

		if existing:
			doc = frappe.get_doc("Service Time Observation", existing["name"])
			unchanged_hours = abs(flt(existing.get("allocated_hours")) - flt(vals["allocated_hours"])) <= 1e-3
			preserve_manager_decision = unchanged_hours and existing.get("review_status") in (
				REVIEW_CONFIRMED,
				REVIEW_EXCLUDED,
			)
			for k, v in vals.items():
				if preserve_manager_decision and k in ("review_status", "exclude_from_calibration"):
					continue
				setattr(doc, k, v)
			doc.flags.ignore_calibration_hook = True
			doc.save(ignore_permissions=True)
			obs_names.append(doc.name)
		else:
			doc = frappe.get_doc({"doctype": "Service Time Observation", **vals})
			doc.flags.ignore_calibration_hook = True
			doc.insert(ignore_permissions=True)
			obs_names.append(doc.name)

		# Sync allocated hours back to Repair Order Service without recursive ro.save()
		if getattr(s, "name", None):
			frappe.db.set_value(
				"Repair Order Service",
				s.name,
				"actual_duration_hours",
				vals["allocated_hours"],
				update_modified=False,
			)
			s.actual_duration_hours = vals["allocated_hours"]

	return obs_names

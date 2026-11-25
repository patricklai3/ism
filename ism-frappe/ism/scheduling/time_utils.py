# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Optional, Sequence, Union

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate

DEFAULT_SHOP_SETTINGS: Dict[str, Any] = {
	"operating_hours_start": timedelta(hours=8),
	"operating_hours_end": timedelta(hours=17),
	"break_start": timedelta(hours=12),
	"break_end": timedelta(hours=12, minutes=30),
	"default_slot_interval": 30,
	"scheduling_horizon_days": 30,
	"enable_technician_capacity": 1,
	"technician_designation": "Technician",
	"holiday_list": None,
	"default_diagnostic_duration": 60,
	"customer_arrival_offset_minutes": 15,
	"reschedule_cutoff_hours": 24,
	"max_self_reschedules": 2,
	"max_concurrent_bookings_per_phone": 2,
	"self_serve_default_entry_type": "Diagnostic",
	"self_serve_min_lead_time_minutes": 120,
	"self_serve_horizon_days": 30,
	"enable_waiter_preference": 1,
	"assignment_policy_mode": "Tiered Waterfall",
	"fairness_window_days": 7,
	"tier_spillover_threshold_pct": 85.0,
	"fairness_weight": 0.55,
	"skill_conservation_weight": 0.25,
	"skill_preference_weight": 0.12,
	"schedule_packing_weight": 0.08,
	"enable_morning_auto_rebalance": 0,
	"auto_rebalance_skew_threshold_pct": 25.0,
}


_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TIME_PATTERN = re.compile(
	r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$",
)


def normalize_date(val: Union[str, date, datetime, None]) -> Optional[str]:
	"""Validate and normalize an ISO YYYY-MM-DD date (or date/datetime object) into YYYY-MM-DD."""
	if val is None:
		return None
	if isinstance(val, (date, datetime)):
		return str(getdate(val))
	if not isinstance(val, str):
		frappe.throw(_("Invalid date value: '{0}'.").format(val), frappe.ValidationError)

	text = val.strip()
	if not text:
		return None

	if not _ISO_DATE_PATTERN.match(text):
		frappe.throw(
			_("Invalid date format: '{0}'. Expected ISO YYYY-MM-DD.").format(val),
			frappe.ValidationError,
		)

	try:
		return date.fromisoformat(text).isoformat()
	except ValueError:
		frappe.throw(
			_("Invalid date format: '{0}'. Expected ISO YYYY-MM-DD.").format(val),
			frappe.ValidationError,
		)


def normalize_time(val: Union[str, time, timedelta, int, float, None]) -> Optional[str]:
	"""Validate and normalize a 24-hour time string (HH:MM or HH:MM:SS) into HH:MM:SS."""
	if val is None:
		return None
	if isinstance(val, timedelta):
		total_seconds = max(0, int(val.total_seconds()))
		hours = (total_seconds // 3600) % 24
		minutes = (total_seconds % 3600) // 60
		seconds = total_seconds % 60
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	if isinstance(val, time):
		return f"{val.hour:02d}:{val.minute:02d}:{val.second:02d}"
	if isinstance(val, (int, float)):
		total_seconds = max(0, int(val))
		hours = (total_seconds // 3600) % 24
		minutes = (total_seconds % 3600) // 60
		seconds = total_seconds % 60
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	if not isinstance(val, str):
		frappe.throw(_("Invalid time value: '{0}'.").format(val), frappe.ValidationError)

	text = val.strip()
	if not text:
		return None

	match = _TIME_PATTERN.match(text)
	if not match:
		frappe.throw(
			_("Invalid time format: '{0}'. Expected 24-hour HH:MM or HH:MM:SS.").format(val),
			frappe.ValidationError,
		)

	hour = int(match.group(1))
	minute = int(match.group(2))
	second = int(match.group(3) or 0)

	if hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
		frappe.throw(
			_("Invalid 24-hour time: '{0}'. Expected HH:MM or HH:MM:SS.").format(val),
			frappe.ValidationError,
		)

	return f"{hour:02d}:{minute:02d}:{second:02d}"


def _log_warning(context: str, exc: Exception) -> None:
	try:
		frappe.logger("ism").warning(f"{context}: {exc}")
	except Exception:
		pass


def to_timedelta(val: Union[str, time, timedelta, int, float, None]) -> timedelta:
	"""Convert time strings, datetime.time, numeric seconds, or timedelta into timedelta."""
	if val is None:
		return timedelta(0)
	if isinstance(val, timedelta):
		return val
	if isinstance(val, time):
		return timedelta(hours=val.hour, minutes=val.minute, seconds=val.second)
	if isinstance(val, (int, float)):
		return timedelta(seconds=int(val))
	if isinstance(val, str):
		text = val.strip()
		if not text:
			return timedelta(0)
		parts = text.split(":")
		try:
			hours = int(parts[0])
			minutes = int(parts[1]) if len(parts) > 1 else 0
			seconds = int(float(parts[2])) if len(parts) > 2 else 0
			return timedelta(hours=hours, minutes=minutes, seconds=seconds)
		except (ValueError, IndexError) as exc:
			_log_warning(f"Unable to parse time value '{text}' in to_timedelta", exc)
			return timedelta(0)
	return timedelta(0)


def format_timedelta(td: timedelta) -> str:
	"""Format a timedelta into an HH:MM clock string."""
	total_seconds = int(td.total_seconds())
	hours = total_seconds // 3600
	minutes = (total_seconds % 3600) // 60
	return f"{hours:02d}:{minutes:02d}"


SHOP_SETTINGS_CACHE_KEY = "ism:shop_settings"
HOLIDAY_LIST_CACHE_PREFIX = "ism:holiday_list:"


def _get_cache():
	if getattr(getattr(frappe, "flags", None), "in_test", False):
		return None
	cache_fn = getattr(frappe, "cache", None)
	if not callable(cache_fn):
		return None
	try:
		return cache_fn()
	except Exception as exc:
		_log_warning("Failed to acquire frappe.cache()", exc)
		return None


def invalidate_shop_settings_cache(doc=None, method=None) -> None:
	"""Clear cached ISM Settings when ISM Settings is updated."""
	cache = _get_cache()
	if cache is not None:
		try:
			cache.delete_value(SHOP_SETTINGS_CACHE_KEY)
		except Exception as exc:
			_log_warning("Failed to invalidate shop settings cache", exc)


def invalidate_holiday_cache(doc=None, method=None) -> None:
	"""Clear cached Holiday List dates when a Holiday List is updated or deleted."""
	cache = _get_cache()
	if cache is None:
		return
	try:
		name = getattr(doc, "name", None) if doc is not None else None
		if name:
			cache.delete_value(f"{HOLIDAY_LIST_CACHE_PREFIX}{name}")
		elif hasattr(cache, "delete_keys"):
			cache.delete_keys(HOLIDAY_LIST_CACHE_PREFIX)
	except Exception as exc:
		_log_warning("Failed to invalidate holiday cache", exc)


def _has_time_value(val: Any) -> bool:
	return val is not None and str(val).strip() != ""


def get_shop_settings() -> Dict[str, Any]:
	"""Read cached shop scheduling defaults and normalized timedeltas from ISM Settings."""
	cache = _get_cache()
	if cache is not None:
		try:
			cached = cache.get_value(SHOP_SETTINGS_CACHE_KEY)
			if isinstance(cached, dict):
				return dict(cached)
		except Exception as exc:
			_log_warning("Failed to read cached shop settings", exc)

	settings = dict(DEFAULT_SHOP_SETTINGS)

	if not frappe.db.exists("DocType", "ISM Settings"):
		return settings

	try:
		doc = frappe.get_single("ISM Settings")
	except frappe.DoesNotExistError as exc:
		_log_warning("ISM Settings not found; using defaults", exc)
		return settings

	if _has_time_value(doc.operating_hours_start):
		settings["operating_hours_start"] = to_timedelta(doc.operating_hours_start)
	if _has_time_value(doc.operating_hours_end):
		settings["operating_hours_end"] = to_timedelta(doc.operating_hours_end)

	settings["break_start"] = to_timedelta(doc.break_start) if _has_time_value(doc.break_start) else None
	settings["break_end"] = to_timedelta(doc.break_end) if _has_time_value(doc.break_end) else None

	if getattr(doc, "default_slot_interval", None):
		settings["default_slot_interval"] = int(doc.default_slot_interval)
	if getattr(doc, "scheduling_horizon_days", None):
		settings["scheduling_horizon_days"] = int(doc.scheduling_horizon_days)
	if hasattr(doc, "enable_technician_capacity") and doc.enable_technician_capacity is not None:
		settings["enable_technician_capacity"] = cint(doc.enable_technician_capacity)
	if getattr(doc, "technician_designation", None):
		settings["technician_designation"] = doc.technician_designation
	if getattr(doc, "holiday_list", None):
		settings["holiday_list"] = doc.holiday_list
	if getattr(doc, "default_diagnostic_duration", None):
		settings["default_diagnostic_duration"] = int(doc.default_diagnostic_duration)

	raw_offset = getattr(doc, "customer_arrival_offset_minutes", None)
	if raw_offset is not None and raw_offset != "":
		settings["customer_arrival_offset_minutes"] = min(120, max(0, cint(raw_offset)))
	raw_cutoff = getattr(doc, "reschedule_cutoff_hours", None)
	if raw_cutoff is not None and raw_cutoff != "":
		settings["reschedule_cutoff_hours"] = max(0, cint(raw_cutoff))
	raw_max_reschedules = getattr(doc, "max_self_reschedules", None)
	if raw_max_reschedules is not None and raw_max_reschedules != "":
		settings["max_self_reschedules"] = max(0, cint(raw_max_reschedules))
	raw_max_concurrent_phone = getattr(doc, "max_concurrent_bookings_per_phone", None)
	if raw_max_concurrent_phone is not None and raw_max_concurrent_phone != "":
		settings["max_concurrent_bookings_per_phone"] = min(10, max(1, cint(raw_max_concurrent_phone)))
	if getattr(doc, "self_serve_default_entry_type", None):
		settings["self_serve_default_entry_type"] = doc.self_serve_default_entry_type
	raw_lead_time = getattr(doc, "self_serve_min_lead_time_minutes", None)
	if raw_lead_time is not None and raw_lead_time != "":
		settings["self_serve_min_lead_time_minutes"] = max(0, cint(raw_lead_time))
	if getattr(doc, "self_serve_horizon_days", None):
		settings["self_serve_horizon_days"] = min(180, max(1, cint(doc.self_serve_horizon_days)))
	raw_waiter = getattr(doc, "enable_waiter_preference", None)
	if raw_waiter is not None and raw_waiter != "":
		settings["enable_waiter_preference"] = 1 if cint(raw_waiter) else 0

	raw_policy_mode = (getattr(doc, "assignment_policy_mode", None) or "").strip()
	if raw_policy_mode in ("Tiered Waterfall", "Equal Balance", "Weighted Share"):
		settings["assignment_policy_mode"] = raw_policy_mode
	raw_fairness_window = getattr(doc, "fairness_window_days", None)
	if raw_fairness_window is not None and raw_fairness_window != "":
		settings["fairness_window_days"] = min(30, max(1, cint(raw_fairness_window)))
	raw_spillover = getattr(doc, "tier_spillover_threshold_pct", None)
	if raw_spillover is not None and raw_spillover != "":
		settings["tier_spillover_threshold_pct"] = min(100.0, max(0.0, flt(raw_spillover)))
	for weight_key in (
		"fairness_weight",
		"skill_conservation_weight",
		"skill_preference_weight",
		"schedule_packing_weight",
	):
		raw_weight = getattr(doc, weight_key, None)
		if raw_weight is not None and raw_weight != "":
			settings[weight_key] = max(0.0, flt(raw_weight))
	raw_auto_rebal = getattr(doc, "enable_morning_auto_rebalance", None)
	if raw_auto_rebal is not None and raw_auto_rebal != "":
		settings["enable_morning_auto_rebalance"] = 1 if cint(raw_auto_rebal) else 0
	raw_skew = getattr(doc, "auto_rebalance_skew_threshold_pct", None)
	if raw_skew is not None and raw_skew != "":
		settings["auto_rebalance_skew_threshold_pct"] = min(100.0, max(0.0, flt(raw_skew)))

	if cache is not None:
		try:
			cache.set_value(SHOP_SETTINGS_CACHE_KEY, dict(settings), expires_in_sec=300)
		except Exception as exc:
			_log_warning("Failed to cache shop settings", exc)

	return settings


def effective_end_time(
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	break_start: Union[str, time, timedelta, None] = None,
	break_end: Union[str, time, timedelta, None] = None,
) -> timedelta:
	"""Return clock end time for a duration, pushing across the shop break window if needed."""
	start_td = to_timedelta(start_time)
	duration_td = timedelta(minutes=float(duration_minutes or 0))

	if break_start is None or break_start == "" or break_end is None or break_end == "":
		settings = get_shop_settings()
		bs_td = settings["break_start"] if (break_start is None or break_start == "") else to_timedelta(break_start)
		be_td = settings["break_end"] if (break_end is None or break_end == "") else to_timedelta(break_end)
	else:
		bs_td = to_timedelta(break_start)
		be_td = to_timedelta(break_end)

	if bs_td and be_td and be_td > bs_td:
		break_duration = be_td - bs_td
		if bs_td <= start_td < be_td:
			return be_td + duration_td
		if start_td < bs_td and (start_td + duration_td) > bs_td:
			return start_td + duration_td + break_duration

	return start_td + duration_td


def _default_is_holiday(target_date: date, holiday_list: Optional[str]) -> bool:
	if not holiday_list:
		return False
	cache = _get_cache()
	cache_key = f"{HOLIDAY_LIST_CACHE_PREFIX}{holiday_list}"
	if cache is not None:
		try:
			cached_dates = cache.get_value(cache_key)
			if isinstance(cached_dates, (list, set, tuple)):
				return str(target_date) in cached_dates
		except Exception as exc:
			_log_warning("Failed to read holiday list cache", exc)

	if not frappe.db.exists("DocType", "Holiday"):
		return False

	rows = frappe.get_all(
		"Holiday",
		filters={"parent": holiday_list},
		fields=["holiday_date"],
	)
	holiday_dates = sorted({str(r["holiday_date"]) for r in rows if r.get("holiday_date")})
	if cache is not None:
		try:
			cache.set_value(cache_key, holiday_dates, expires_in_sec=3600)
		except Exception as exc:
			_log_warning("Failed to write holiday list cache", exc)
	return str(target_date) in holiday_dates


def _consume_window_minutes(
	segments: list[Dict[str, Any]],
	curr_date: date,
	curr_time: timedelta,
	window_end: timedelta,
	rem_minutes: float,
) -> tuple[timedelta, float, bool]:
	"""Consume working minutes within [curr_time, window_end]; return (next_time, remaining, completed)."""
	avail_minutes = (window_end - curr_time).total_seconds() / 60.0
	if avail_minutes <= 0:
		return curr_time, rem_minutes, False

	if rem_minutes <= avail_minutes:
		seg_end = curr_time + timedelta(minutes=rem_minutes)
		segments.append({
			"date": str(curr_date),
			"start_time": format_timedelta(curr_time),
			"end_time": format_timedelta(seg_end),
			"duration": rem_minutes,
		})
		return seg_end, 0.0, True

	segments.append({
		"date": str(curr_date),
		"start_time": format_timedelta(curr_time),
		"end_time": format_timedelta(window_end),
		"duration": avail_minutes,
	})
	return window_end, rem_minutes - avail_minutes, False


def calculate_working_span(
	start_date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
) -> Dict[str, Any]:
	"""Calculate active working segments and completion datetime across lunch, overnight, and holidays."""
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings["break_start"]
	be_td = settings["break_end"]

	s_date = getdate(start_date)
	s_time = to_timedelta(start_time)
	duration_val = float(duration_minutes or 0)
	if duration_val <= 0:
		duration_val = float(settings.get("default_diagnostic_duration") or 60)

	if not op_start or not op_end or op_start >= op_end:
		end_td = s_time + timedelta(minutes=duration_val)
		end_clock = format_timedelta(end_td)
		start_clock = format_timedelta(s_time)
		return {
			"start_date": str(s_date),
			"start_time": start_clock,
			"completion_date": str(s_date),
			"completion_time": end_clock,
			"spans_days": 1,
			"is_multi_day": False,
			"spans_lunch": False,
			"segments": [{
				"date": str(s_date),
				"start_time": start_clock,
				"end_time": end_clock,
				"duration": duration_val,
			}],
			"bay_window": {
				"start_date": str(s_date),
				"start_time": start_clock,
				"end_date": str(s_date),
				"end_time": end_clock,
			},
		}

	curr_date = s_date
	curr_time = s_time
	if curr_time < op_start:
		curr_time = op_start
	elif curr_time >= op_end:
		curr_date = add_days(curr_date, 1)
		curr_time = op_start

	holiday_list = settings.get("holiday_list")
	has_lunch = bool(bs_td and be_td and op_start < bs_td < be_td < op_end)
	rem_minutes = duration_val
	segments: list[Dict[str, Any]] = []
	spans_lunch = False
	days_checked = 0

	while rem_minutes > 0 and days_checked < 60:
		days_checked += 1
		is_hol = is_holiday_fn(curr_date) if is_holiday_fn else _default_is_holiday(curr_date, holiday_list)
		if is_hol:
			curr_date = add_days(curr_date, 1)
			curr_time = op_start
			continue

		if has_lunch and bs_td <= curr_time < be_td:
			curr_time = be_td
			spans_lunch = True

		if has_lunch and curr_time < bs_td:
			curr_time, rem_minutes, completed = _consume_window_minutes(
				segments, curr_date, curr_time, bs_td, rem_minutes
			)
			if completed:
				break
			curr_time = be_td
			spans_lunch = True

		if rem_minutes > 0 and curr_time < op_end:
			curr_time, rem_minutes, completed = _consume_window_minutes(
				segments, curr_date, curr_time, op_end, rem_minutes
			)
			if completed:
				break

		curr_date = add_days(curr_date, 1)
		curr_time = op_start

	if rem_minutes > 0:
		_log_warning(
			"calculate_working_span reached 60-day horizon with remaining minutes",
			ValueError(f"{rem_minutes} minutes unallocated from {s_date}"),
		)

	spans_days = (curr_date - s_date).days + 1
	is_multi = spans_days > 1 or curr_date > s_date
	start_clock = format_timedelta(s_time)
	comp_clock = format_timedelta(curr_time)

	return {
		"start_date": str(s_date),
		"start_time": start_clock,
		"completion_date": str(curr_date),
		"completion_time": comp_clock,
		"spans_days": spans_days,
		"is_multi_day": is_multi,
		"spans_lunch": spans_lunch,
		"segments": segments,
		"bay_window": {
			"start_date": str(s_date),
			"start_time": start_clock,
			"end_date": str(curr_date),
			"end_time": comp_clock,
		},
	}


def _coerce_datetime(val: Union[datetime, str, None]) -> Optional[datetime]:
	if val is None or val == "":
		return None
	if isinstance(val, datetime):
		return val
	return get_datetime(val)


def subtract_intervals(
	base_start: datetime,
	base_end: datetime,
	exclude_intervals: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
	"""Subtract a sequence of [start, end] intervals from [base_start, base_end].

	Returns sorted, disjoint remaining (start, end) datetime spans with end > start.
	"""
	start_dt = _coerce_datetime(base_start)
	end_dt = _coerce_datetime(base_end)
	if start_dt is None or end_dt is None or end_dt <= start_dt:
		return []

	clipped: list[tuple[datetime, datetime]] = []
	for interval in exclude_intervals or ():
		if not interval or len(interval) < 2:
			continue
		ex_start = _coerce_datetime(interval[0])
		ex_end = _coerce_datetime(interval[1])
		if ex_start is None or ex_end is None or ex_end <= ex_start:
			continue
		clamped_s = max(ex_start, start_dt)
		clamped_e = min(ex_end, end_dt)
		if clamped_e > clamped_s:
			clipped.append((clamped_s, clamped_e))

	if not clipped:
		return [(start_dt, end_dt)]

	clipped.sort(key=lambda item: (item[0], item[1]))
	merged: list[tuple[datetime, datetime]] = [clipped[0]]
	for ex_s, ex_e in clipped[1:]:
		prev_s, prev_e = merged[-1]
		if ex_s <= prev_e:
			merged[-1] = (prev_s, max(prev_e, ex_e))
		else:
			merged.append((ex_s, ex_e))

	remaining: list[tuple[datetime, datetime]] = []
	cursor = start_dt
	for ex_s, ex_e in merged:
		if ex_s > cursor:
			remaining.append((cursor, ex_s))
		if ex_e > cursor:
			cursor = ex_e
	if cursor < end_dt:
		remaining.append((cursor, end_dt))
	return remaining


def calculate_net_working_session(
	start_dt: datetime,
	end_dt: datetime,
	*,
	settings: Optional[Dict[str, Any]] = None,
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
	max_single_session_hours: float = 8.5,
	exclude_intervals: Optional[Sequence[tuple[datetime, datetime]]] = None,
) -> Dict[str, Any]:
	"""Compute raw_hours, net_hours, deducted_overlap_hours, spans_overnight, needs_review, flag_reason.

	- Same-day (`start_dt.date() == end_dt.date()`): subtract `exclude_intervals` AND configured
	  shop lunch break (`break_start..break_end`), preserving early-start, past-closing, and
	  weekend/holiday overtime.
	- Multi-day (`start_dt.date() != end_dt.date()`): subtract `exclude_intervals`, clip remaining
	  spans to configured `operating_hours_start..operating_hours_end` minus lunch on non-holiday
	  days, and set `spans_overnight=True`, `needs_review=True`,
	  `flag_reason="Overnight / Multi-Day Session"`.
	- If `net_hours > max_single_session_hours`: set `needs_review=True` and include
	  `"Oversized Session"` in `flag_reason`.
	"""
	s_dt = _coerce_datetime(start_dt)
	e_dt = _coerce_datetime(end_dt)
	if s_dt is None or e_dt is None or e_dt <= s_dt:
		return {
			"raw_hours": 0.0,
			"net_hours": 0.0,
			"deducted_overlap_hours": 0.0,
			"spans_overnight": False,
			"needs_review": False,
			"flag_reason": "",
		}

	cfg = settings if settings is not None else get_shop_settings()
	raw_op_start = cfg.get("operating_hours_start")
	raw_op_end = cfg.get("operating_hours_end")
	raw_bs = cfg.get("break_start")
	raw_be = cfg.get("break_end")

	op_start = to_timedelta(raw_op_start) if _has_time_value(raw_op_start) else None
	op_end = to_timedelta(raw_op_end) if _has_time_value(raw_op_end) else None
	bs_td = to_timedelta(raw_bs) if _has_time_value(raw_bs) else None
	be_td = to_timedelta(raw_be) if _has_time_value(raw_be) else None
	has_lunch = bool(bs_td is not None and be_td is not None and be_td > bs_td)
	holiday_list = cfg.get("holiday_list")

	raw_seconds = (e_dt - s_dt).total_seconds()
	spans_overnight = s_dt.date() != e_dt.date()

	base_working_spans: list[tuple[datetime, datetime]] = []
	if not spans_overnight:
		day_midnight = datetime.combine(s_dt.date(), time.min)
		lunch_exclusions = [(day_midnight + bs_td, day_midnight + be_td)] if has_lunch else []
		base_working_spans = subtract_intervals(s_dt, e_dt, lunch_exclusions)
	else:
		has_op_hours = bool(op_start is not None and op_end is not None and op_end > op_start)
		curr_date = s_dt.date()
		last_date = e_dt.date()
		while curr_date <= last_date:
			is_hol = is_holiday_fn(curr_date) if is_holiday_fn else _default_is_holiday(curr_date, holiday_list)
			if not is_hol:
				day_midnight = datetime.combine(curr_date, time.min)
				if has_op_hours:
					win_start = max(s_dt, day_midnight + op_start)
					win_end = min(e_dt, day_midnight + op_end)
				else:
					win_start = max(s_dt, day_midnight)
					win_end = min(e_dt, day_midnight + timedelta(days=1))

				if win_end > win_start:
					lunch_exclusions = [(day_midnight + bs_td, day_midnight + be_td)] if has_lunch else []
					base_working_spans.extend(subtract_intervals(win_start, win_end, lunch_exclusions))
			curr_date = add_days(curr_date, 1)

	if exclude_intervals:
		final_spans: list[tuple[datetime, datetime]] = []
		for span_s, span_e in base_working_spans:
			final_spans.extend(subtract_intervals(span_s, span_e, exclude_intervals))
	else:
		final_spans = base_working_spans

	base_working_seconds = sum((e - s).total_seconds() for s, e in base_working_spans)
	net_seconds = sum((e - s).total_seconds() for s, e in final_spans)
	deducted_overlap_seconds = max(0.0, base_working_seconds - net_seconds)

	raw_hours = round(raw_seconds / 3600.0, 3)
	net_hours = round(net_seconds / 3600.0, 3)
	deducted_overlap_hours = round(deducted_overlap_seconds / 3600.0, 3)

	reasons: list[str] = []
	needs_review = False
	if spans_overnight:
		needs_review = True
		reasons.append("Overnight / Multi-Day Session")
	if max_single_session_hours and float(max_single_session_hours) > 0 and net_hours > float(max_single_session_hours):
		needs_review = True
		reasons.append("Oversized Session")

	return {
		"raw_hours": raw_hours,
		"net_hours": net_hours,
		"deducted_overlap_hours": deducted_overlap_hours,
		"spans_overnight": spans_overnight,
		"needs_review": needs_review,
		"flag_reason": "; ".join(reasons),
	}

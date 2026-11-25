# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Per-technician weekly shift windows and AM/PM half-day leave helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Optional, Union

from frappe.utils import add_days, cint, getdate

from ism.scheduling.time_utils import get_shop_settings, normalize_date, to_timedelta

DAY_NAMES: tuple[str, ...] = (
	"Monday",
	"Tuesday",
	"Wednesday",
	"Thursday",
	"Friday",
	"Saturday",
	"Sunday",
)


def _has_time_value(val: Any) -> bool:
	return val is not None and str(val).strip() != ""


def _lookup_day_schedule(profile: Optional[Mapping[str, Any]], day_name: str) -> Optional[Mapping[str, Any]]:
	if not profile:
		return None
	weekly = profile.get("weekly_schedule")
	if not weekly:
		return None
	if isinstance(weekly, Mapping):
		return weekly.get(day_name)
	for row in weekly:
		row_day = row.get("day_of_week") if isinstance(row, Mapping) else getattr(row, "day_of_week", None)
		if (row_day or "").strip() == day_name:
			if isinstance(row, Mapping):
				return row
			return {
				"day_of_week": row_day,
				"is_working_day": getattr(row, "is_working_day", 1),
				"shift_start": getattr(row, "shift_start", None),
				"shift_end": getattr(row, "shift_end", None),
				"break_start": getattr(row, "break_start", None),
				"break_end": getattr(row, "break_end", None),
			}
	return None


def _compute_net_minutes(
	start: timedelta,
	end: timedelta,
	break_start: Optional[timedelta],
	break_end: Optional[timedelta],
) -> int:
	if end <= start:
		return 0
	gross_seconds = (end - start).total_seconds()
	if break_start is not None and break_end is not None and start < break_start < break_end < end:
		gross_seconds -= (break_end - break_start).total_seconds()
	return max(0, int(gross_seconds // 60))


def get_technician_shift_window(
	profile: Optional[dict[str, Any]],
	target_date: Union[str, date, datetime],
	settings: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
	"""Return {'start': timedelta, 'end': timedelta, 'break_start': Optional[timedelta], 'break_end': Optional[timedelta], 'net_minutes': int} or None if off-day."""
	shop = settings if settings is not None else get_shop_settings()
	target_d = getdate(normalize_date(target_date) or target_date)
	day_name = DAY_NAMES[target_d.weekday()]

	day_row = _lookup_day_schedule(profile, day_name)
	if day_row is not None and not cint(day_row.get("is_working_day", 1)):
		return None

	raw_start = day_row.get("shift_start") if day_row is not None else None
	raw_end = day_row.get("shift_end") if day_row is not None else None

	start_td = (
		to_timedelta(raw_start)
		if _has_time_value(raw_start)
		else to_timedelta(shop.get("operating_hours_start"))
	)
	end_td = (
		to_timedelta(raw_end)
		if _has_time_value(raw_end)
		else to_timedelta(shop.get("operating_hours_end"))
	)

	if end_td <= start_td:
		return None

	raw_bstart = day_row.get("break_start") if day_row is not None else None
	raw_bend = day_row.get("break_end") if day_row is not None else None

	if _has_time_value(raw_bstart) and _has_time_value(raw_bend):
		break_start: Optional[timedelta] = to_timedelta(raw_bstart)
		break_end: Optional[timedelta] = to_timedelta(raw_bend)
	elif not _has_time_value(raw_bstart) and not _has_time_value(raw_bend):
		shop_bs = shop.get("break_start")
		shop_be = shop.get("break_end")
		break_start = to_timedelta(shop_bs) if _has_time_value(shop_bs) else None
		break_end = to_timedelta(shop_be) if _has_time_value(shop_be) else None
	else:
		break_start = None
		break_end = None

	if not (break_start is not None and break_end is not None and start_td < break_start < break_end < end_td):
		break_start = None
		break_end = None

	return {
		"start": start_td,
		"end": end_td,
		"break_start": break_start,
		"break_end": break_end,
		"net_minutes": _compute_net_minutes(start_td, end_td, break_start, break_end),
	}


def apply_leave_to_shift_window(
	shift: Optional[dict[str, Any]],
	emp_leaves: Sequence[dict[str, Any]],
	target_date: Union[str, date, datetime],
) -> Optional[dict[str, Any]]:
	"""Apply full-day or AM/PM half-day approved Leave Application to shift window.

	- Full-day leave returns None.
	- AM half-day blocks [shift_start, break_start) -> returns afternoon window [break_end, shift_end).
	- PM half-day blocks [break_end, shift_end) -> returns morning window [shift_start, break_start).
	- Half-day leave with blank period conservatively blocks the full day (returns None).
	"""
	if not shift:
		return None
	if not emp_leaves:
		return dict(shift)

	target_str = normalize_date(target_date) or str(getdate(target_date))
	blocked_am = False
	blocked_pm = False

	for leave in emp_leaves:
		from_str = normalize_date(leave.get("from_date")) or str(leave.get("from_date") or "")
		to_str = normalize_date(leave.get("to_date")) or str(leave.get("to_date") or "")
		if from_str and to_str and not (from_str <= target_str <= to_str):
			continue

		if not cint(leave.get("half_day")):
			return None

		half_day_raw = leave.get("half_day_date")
		half_day_str = (normalize_date(half_day_raw) or str(half_day_raw)) if half_day_raw else None
		if half_day_str and half_day_str != target_str:
			# On a multi-day leave with half_day=1, dates other than half_day_date are full days off.
			return None

		period = (
			leave.get("ism_half_day_period")
			or leave.get("half_day_period")
			or ""
		)
		period_norm = str(period).strip().upper()
		if period_norm == "AM":
			blocked_am = True
		elif period_norm == "PM":
			blocked_pm = True
		else:
			# Blank half-day period conservatively blocks full day.
			return None

	if not blocked_am and not blocked_pm:
		return dict(shift)
	if blocked_am and blocked_pm:
		return None

	start_td: timedelta = shift["start"]
	end_td: timedelta = shift["end"]
	break_start: Optional[timedelta] = shift.get("break_start")
	break_end: Optional[timedelta] = shift.get("break_end")

	if break_start is not None and break_end is not None and start_td < break_start < break_end < end_td:
		am_end = break_start
		pm_start = break_end
	else:
		half_mins = int((end_td - start_td).total_seconds() // 120)
		midpoint = start_td + timedelta(minutes=half_mins)
		am_end = midpoint
		pm_start = midpoint

	if blocked_am:
		new_start, new_end = pm_start, end_td
	else:
		new_start, new_end = start_td, am_end

	if new_end <= new_start:
		return None

	return {
		"start": new_start,
		"end": new_end,
		"break_start": None,
		"break_end": None,
		"net_minutes": _compute_net_minutes(new_start, new_end, None, None),
	}


def is_interval_within_shift(
	start_td: timedelta,
	end_td: timedelta,
	shift: Optional[dict[str, Any]],
) -> bool:
	"""Return True iff [start_td, end_td) is non-empty and fits completely inside shift['start']..shift['end']."""
	if not shift:
		return False
	s_td = to_timedelta(start_td)
	e_td = to_timedelta(end_td)
	if e_td <= s_td:
		return False
	return shift["start"] <= s_td and e_td <= shift["end"]


def calculate_technician_available_hours(
	profile: Optional[dict[str, Any]],
	emp_leaves: Sequence[dict[str, Any]],
	start_date: Union[str, date, datetime],
	end_date: Union[str, date, datetime],
	*,
	is_holiday_fn: Optional[Callable[[Any], bool]] = None,
	settings: Optional[dict[str, Any]] = None,
) -> float:
	"""Sum net available working hours across [start_date, end_date] inclusive, respecting shifts, holidays, and leaves."""
	s_date = getdate(normalize_date(start_date) or start_date)
	e_date = getdate(normalize_date(end_date) or end_date)
	if e_date < s_date:
		return 0.0

	shop = settings if settings is not None else get_shop_settings()
	total_minutes = 0
	curr = s_date
	while curr <= e_date:
		if not (is_holiday_fn and is_holiday_fn(curr)):
			shift = get_technician_shift_window(profile, curr, settings=shop)
			effective = apply_leave_to_shift_window(shift, emp_leaves, curr)
			if effective:
				total_minutes += int(effective.get("net_minutes") or 0)
		curr = add_days(curr, 1)

	return round(total_minutes / 60.0, 4)

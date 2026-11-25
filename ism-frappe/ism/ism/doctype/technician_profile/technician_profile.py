# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ism.scheduling.time_utils import to_timedelta

VALID_SKILL_ROLES = ("Preferred", "Standard", "Backup")
VALID_DAYS_OF_WEEK = (
	"Monday",
	"Tuesday",
	"Wednesday",
	"Thursday",
	"Friday",
	"Saturday",
	"Sunday",
)


def _has_time(val) -> bool:
	return val is not None and str(val).strip() != ""


class TechnicianProfile(Document):
	def validate(self):
		self._validate_tier_and_weights()
		self._validate_skills()
		self._validate_weekly_schedule()

	def _validate_tier_and_weights(self):
		tier = cint(self.assignment_tier)
		if tier < 1:
			frappe.throw(
				_("Assignment Tier must be 1 or greater (1=Workhorse, 2=Buffer, 3=Reserve)."),
				title=_("Invalid Assignment Tier"),
			)
		self.assignment_tier = tier

		weight = flt(self.target_load_weight)
		if weight <= 0:
			frappe.throw(
				_("Target Load Weight must be greater than 0."),
				title=_("Invalid Target Load Weight"),
			)
		self.target_load_weight = weight

		if self.daily_soft_cap_pct is not None and self.daily_soft_cap_pct != "":
			cap = flt(self.daily_soft_cap_pct)
			if cap < 0 or cap > 100:
				frappe.throw(
					_("Daily Soft Cap (%) must be between 0 and 100."),
					title=_("Invalid Daily Soft Cap"),
				)

	def _validate_skills(self):
		seen_skills: set[str] = set()
		for row in self.skills or []:
			skill_name = (getattr(row, "skill", None) or "").strip()
			if not skill_name:
				frappe.throw(
					_("Skill is required at row #{0}.").format(row.idx),
					title=_("Missing Skill"),
				)
			row.skill = skill_name
			if skill_name in seen_skills:
				frappe.throw(
					_("Duplicate skill '{0}' at row #{1}.").format(skill_name, row.idx),
					title=_("Duplicate Skill"),
				)
			seen_skills.add(skill_name)

			raw_diff = getattr(row, "max_difficulty", None)
			if raw_diff is None or raw_diff == "":
				diff = 0
			else:
				try:
					diff_float = float(raw_diff)
				except (TypeError, ValueError):
					frappe.throw(
						_("Max Difficulty must be an integer between 0 and 3 at row #{0}.").format(row.idx),
						title=_("Invalid Difficulty"),
					)
				if diff_float != int(diff_float) or int(diff_float) not in (0, 1, 2, 3):
					frappe.throw(
						_("Max Difficulty must be between 0 and 3 at row #{0}.").format(row.idx),
						title=_("Invalid Difficulty"),
					)
				diff = int(diff_float)
			row.max_difficulty = diff

			role = (getattr(row, "skill_role", None) or "Standard").strip()
			if role not in VALID_SKILL_ROLES:
				frappe.throw(
					_("Invalid Skill Role '{0}' at row #{1}. Expected one of: {2}.").format(
						role, row.idx, ", ".join(VALID_SKILL_ROLES)
					),
					title=_("Invalid Skill Role"),
				)
			row.skill_role = role

	def _validate_weekly_schedule(self):
		seen_days: set[str] = set()
		for row in self.weekly_schedule or []:
			day = (getattr(row, "day_of_week", None) or "").strip()
			if day not in VALID_DAYS_OF_WEEK:
				frappe.throw(
					_("Invalid Day of Week '{0}' at row #{1}.").format(day, row.idx),
					title=_("Invalid Day of Week"),
				)
			row.day_of_week = day
			if day in seen_days:
				frappe.throw(
					_("Duplicate Day of Week '{0}' at row #{1}.").format(day, row.idx),
					title=_("Duplicate Shift Day"),
				)
			seen_days.add(day)

			has_start = _has_time(getattr(row, "shift_start", None))
			has_end = _has_time(getattr(row, "shift_end", None))
			if has_start and has_end:
				start_td = to_timedelta(row.shift_start)
				end_td = to_timedelta(row.shift_end)
				if start_td >= end_td:
					frappe.throw(
						_("Shift Start must be before Shift End for {0} (row #{1}).").format(
							day, row.idx
						),
						title=_("Invalid Shift Window"),
					)

			has_bstart = _has_time(getattr(row, "break_start", None))
			has_bend = _has_time(getattr(row, "break_end", None))
			if has_bstart and has_bend:
				bstart_td = to_timedelta(row.break_start)
				bend_td = to_timedelta(row.break_end)
				if bstart_td >= bend_td:
					frappe.throw(
						_("Break Start must be before Break End for {0} (row #{1}).").format(
							day, row.idx
						),
						title=_("Invalid Break Window"),
					)

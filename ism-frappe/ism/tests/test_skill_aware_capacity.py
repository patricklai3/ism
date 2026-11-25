# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 2A tests for skill- and shift-aware capacity gating, AM/PM half-day leave,
unassigned-entry headcount accounting, and bookable_catalog skill requirements."""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, getdate, today

from ism.api.technician_availability import (
	check_technician_availability,
	get_active_technicians,
	get_technician_pool_availability,
)
from ism.scheduling.bookable_catalog import (
	compute_packages_booking_requirements,
	list_bookable_packages,
)
from ism.scheduling.capacity import ShopCapacityWindow
from ism.scheduling.entry_queries import (
	fetch_leaves_by_employee,
	is_employee_on_leave_for_date,
)
from ism.scheduling.shifts import DAY_NAMES
from ism.scheduling.slot_search import find_next_available_slot_batch, search_day_slots
from ism.setup_tasks import setup_leave_custom_fields
from ism.tests.base import ISMTestCase, TEST_PREFIX

PUBLIC_CARD_KEYS = {
	"package_id",
	"title",
	"summary",
	"applicable_model",
	"applicable_generation",
	"sort_order",
	"price_display_mode",
	"currency",
	"price_min",
	"price_max",
	"price_display_label",
}


class TestSkillAwareCapacity(ISMTestCase):
	"""Verify Wave 2A capacity engine upgrades across capacity, slot_search, availability & catalog."""

	def setUp(self):
		super().setUp()
		setup_leave_custom_fields()
		self.manifest = self.get_manifest()
		self.marcus = self.manifest.techs["marcus"]
		self.elena = self.manifest.techs["elena"]
		self.devon = self.manifest.techs["devon"]
		self.noah = self.manifest.techs["noah"]
		self.bay1 = self.manifest.bays["main_lift"]
		self.bay2 = self.manifest.bays["alignment"]
		self.bay3 = self.manifest.bays["hv_diagnostics"]
		self.bay4 = self.manifest.bays["tire_quick"]
		self.customer = self.manifest.customers["retail_1"]
		self._saved_profiles: list[tuple[str, list[dict], int]] = []
		self._set_ism_settings(
			operating_hours_start="08:00:00",
			operating_hours_end="17:00:00",
			break_start="12:00:00",
			break_end="12:30:00",
			default_slot_interval=30,
			enable_technician_capacity=1,
			holiday_list=None,
		)

	def tearDown(self):
		for emp_id, orig_schedule, orig_active in reversed(self._saved_profiles):
			if frappe.db.exists("Technician Profile", emp_id):
				prof = frappe.get_doc("Technician Profile", emp_id)
				prof.is_active = orig_active
				prof.set("weekly_schedule", orig_schedule)
				prof.save(ignore_permissions=True)
		super().tearDown()

	def _set_ism_settings(self, **kwargs):
		settings = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			setattr(settings, key, val)
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype="ISM Settings")

	def _create_distinct_ro(self):
		veh = self.create_test_vehicle(customer=self.customer)
		return self.create_test_repair_order(
			customer=self.customer,
			vehicle=veh.name,
			odometer_in=45000,
		)

	def _create_approved_leave(
		self,
		employee: str,
		from_date: str,
		to_date: str | None = None,
		*,
		half_day: int = 0,
		half_day_date: str | None = None,
		half_day_period: str | None = None,
	):
		leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"
		payload = {
			"doctype": "Leave Application",
			"employee": employee,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date or from_date,
			"half_day": half_day,
			"status": "Approved",
			"company": self.manifest.company,
		}
		if half_day:
			payload["half_day_date"] = half_day_date or from_date
		if half_day_period is not None and frappe.db.has_column("Leave Application", "ism_half_day_period"):
			payload["ism_half_day_period"] = half_day_period
		leave = frappe.get_doc(payload)
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		self.track(leave)
		updates = {"docstatus": 1, "status": "Approved"}
		if half_day_period is not None and frappe.db.has_column("Leave Application", "ism_half_day_period"):
			updates["ism_half_day_period"] = half_day_period
		frappe.db.set_value("Leave Application", leave.name, updates)
		return leave

	def _mutate_profile(self, employee: str, *, weekly_schedule=None, is_active=None):
		prof = frappe.get_doc("Technician Profile", employee)
		orig_schedule = [
			{
				"day_of_week": r.day_of_week,
				"is_working_day": r.is_working_day,
				"shift_start": r.shift_start,
				"shift_end": r.shift_end,
				"break_start": r.break_start,
				"break_end": r.break_end,
			}
			for r in (prof.weekly_schedule or [])
		]
		self._saved_profiles.append((employee, orig_schedule, int(prof.is_active)))
		if is_active is not None:
			prof.is_active = is_active
		if weekly_schedule is not None:
			prof.set("weekly_schedule", weekly_schedule)
		prof.save(ignore_permissions=True)
		return prof

	def test_unassigned_entries_prevent_technician_overbooking(self):
		"""2 on-duty techs, 4 active bays, 2 unassigned blocking entries -> 3rd overlapping slot blocked."""
		target_date = add_days(today(), 20)

		# Leave devon and noah off duty so exactly 2 techs (marcus, elena) are on duty across 4 active bays
		self._create_approved_leave(self.devon, target_date)
		self._create_approved_leave(self.noah, target_date)

		# 1st unassigned blocking entry on bay1 at 09:00-10:00
		ro1 = self._create_distinct_ro()
		se1 = self.track(
			self.create_test_schedule_entry(
				repair_order=ro1.name,
				scheduled_date=target_date,
				scheduled_time="09:00:00",
				estimated_duration=60,
				service_bay=self.bay1,
				assigned_technician=None,
			)
		)

		win_one = ShopCapacityWindow(target_date, target_date)
		slots_one = {s["start_time"]: s for s in win_one.slots_for_date(target_date, 60)}
		self.assertIn("09:00", slots_one)
		self.assertEqual(slots_one["09:00"]["available_bays"], 3)
		self.assertEqual(slots_one["09:00"]["available_technicians"], 1)
		self.assertEqual(slots_one["09:00"]["effective_capacity"], 1)
		self.assertEqual(slots_one["09:00"]["bottleneck"], "technician")

		pool_one = get_technician_pool_availability(target_date, "09:00:00", 60)
		self.assertTrue(pool_one["is_available"])
		self.assertEqual(pool_one["on_duty"], 2)
		self.assertEqual(pool_one["unassigned_occupied"], 1)
		self.assertEqual(pool_one["available"], 1)

		# 2nd unassigned blocking entry on bay2 at 09:00-10:00 -> exhausts the 2 on-duty techs while 2 bays stay free
		ro2 = self._create_distinct_ro()
		se2 = self.track(
			self.create_test_schedule_entry(
				repair_order=ro2.name,
				scheduled_date=target_date,
				scheduled_time="09:00:00",
				estimated_duration=60,
				service_bay=self.bay2,
				assigned_technician=None,
			)
		)

		win_two = ShopCapacityWindow(target_date, target_date)
		slots_two = {s["start_time"]: s for s in win_two.slots_for_date(target_date, 60)}
		self.assertNotIn("09:00", slots_two)
		self.assertIn("10:00", slots_two)
		self.assertEqual(slots_two["10:00"]["available_technicians"], 2)

		day_slots_all = {
			s["start_time"]: s
			for s in search_day_slots(target_date, 60, include_all=True)
		}
		self.assertFalse(day_slots_all["09:00"]["is_available"])
		self.assertTrue(day_slots_all["09:00"]["bay_available"])
		self.assertEqual(day_slots_all["09:00"]["available_bays"], 2)
		self.assertFalse(day_slots_all["09:00"]["technician_available"])
		self.assertEqual(day_slots_all["09:00"]["available_technicians"], 0)
		self.assertEqual(day_slots_all["09:00"]["effective_capacity"], 0)

		pool_two = get_technician_pool_availability(target_date, "09:00:00", 60)
		self.assertFalse(pool_two["is_available"])
		self.assertEqual(pool_two["on_duty"], 2)
		self.assertEqual(pool_two["occupied"], 2)
		self.assertEqual(pool_two["unassigned_occupied"], 2)
		self.assertEqual(pool_two["available"], 0)
		self.assertEqual(pool_two["available_technicians"], [])

		# Excluding se2 restores 1 slot of technician pool capacity
		pool_excl = get_technician_pool_availability(target_date, "09:00:00", 60, exclude_entry=se2.name)
		self.assertTrue(pool_excl["is_available"])
		self.assertEqual(pool_excl["available"], 1)
		self.assertTrue(se1.name)

	def test_skill_aware_slot_filtering_and_independent_bay_selection(self):
		"""HV Level 3 requires Noah; blocked when Noah is on leave even when Marcus/Elena/Devon are free."""
		day1 = add_days(today(), 22)
		day2 = add_days(today(), 23)
		hv_l3 = [("High Voltage & Battery", 3)]

		# Baseline: Noah is on duty -> exactly 1 qualified tech for HV L3
		win_open = ShopCapacityWindow(day1, day1, required_skills=hv_l3)
		slots_open = {s["start_time"]: s for s in win_open.slots_for_date(day1, 60)}
		self.assertIn("09:00", slots_open)
		self.assertEqual(slots_open["09:00"]["available_technicians"], 1)
		self.assertEqual(slots_open["09:00"]["available_technician_ids"], [self.noah])

		# Bay selection remains independent: requesting Alignment Rack + HV L3 still picks bay2 (alignment)
		win_bay = ShopCapacityWindow(day1, day1, required_tags=["Alignment Rack"], required_skills=hv_l3)
		bay_slots = win_bay.slots_for_date(day1, 60)
		self.assertTrue(bay_slots)
		self.assertEqual(bay_slots[0]["available_bay_ids"], [self.bay2])

		# Individual skill qualification checks
		chk_marcus = check_technician_availability(self.marcus, day1, "09:00:00", 60, required_skills=hv_l3)
		self.assertFalse(chk_marcus["is_available"])
		self.assertEqual(chk_marcus["reason"], "unqualified")

		chk_noah = check_technician_availability(self.noah, day1, "09:00:00", 60, required_skills=hv_l3)
		self.assertTrue(chk_noah["is_available"])

		# Put Noah on approved full-day leave on day1 (marcus, elena, devon remain free)
		self._create_approved_leave(self.noah, day1)

		win_closed = ShopCapacityWindow(day1, day1, required_skills=hv_l3)
		self.assertEqual(win_closed.slots_for_date(day1, 60), [])

		day_slots_hv = search_day_slots(day1, 60, required_skills=hv_l3)
		self.assertEqual(day_slots_hv, [])

		pool_hv = get_technician_pool_availability(day1, "09:00:00", 60, required_skills=hv_l3)
		self.assertFalse(pool_hv["is_available"])
		self.assertEqual(pool_hv["available"], 0)
		self.assertEqual(pool_hv["on_duty"], 0)

		# Generic / Tires L1 jobs remain open for Marcus, Elena, Devon on day1
		win_generic = ShopCapacityWindow(day1, day1)
		generic_slots = {s["start_time"]: s for s in win_generic.slots_for_date(day1, 60)}
		self.assertIn("09:00", generic_slots)
		self.assertEqual(generic_slots["09:00"]["available_technicians"], 3)

		# Multi-day search skips day1 (Noah on leave) and lands on day2
		next_hv = find_next_available_slot_batch(
			getdate(add_days(day1, -1)),
			max_days=5,
			duration_minutes=60,
			required_skills=hv_l3,
		)
		self.assertIsNotNone(next_hv)
		self.assertEqual(next_hv["date"], str(day2))

	def test_per_tech_custom_weekly_shift_and_off_day(self):
		"""Custom 10:00-17:00 shift blocks 08:00-09:30 slots and allows 10:00+; off-day blocks all slots."""
		shift_date = add_days(today(), 26)
		off_date = add_days(today(), 27)
		shift_day_name = DAY_NAMES[getdate(shift_date).weekday()]
		off_day_name = DAY_NAMES[getdate(off_date).weekday()]
		hv_l3 = [("High Voltage & Battery", 3)]

		self._mutate_profile(
			self.noah,
			weekly_schedule=[
				{
					"day_of_week": shift_day_name,
					"is_working_day": 1,
					"shift_start": "10:00:00",
					"shift_end": "17:00:00",
					"break_start": "12:00:00",
					"break_end": "12:30:00",
				},
				{
					"day_of_week": off_day_name,
					"is_working_day": 0,
				},
			],
		)

		# ShopCapacityWindow for HV L3 (only Noah): 08:00-09:30 blocked, 10:00+ open
		win = ShopCapacityWindow(shift_date, off_date, required_skills=hv_l3)
		shift_times = [s["start_time"] for s in win.slots_for_date(shift_date, 60)]
		for early in ("08:00", "08:30", "09:00", "09:30"):
			self.assertNotIn(early, shift_times)
		self.assertIn("10:00", shift_times)
		self.assertIn("14:00", shift_times)
		# Off-day has zero slots
		self.assertEqual(win.slots_for_date(off_date, 60), [])

		# search_day_slots with technician=noah + include_all reports Off Shift for 08:00
		day_all = {
			s["start_time"]: s
			for s in search_day_slots(shift_date, 60, technician=self.noah, include_all=True)
		}
		self.assertFalse(day_all["08:00"]["is_available"])
		self.assertEqual(day_all["08:00"]["badge_label"], "Off Shift")
		self.assertTrue(day_all["10:00"]["is_available"])

		# check_technician_availability and pool availability respect custom shift & off-day
		chk_early = check_technician_availability(self.noah, shift_date, "08:00:00", 60)
		self.assertFalse(chk_early["is_available"])
		self.assertEqual(chk_early["reason"], "off_shift")

		chk_on_shift = check_technician_availability(self.noah, shift_date, "10:00:00", 60)
		self.assertTrue(chk_on_shift["is_available"])

		chk_off = check_technician_availability(self.noah, off_date, "10:00:00", 60)
		self.assertFalse(chk_off["is_available"])
		self.assertEqual(chk_off["reason"], "off_shift")

		pool_early = get_technician_pool_availability(shift_date, "08:00:00", 60, required_skills=hv_l3)
		self.assertFalse(pool_early["is_available"])
		pool_on = get_technician_pool_availability(shift_date, "10:00:00", 60, required_skills=hv_l3)
		self.assertTrue(pool_on["is_available"])
		self.assertEqual(pool_on["available_technicians"], [self.noah])

	def test_am_and_pm_half_day_leave_gating(self):
		"""AM leave blocks morning & allows afternoon; PM leave allows morning & blocks afternoon; blank blocks full day."""
		day_am = add_days(today(), 30)
		day_pm = add_days(today(), 31)
		day_blank = add_days(today(), 32)
		hv_l3 = [("High Voltage & Battery", 3)]

		self._create_approved_leave(self.noah, day_am, half_day=1, half_day_date=day_am, half_day_period="AM")
		self._create_approved_leave(self.noah, day_pm, half_day=1, half_day_date=day_pm, half_day_period="PM")
		self._create_approved_leave(self.noah, day_blank, half_day=1, half_day_date=day_blank, half_day_period="")

		# entry_queries returns ism_half_day_period & half_day_period
		leaves_map = fetch_leaves_by_employee([self.noah], day_am, day_blank)
		noah_leaves = leaves_map.get(self.noah, [])
		self.assertEqual(
			is_employee_on_leave_for_date(noah_leaves, day_am),
			{"on_leave": True, "half_day": True, "half_day_period": "AM"},
		)
		self.assertEqual(
			is_employee_on_leave_for_date(noah_leaves, day_pm),
			{"on_leave": True, "half_day": True, "half_day_period": "PM"},
		)
		self.assertEqual(
			is_employee_on_leave_for_date(noah_leaves, day_blank),
			{"on_leave": True, "half_day": True, "half_day_period": None},
		)

		active_am = {t["employee"]: t for t in get_active_technicians(date=day_am)}
		self.assertTrue(active_am[self.noah]["on_leave"])
		self.assertTrue(active_am[self.noah]["half_day"])
		self.assertEqual(active_am[self.noah]["half_day_period"], "AM")

		# ShopCapacityWindow over all 3 days for HV L3
		win = ShopCapacityWindow(day_am, day_blank, required_skills=hv_l3)

		am_times = [s["start_time"] for s in win.slots_for_date(day_am, 60)]
		for blocked in ("08:00", "09:00", "11:00", "11:30"):
			self.assertNotIn(blocked, am_times)
		for allowed in ("12:30", "13:00", "15:00"):
			self.assertIn(allowed, am_times)

		pm_times = [s["start_time"] for s in win.slots_for_date(day_pm, 60)]
		for allowed in ("08:00", "09:00", "10:00", "11:00"):
			self.assertIn(allowed, pm_times)
		for blocked in ("11:30", "12:30", "13:00", "15:00"):
			self.assertNotIn(blocked, pm_times)

		# Blank half-day period conservatively blocks full day
		self.assertEqual(win.slots_for_date(day_blank, 60), [])

		# search_day_slots matches AM/PM half-day behavior
		slot_am_times = [s["start_time"] for s in search_day_slots(day_am, 60, required_skills=hv_l3)]
		self.assertNotIn("09:00", slot_am_times)
		self.assertIn("13:00", slot_am_times)

		slot_pm_times = [s["start_time"] for s in search_day_slots(day_pm, 60, required_skills=hv_l3)]
		self.assertIn("09:00", slot_pm_times)
		self.assertNotIn("13:00", slot_pm_times)

		# check_technician_availability + pool availability match AM/PM half-day behavior
		chk_am_morning = check_technician_availability(self.noah, day_am, "09:00:00", 60)
		self.assertFalse(chk_am_morning["is_available"])
		self.assertEqual(chk_am_morning["reason"], "on_leave")
		self.assertTrue(chk_am_morning["half_day"])
		self.assertEqual(chk_am_morning["half_day_period"], "AM")

		chk_am_afternoon = check_technician_availability(self.noah, day_am, "13:00:00", 60)
		self.assertTrue(chk_am_afternoon["is_available"])

		chk_pm_morning = check_technician_availability(self.noah, day_pm, "09:00:00", 60)
		self.assertTrue(chk_pm_morning["is_available"])

		chk_pm_afternoon = check_technician_availability(self.noah, day_pm, "13:00:00", 60)
		self.assertFalse(chk_pm_afternoon["is_available"])
		self.assertEqual(chk_pm_afternoon["reason"], "on_leave")
		self.assertEqual(chk_pm_afternoon["half_day_period"], "PM")

		self.assertFalse(get_technician_pool_availability(day_am, "09:00:00", 60, required_skills=hv_l3)["is_available"])
		self.assertTrue(get_technician_pool_availability(day_am, "13:00:00", 60, required_skills=hv_l3)["is_available"])
		self.assertTrue(get_technician_pool_availability(day_pm, "09:00:00", 60, required_skills=hv_l3)["is_available"])
		self.assertFalse(get_technician_pool_availability(day_pm, "13:00:00", 60, required_skills=hv_l3)["is_available"])

	def test_bookable_catalog_returns_internal_required_skills_with_zero_public_leak(self):
		"""compute_packages_booking_requirements returns required_skills; list_bookable_packages leaks nothing."""
		pkg_a = self.track(
			frappe.get_doc({
				"doctype": "Service Package",
				"package_name": f"{TEST_PREFIX}HV_Diag_Bookable",
				"applicable_model": "Model 3",
				"standard_labor_hours": 1.0,
				"hourly_rate": 160.0,
				"service_rate": 160.0,
				"allow_self_serve_booking": 1,
				"customer_title": "High Voltage Inspection",
				"customer_summary": "Comprehensive battery isolation diagnostic.",
				"customer_price_display_mode": "Exact Price",
				"customer_estimate_min": 160.0,
				"required_skills": [
					{"skill": "General Diagnostics", "difficulty_level": 2},
					{"skill": "High Voltage & Battery", "difficulty_level": 2},
				],
			}).insert(ignore_permissions=True)
		)
		pkg_b = self.track(
			frappe.get_doc({
				"doctype": "Service Package",
				"package_name": f"{TEST_PREFIX}HV_Pyro_Bookable",
				"applicable_model": "Model 3",
				"standard_labor_hours": 1.5,
				"hourly_rate": 160.0,
				"service_rate": 240.0,
				"allow_self_serve_booking": 1,
				"customer_title": "Pyro Fuse Service",
				"customer_summary": "Replace high-voltage pyro fuse.",
				"customer_price_display_mode": "Exact Price",
				"customer_estimate_min": 240.0,
				"required_skills": [
					{"skill": "High Voltage & Battery", "difficulty_level": 3},
				],
			}).insert(ignore_permissions=True)
		)

		reqs = compute_packages_booking_requirements([pkg_a.name, pkg_b.name], vehicle_model="Model 3")
		self.assertEqual(
			reqs["required_skills"],
			[("General Diagnostics", 2), ("High Voltage & Battery", 3)],
		)

		# Public catalog cards must contain ONLY PUBLIC_CARD_KEYS and zero internal skill/tech metadata
		cards = list_bookable_packages(vehicle_model="Model 3")
		card_by_id = {c["package_id"]: c for c in cards}
		self.assertIn(pkg_a.name, card_by_id)
		self.assertIn(pkg_b.name, card_by_id)
		for pkg_id in (pkg_a.name, pkg_b.name):
			card = card_by_id[pkg_id]
			self.assertEqual(set(card.keys()), PUBLIC_CARD_KEYS)
			serialized = json.dumps(card)
			self.assertNotIn("required_skills", serialized)
			self.assertNotIn("High Voltage & Battery", serialized)
			self.assertNotIn("difficulty", serialized)
			self.assertNotIn("assigned_technician", serialized)

		# Booked packages snapshot inside reqs must also contain zero internal skill/tech fields
		snap_serialized = json.dumps(reqs["booked_packages_snapshot"])
		self.assertNotIn("required_skills", snap_serialized)
		self.assertNotIn("High Voltage & Battery", snap_serialized)
		self.assertNotIn("difficulty", snap_serialized)

	def test_inactive_technician_profile_excluded(self):
		"""Deactivating a Technician Profile excludes that technician from pool and individual checks."""
		target_date = add_days(today(), 34)
		hv_l3 = [("High Voltage & Battery", 3)]
		self._mutate_profile(self.noah, is_active=0)

		chk = check_technician_availability(self.noah, target_date, "09:00:00", 60)
		self.assertFalse(chk["is_available"])
		self.assertEqual(chk["reason"], "inactive")

		pool_hv = get_technician_pool_availability(target_date, "09:00:00", 60, required_skills=hv_l3)
		self.assertFalse(pool_hv["is_available"])

		pool_generic = get_technician_pool_availability(target_date, "09:00:00", 60)
		self.assertEqual(pool_generic["on_duty"], 3)
		self.assertNotIn(self.noah, pool_generic["available_technicians"])

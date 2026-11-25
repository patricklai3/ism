# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 3A E2E tests for booking auto-assignment, multi-service split, self-serve zero-leak, RO sync/lock, and hooks."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any

import frappe
from frappe.utils import add_days, cint, getdate, today

from ism.api.scheduling import (
	allocate_schedule_entry,
	find_next_available_slot,
	get_available_slots,
)
from ism.permissions import get_eligible_ro_names_for_employee, is_ro_visible_to_user
from ism.scheduling.entry_queries import build_holiday_checker
from ism.scheduling.self_serve import (
	create_provisional_booking,
	find_self_serve_slots,
	reschedule_self_serve_appointment,
)
from ism.scheduling.time_utils import get_shop_settings, invalidate_shop_settings_cache
from ism.setup_tasks import setup_leave_custom_fields
from ism.tests.base import ISMTestCase, TEST_PREFIX

_FORBIDDEN_GUEST_KEYS = {
	"assigned_technician",
	"assignment_mode",
	"assignment_locked",
	"assignment_reason",
	"required_skills",
	"required_skills_json",
	"available_technicians",
	"available_technician_ids",
	"service_bay",
	"available_bays",
	"available_bay_ids",
}


def _collect_keys_and_values(obj: Any, keys_out: set[str], values_out: list[str]) -> None:
	if isinstance(obj, dict):
		for k, v in obj.items():
			keys_out.add(str(k))
			_collect_keys_and_values(v, keys_out, values_out)
	elif isinstance(obj, (list, tuple, set)):
		for item in obj:
			_collect_keys_and_values(item, keys_out, values_out)
	elif isinstance(obj, str):
		values_out.append(obj)


class TestAssignmentBookingE2E(ISMTestCase):
	"""End-to-end integration tests for Wave 3A booking wiring, sync, locks, and hooks."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		setup_leave_custom_fields()
		self.manifest = self.get_manifest()
		self.marcus = self.manifest.resolve("techs.marcus")
		self.elena = self.manifest.resolve("techs.elena")
		self.devon = self.manifest.resolve("techs.devon")
		self.noah = self.manifest.resolve("techs.noah")
		self.canonical_techs = [self.marcus, self.elena, self.devon, self.noah]
		self.bay_main = self.manifest.resolve("bays.main_lift")
		self.bay_align = self.manifest.resolve("bays.alignment")
		self.bay_hv = self.manifest.resolve("bays.hv_diagnostics")
		self.bay_tire = self.manifest.resolve("bays.tire_quick")
		self.customer = self.manifest.resolve("customers.retail_1")
		self.vehicle = self.manifest.resolve("vehicles.veh_model3_liam")
		self._saved_settings: dict[str, Any] = {}
		self._set_ism_settings(
			operating_hours_start="08:00:00",
			operating_hours_end="17:00:00",
			break_start="12:00:00",
			break_end="12:30:00",
			default_slot_interval=30,
			default_diagnostic_duration=60,
			enable_technician_capacity=1,
			technician_designation="Technician",
			holiday_list=None,
			customer_arrival_offset_minutes=15,
			reschedule_cutoff_hours=24,
			max_self_reschedules=2,
			max_concurrent_bookings_per_phone=2,
			self_serve_default_entry_type="Diagnostic",
			self_serve_min_lead_time_minutes=120,
			self_serve_horizon_days=30,
			enable_waiter_preference=1,
			buffer_padding_minutes=0,
			assignment_policy_mode="Tiered Waterfall",
			tier_spillover_threshold_pct=85.0,
			auto_rebalance_skew_threshold_pct=25.0,
		)
		self.now_anchor = datetime.combine(getdate(today()), time(9, 0, 0))

	def tearDown(self):
		frappe.set_user("Administrator")
		if self._saved_settings:
			settings_doc = frappe.get_single("ISM Settings")
			for key, val in self._saved_settings.items():
				setattr(settings_doc, key, val)
			settings_doc.save(ignore_permissions=True)
			self._saved_settings.clear()
			invalidate_shop_settings_cache()
		frappe.flags.ism_rebalancing = False
		super().tearDown()

	def _set_ism_settings(self, **kwargs: Any) -> None:
		settings_doc = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			if key not in self._saved_settings:
				self._saved_settings[key] = getattr(settings_doc, key, None)
			setattr(settings_doc, key, val)
		settings_doc.save(ignore_permissions=True)
		invalidate_shop_settings_cache()

	def _next_workdays(self, count: int = 3, start_offset: int = 8) -> list[str]:
		shop = get_shop_settings()
		is_holiday = build_holiday_checker(shop.get("holiday_list"), min_date=today())
		cursor = add_days(getdate(today()), start_offset)
		while True:
			days = [add_days(cursor, i) for i in range(count)]
			if all(d.weekday() < 5 and not is_holiday(d) for d in days):
				return [str(d) for d in days]
			cursor = add_days(cursor, 1)

	def _make_custom_skill(self, suffix: str, scarcity_weight: float = 1.0) -> str:
		doc = frappe.get_doc({
			"doctype": "Technician Skill",
			"skill_name": f"{TEST_PREFIX}W3Skill_{suffix}_{frappe.generate_hash(length=5)}",
			"is_active": 1,
			"scarcity_weight": scarcity_weight,
		}).insert(ignore_permissions=True)
		self.track(doc)
		return doc.name

	def _make_custom_tech(
		self,
		suffix: str,
		*,
		assignment_tier: int = 1,
		target_load_weight: float = 1.0,
		skills: list[dict[str, Any]] | None = None,
	) -> tuple[str, str]:
		emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": f"{TEST_PREFIX}W3Tech",
			"last_name": suffix,
			"employee_name": f"{TEST_PREFIX}W3Tech {suffix}",
			"status": "Active",
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2023-01-01",
			"company": self.manifest.company,
			"designation": "Technician",
			"holiday_list": self.manifest.holiday_list,
		}).insert(ignore_permissions=True)
		self.track(emp)

		prof = frappe.get_doc({
			"doctype": "Technician Profile",
			"employee": emp.name,
			"is_active": 1,
			"assignment_tier": assignment_tier,
			"target_load_weight": target_load_weight,
			"skills": skills or [],
		}).insert(ignore_permissions=True)
		self.track(prof)
		return emp.name, prof.name

	def _make_repair_order(
		self,
		*,
		vehicle: str | None = None,
		status: str = "Draft Estimate",
		approval_status: str = "Estimating",
		services: list[dict[str, Any]] | None = None,
	):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": self.manifest.company,
			"customer": self.customer,
			"vehicle": vehicle or self.vehicle,
			"odometer_in": 31000,
			"status": status,
			"approval_status": approval_status,
			"services": services or [],
		}).insert(ignore_permissions=True)
		return self.track(ro)

	def _make_service_package(
		self,
		suffix: str,
		*,
		allow_self_serve: int = 0,
		duration_minutes: int = 60,
		required_skills: list[dict[str, Any]] | None = None,
	):
		name = f"{TEST_PREFIX}W3Pkg_{suffix}_{frappe.generate_hash(length=5)}"
		doc = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": name,
			"applicable_model": "Model 3",
			"standard_labor_hours": max(0.5, duration_minutes / 60.0),
			"is_flat_rate": 0,
			"allow_self_serve_booking": allow_self_serve,
			"self_serve_duration_minutes": duration_minutes if allow_self_serve else 0,
			"customer_title": f"Customer {suffix}",
			"customer_summary": f"Summary for {suffix}",
			"customer_price_display_mode": "Exact Price",
			"customer_estimate_min": 149.0,
			"required_skills": required_skills or [],
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def _assert_zero_guest_leak(self, payload: Any) -> None:
		keys: set[str] = set()
		values: list[str] = []
		_collect_keys_and_values(payload, keys, values)
		leaked_keys = keys & _FORBIDDEN_GUEST_KEYS
		self.assertFalse(leaked_keys, f"Guest payload leaked internal keys: {sorted(leaked_keys)}")
		for emp_id in self.canonical_techs:
			for val in values:
				self.assertNotIn(emp_id, val, f"Guest payload leaked technician ID {emp_id} in '{val}'")

	# ------------------------------------------------------------------
	# 1. allocate_schedule_entry: auto-assign, manual override, ROS sync, slot search
	# ------------------------------------------------------------------

	def test_allocate_schedule_entry_auto_assign_manual_override_and_ros_sync(self):
		"""allocate_schedule_entry auto-assigns, syncs RO Service rows, handles manual override lock, and auto-locks on In Progress."""
		target_date = self._next_workdays(count=1, start_offset=10)[0]

		ro = self._make_repair_order(
			services=[{
				"service_title": "HV Battery Pack Inspection",
				"status": "Approved",
				"labor_hours": 1.0,
				"required_skill": "High Voltage & Battery",
				"difficulty_level": 3,
			}],
			approval_status="Estimating",
		)
		svc_name = ro.services[0].name

		# Slot search auto-derives required_skills from repair_order (only Noah qualifies for HV L3)
		slots = get_available_slots(
			date=target_date,
			duration_minutes=60,
			repair_order=ro.name,
		)
		self.assertTrue(slots)
		self.assertEqual(slots[0]["available_technicians"], 1)

		next_slot = find_next_available_slot(
			from_date=str(add_days(getdate(target_date), -1)),
			duration_minutes=60,
			repair_order=ro.name,
		)
		self.assertIsNotNone(next_slot)
		self.assertEqual(next_slot["first_slot"]["available_technicians"], 1)

		# Auto-assign via allocate_schedule_entry (assigned_technician omitted -> Noah)
		res = allocate_schedule_entry(
			repair_order=ro.name,
			entry_type="Repair",
			scheduled_date=target_date,
			scheduled_time="09:00:00",
			estimated_duration=60,
			service_bay=self.bay_hv,
			selected_services=[svc_name],
		)
		self.assertEqual(res["assigned_technician"], self.noah)
		self.assertEqual(res["assignment_mode"], "Auto")
		self.assertEqual(res["assignment_locked"], 0)
		self.assertTrue(res["assignment_reason"])

		se = frappe.get_doc("Schedule Entry", res["name"])
		self.track(se)
		self.assertEqual(se.assigned_technician, self.noah)
		self.assertEqual(se.assignment_mode, "Auto")
		self.assertEqual(cint(se.assignment_locked), 0)
		self.assertEqual(json.loads(se.required_skills_json), [["High Voltage & Battery", 3]])

		# Repair Order Service row synced with schedule_entry AND assigned_technician
		ros_row = frappe.db.get_value(
			"Repair Order Service",
			svc_name,
			["schedule_entry", "assigned_technician"],
			as_dict=True,
		)
		self.assertEqual(ros_row.schedule_entry, se.name)
		self.assertEqual(ros_row.assigned_technician, self.noah)

		# Manual override via allocate_schedule_entry locks assignment and updates RO Service row
		upd = allocate_schedule_entry(
			repair_order=ro.name,
			entry_type="Repair",
			scheduled_date=target_date,
			scheduled_time="09:00:00",
			estimated_duration=60,
			service_bay=self.bay_hv,
			assigned_technician=self.elena,
			selected_services=[svc_name],
			schedule_entry_name=se.name,
		)
		self.assertEqual(upd["assigned_technician"], self.elena)
		self.assertEqual(upd["assignment_mode"], "Manual Override")
		self.assertEqual(upd["assignment_locked"], 1)
		self.assertEqual(upd["assignment_reason"], "Manual override by staff")
		# Soft warning emitted because Elena is L2 HV while job needs L3 HV
		self.assertTrue(upd.get("warnings"))

		ros_after_override = frappe.db.get_value("Repair Order Service", svc_name, "assigned_technician")
		self.assertEqual(ros_after_override, self.elena)

		# Direct Schedule Entry save syncs technician change & auto-locks on In Progress
		se.reload()
		se.assignment_locked = 0
		se.assigned_technician = self.noah
		se.status = "In Progress"
		se.save(ignore_permissions=True)
		se.reload()
		self.assertEqual(cint(se.assignment_locked), 1)
		self.assertEqual(
			frappe.db.get_value("Repair Order Service", svc_name, "assigned_technician"),
			self.noah,
		)

	# ------------------------------------------------------------------
	# 2. Multi-service RO auto-split across sequential blocks (lunch-aware)
	# ------------------------------------------------------------------

	def test_allocate_schedule_entry_multi_service_auto_split(self):
		"""allocate_schedule_entry splits incompatible multi-service RO into sequential lunch-aware blocks."""
		target_date = self._next_workdays(count=1, start_offset=14)[0]
		skill_a = self._make_custom_skill("Alpha")
		skill_b = self._make_custom_skill("Beta")
		tech_a, _ = self._make_custom_tech(
			"SpecA",
			skills=[{"skill": skill_a, "max_difficulty": 3, "skill_role": "Preferred"}],
		)
		tech_b, _ = self._make_custom_tech(
			"SpecB",
			skills=[{"skill": skill_b, "max_difficulty": 3, "skill_role": "Preferred"}],
		)

		ro = self._make_repair_order(
			services=[
				{
					"service_title": "Alpha Calibration",
					"status": "Approved",
					"labor_hours": 1.0,
					"required_skill": skill_a,
					"difficulty_level": 2,
				},
				{
					"service_title": "Beta Calibration",
					"status": "Approved",
					"labor_hours": 1.5,
					"required_skill": skill_b,
					"difficulty_level": 2,
				},
			]
		)
		svc_a, svc_b = ro.services[0].name, ro.services[1].name

		# Start at 11:00: Block 1 (60m) runs 11:00-12:00, Block 2 (90m) skips lunch (12:00-12:30) and runs 12:30-14:00
		res = allocate_schedule_entry(
			repair_order=ro.name,
			entry_type="Repair",
			scheduled_date=target_date,
			scheduled_time="11:00:00",
			estimated_duration=150,
			service_bay=self.bay_main,
			selected_services=[svc_a, svc_b],
			auto_split_services=1,
		)
		self.assertIn("split_entries", res)
		split_entries = res["split_entries"]
		self.assertEqual(len(split_entries), 2)

		se1 = frappe.get_doc("Schedule Entry", split_entries[0]["name"])
		se2 = frappe.get_doc("Schedule Entry", split_entries[1]["name"])
		self.track(se1)
		self.track(se2)

		self.assertEqual(res["name"], se1.name)
		self.assertEqual(se1.assigned_technician, tech_a)
		self.assertEqual(se2.assigned_technician, tech_b)
		self.assertTrue(se1.service_bay)
		self.assertTrue(se2.service_bay)
		self.assertEqual(se1.scheduled_time, timedelta(hours=11))
		# Block 2 respects lunch break (12:00-12:30) and starts cleanly at 12:30
		self.assertEqual(se2.scheduled_time, timedelta(hours=12, minutes=30))

		# Verify each Repair Order Service row is linked to its own Schedule Entry + specialist tech
		row_a = frappe.db.get_value("Repair Order Service", svc_a, ["schedule_entry", "assigned_technician"], as_dict=True)
		row_b = frappe.db.get_value("Repair Order Service", svc_b, ["schedule_entry", "assigned_technician"], as_dict=True)
		self.assertEqual(row_a.schedule_entry, se1.name)
		self.assertEqual(row_a.assigned_technician, tech_a)
		self.assertEqual(row_b.schedule_entry, se2.name)
		self.assertEqual(row_b.assigned_technician, tech_b)

	# ------------------------------------------------------------------
	# 3. Self-serve booking & reschedule soft-assign + strict zero guest leak
	# ------------------------------------------------------------------

	def test_self_serve_booking_and_reschedule_soft_assign_and_zero_leak(self):
		"""Self-serve discovery, booking, and rescheduling soft-assign qualified techs with zero guest metadata leaks."""
		days = self._next_workdays(count=2, start_offset=6)
		day1, day2 = days[0], days[1]

		hv_pkg = self._make_service_package(
			"HVCheck",
			allow_self_serve=1,
			duration_minutes=60,
			required_skills=[{"skill": "High Voltage & Battery", "difficulty_level": 3}],
		)

		slots_res = find_self_serve_slots(
			package_ids=[hv_pkg.name],
			start_date=day1,
			days_ahead=1,
			vehicle_model="Model 3",
			now_dt=self.now_anchor,
		)
		self._assert_zero_guest_leak(slots_res)
		self.assertGreater(slots_res["total_slots"], 0)

		booking = create_provisional_booking(
			package_ids=[hv_pkg.name],
			target_date=day1,
			customer_arrival_time="08:45",  # Internal 09:00:00
			customer_name="Taylor Guest",
			phone="4165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			vehicle_color="Pearl White",
			license_plate="HVTEST1",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_zero_guest_leak(booking)

		se = frappe.get_doc("Schedule Entry", booking["booking_reference"])
		self.track(se)
		self.assertEqual(se.assigned_technician, self.noah)
		self.assertEqual(se.assignment_mode, "Auto")
		self.assertEqual(cint(se.assignment_locked), 0)
		self.assertTrue(se.assignment_reason)
		self.assertEqual(json.loads(se.required_skills_json), [["High Voltage & Battery", 3]])

		# Book a general package (Tires L1 -> Devon/Marcus/Elena/Noah qualify) and test reschedule soft-assign + lock retention
		tire_pkg = self._make_service_package(
			"TireRotate",
			allow_self_serve=1,
			duration_minutes=60,
			required_skills=[{"skill": "Tires & Maintenance", "difficulty_level": 1}],
		)
		b2 = create_provisional_booking(
			package_ids=[tire_pkg.name],
			target_date=day1,
			customer_arrival_time="09:45",  # Internal 10:00:00
			customer_name="Jordan Driver",
			phone="4165550192",
			vehicle_model="Model 3",
			vehicle_year=2022,
			vehicle_color="Blue",
			license_plate="TIREROT2",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_zero_guest_leak(b2)
		se2 = frappe.get_doc("Schedule Entry", b2["booking_reference"])
		self.track(se2)
		orig_tech = se2.assigned_technician
		self.assertTrue(orig_tech)
		self.assertEqual(cint(se2.assignment_locked), 0)

		# Occupy orig_tech at 13:00 on day1 so reschedule to 12:45 (internal 13:00) must soft-assign another qualified tech
		blocker = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": "Scheduled",
			"scheduled_date": day1,
			"scheduled_time": "13:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay_tire,
			"assigned_technician": orig_tech,
			"provisional_customer_name": "Blocker Customer",
			"provisional_phone": "+14165550199",
		}).insert(ignore_permissions=True)
		self.track(blocker)

		resched = reschedule_self_serve_appointment(
			se2.name,
			day1,
			"12:45",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_zero_guest_leak(resched)
		se2.reload()
		self.assertIsNotNone(se2.assigned_technician)
		self.assertNotEqual(se2.assigned_technician, orig_tech)
		self.assertEqual(cint(se2.assignment_locked), 0)

		# Lock se2 on Marcus, then reschedule to day2 09:45 where Marcus is free -> locked tech retained!
		se2.assigned_technician = self.marcus
		se2.assignment_locked = 1
		se2.assignment_mode = "Manual Override"
		se2.save(ignore_permissions=True)

		resched_locked = reschedule_self_serve_appointment(
			se2.name,
			day2,
			"09:45",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_zero_guest_leak(resched_locked)
		se2.reload()
		self.assertEqual(se2.assigned_technician, self.marcus)
		self.assertEqual(cint(se2.assignment_locked), 1)

	# ------------------------------------------------------------------
	# 4. Package skill auto-pop, RO auth/pickup lock, and /tech visibility
	# ------------------------------------------------------------------

	def test_ro_package_skill_auto_populate_authorization_lock_and_tech_visibility(self):
		"""add_service_package populates highest-difficulty skill; RO authorize/pickup locks SE; /tech isolation passes."""
		target_date = self._next_workdays(count=1, start_offset=18)[0]

		pkg = self._make_service_package(
			"MultiSkillBrake",
			allow_self_serve=0,
			duration_minutes=90,
			required_skills=[
				{"skill": "Tires & Maintenance", "difficulty_level": 1},
				{"skill": "Brakes & Suspension", "difficulty_level": 3},
			],
		)

		ro = self._make_repair_order(status="Draft Estimate", approval_status="Estimating")
		ro.add_service_package(service_package=pkg.name)
		ro.reload()
		self.assertEqual(len(ro.services), 1)
		self.assertEqual(ro.services[0].required_skill, "Brakes & Suspension")
		self.assertEqual(cint(ro.services[0].difficulty_level), 3)

		# Allocate Schedule Entry -> auto-assigns a qualified L3 Brakes tech (Marcus or Elena), unlocked
		alloc = allocate_schedule_entry(
			repair_order=ro.name,
			entry_type="Repair",
			scheduled_date=target_date,
			scheduled_time="08:30:00",
			estimated_duration=90,
			service_bay=self.bay_main,
			selected_services=[ro.services[0].name],
		)
		se = frappe.get_doc("Schedule Entry", alloc["name"])
		self.track(se)
		assigned_emp = se.assigned_technician
		self.assertIn(assigned_emp, {self.marcus, self.elena, self.noah})
		self.assertEqual(cint(se.assignment_locked), 0)

		# Authorize RO -> automatically locks linked Schedule Entry and makes RO visible to assigned tech only
		ro.reload()
		ro.authorize_jobs(
			approvals={ro.services[0].name: "Approved"},
			channel="In-Person Signature",
			signer_name="Test Owner",
		)
		se.reload()
		self.assertEqual(cint(se.assignment_locked), 1)

		# Verify /tech RBAC visibility via permissions.py
		assigned_user = frappe.db.get_value("Employee", assigned_emp, "user_id")
		other_emp = next(e for e in (self.marcus, self.elena, self.devon) if e != assigned_emp)
		other_user = frappe.db.get_value("Employee", other_emp, "user_id")
		if assigned_user:
			self.assertTrue(is_ro_visible_to_user(ro.name, assigned_user))
			self.assertIn(ro.name, get_eligible_ro_names_for_employee(assigned_emp))
		if other_user:
			self.assertFalse(is_ro_visible_to_user(ro.name, other_user))
			self.assertNotIn(ro.name, get_eligible_ro_names_for_employee(other_emp))

		# Adding a second unlocked SE and calling pick_up_vehicle() also locks it
		se_extra = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": ro.name,
			"entry_type": "Repair",
			"status": "Scheduled",
			"scheduled_date": target_date,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay_main,
			"assigned_technician": assigned_emp,
			"assignment_locked": 0,
		}).insert(ignore_permissions=True)
		self.track(se_extra)
		self.assertEqual(cint(se_extra.assignment_locked), 0)

		ro.reload()
		ro.pick_up_vehicle()
		se_extra.reload()
		self.assertEqual(cint(se_extra.assignment_locked), 1)

	# ------------------------------------------------------------------
	# 5. Leave Application submit hook: rebalance unlocked + alert locked
	# ------------------------------------------------------------------

	def test_leave_application_submit_hook_rebalances_unlocked_and_alerts_locked(self):
		"""Submitting approved Leave Application fires hooks.py to rebalance unlocked SE and alert locked SE."""
		target_date = self._next_workdays(count=1, start_offset=22)[0]
		veh2 = self.create_test_vehicle(customer=self.customer)

		ro_unlocked = self._make_repair_order(
			services=[{
				"service_title": "Brake Pad Swap",
				"status": "Estimating",
				"labor_hours": 1.0,
				"required_skill": "Brakes & Suspension",
				"difficulty_level": 2,
			}],
		)
		svc_u = ro_unlocked.services[0].name

		se_unlocked = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": ro_unlocked.name,
			"entry_type": "Repair",
			"status": "Scheduled",
			"scheduled_date": target_date,
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay_main,
			"assigned_technician": self.marcus,
			"assignment_mode": "Auto",
			"assignment_locked": 0,
			"required_skills_json": json.dumps([["Brakes & Suspension", 2]]),
		}).insert(ignore_permissions=True)
		self.track(se_unlocked)
		frappe.db.set_value(
			"Repair Order Service",
			svc_u,
			{"schedule_entry": se_unlocked.name, "assigned_technician": self.marcus},
			update_modified=False,
		)

		ro_locked = self._make_repair_order(
			vehicle=veh2.name,
			services=[{
				"service_title": "Suspension Inspection",
				"status": "Estimating",
				"labor_hours": 1.0,
				"required_skill": "Brakes & Suspension",
				"difficulty_level": 2,
			}],
		)
		se_locked = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": ro_locked.name,
			"entry_type": "Repair",
			"status": "Scheduled",
			"scheduled_date": target_date,
			"scheduled_time": "11:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay_main,
			"assigned_technician": self.marcus,
			"assignment_mode": "Manual Override",
			"assignment_locked": 1,
			"required_skills_json": json.dumps([["Brakes & Suspension", 2]]),
		}).insert(ignore_permissions=True)
		self.track(se_locked)

		# Ensure a Leave Type exists and submit a real Leave Application so hooks.py on_submit fires
		leave_type = frappe.db.get_value("Leave Type", {"allow_negative": 1}, "name")
		if not leave_type:
			lt = frappe.get_doc({
				"doctype": "Leave Type",
				"leave_type_name": f"{TEST_PREFIX}W3Leave_{frappe.generate_hash(length=5)}",
				"allow_negative": 1,
			}).insert(ignore_permissions=True)
			self.track(lt)
			leave_type = lt.name

		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.marcus,
			"company": self.manifest.company,
			"leave_type": leave_type,
			"from_date": target_date,
			"to_date": target_date,
			"total_leave_days": 1,
			"status": "Approved",
			"description": f"{TEST_PREFIX}W3HookLeave",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		self.track(leave)
		leave.flags.ignore_validate = True
		leave.submit()

		# Unlocked SE automatically rebalanced away from Marcus + synced onto RO Service
		se_unlocked.reload()
		self.assertIsNotNone(se_unlocked.assigned_technician)
		self.assertNotEqual(se_unlocked.assigned_technician, self.marcus)
		self.assertEqual(se_unlocked.assignment_mode, "Rebalanced")
		self.assertEqual(
			frappe.db.get_value("Repair Order Service", svc_u, "assigned_technician"),
			se_unlocked.assigned_technician,
		)

		# Locked SE stays on Marcus + deduplicated Shop Action Item created
		se_locked.reload()
		self.assertEqual(se_locked.assigned_technician, self.marcus)
		self.assertEqual(cint(se_locked.assignment_locked), 1)
		action_item_name = frappe.db.get_value(
			"Shop Action Item",
			{"dedup_key": f"leave_conflict:locked:{se_locked.name}:{leave.name}"},
			"name",
		)
		self.assertTrue(action_item_name, "Expected Shop Action Item for locked SE leave conflict")
		if action_item_name:
			self.track(frappe.get_doc("Shop Action Item", action_item_name))

	# ------------------------------------------------------------------
	# 6. Cancellation hook skew trigger
	# ------------------------------------------------------------------

	def test_cancellation_hook_triggers_skew_rebalance(self):
		"""Cancelling a Schedule Entry triggers hooks.py on_schedule_entry_change to rebalance unlocked skew."""
		target_date = self._next_workdays(count=1, start_offset=26)[0]
		self._set_ism_settings(
			assignment_policy_mode="Equal Balance",
			auto_rebalance_skew_threshold_pct=15.0,
		)
		skew_skill = self._make_custom_skill("SkewHook")
		tech_x, _ = self._make_custom_tech(
			"SkewX",
			skills=[{"skill": skew_skill, "max_difficulty": 2, "skill_role": "Preferred"}],
		)
		tech_y, _ = self._make_custom_tech(
			"SkewY",
			skills=[{"skill": skew_skill, "max_difficulty": 2, "skill_role": "Preferred"}],
		)

		# Give tech_x 2 locked morning blocks (240m) + 1 unlocked afternoon block (120m)
		# Give tech_y 1 large block (240m) that gets cancelled -> skew spikes -> unlocked block moves to tech_y!
		def _mk(sched_time: str, dur: int, tech: str, bay: str, locked: int):
			doc = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Maintenance",
				"status": "Scheduled",
				"scheduled_date": target_date,
				"scheduled_time": sched_time,
				"estimated_duration": dur,
				"service_bay": bay,
				"assigned_technician": tech,
				"assignment_mode": "Manual Override" if locked else "Auto",
				"assignment_locked": locked,
				"required_skills_json": json.dumps([[skew_skill, 2]]),
			})
			doc.flags.ignore_validate = True
			doc.insert(ignore_permissions=True)
			return self.track(doc)

		_mk("08:00:00", 240, tech_x, self.bay_main, locked=1)
		se_unlocked = _mk("13:00:00", 120, tech_x, self.bay_main, locked=0)
		se_to_cancel = _mk("08:00:00", 240, tech_y, self.bay_tire, locked=0)

		# Cancel se_to_cancel via normal save() so hooks.py Schedule Entry.on_update fires
		se_to_cancel.status = "Cancelled"
		se_to_cancel.save(ignore_permissions=True)

		se_unlocked.reload()
		self.assertEqual(se_unlocked.assigned_technician, tech_y)
		self.assertEqual(se_unlocked.assignment_mode, "Rebalanced")

# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 1 unit tests for Technician Assignment & Scheduling Matrix schema, seeds, skills, and shifts."""

from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe.utils import flt

from ism.scheduling.shifts import (
	apply_leave_to_shift_window,
	calculate_technician_available_hours,
	get_technician_shift_window,
	is_interval_within_shift,
)
from ism.scheduling.skills import (
	get_required_skills_for_packages,
	get_required_skills_for_repair_order,
	get_required_skills_for_schedule_entry,
	is_technician_qualified,
	load_active_technician_profiles,
	load_package_skills_map,
	normalize_skill_requirements,
	worst_skill_role,
)
from ism.scheduling.time_utils import DEFAULT_SHOP_SETTINGS, get_shop_settings
from ism.setup_tasks import (
	DEFAULT_TECHNICIAN_SKILLS,
	seed_schedule_entry_types,
	seed_technician_skills,
	setup_default_ism_settings,
	setup_leave_custom_fields,
)
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestTechnicianMatrixSchema(ISMTestCase):
	"""Verify Wave 1 DocTypes, validations, seeds, settings, skills.py, and shifts.py contracts."""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_skill(self, suffix: str, scarcity_weight: float = 1.0):
		skill_name = f"{TEST_PREFIX}Skill_{suffix}_{frappe.generate_hash(length=6)}"
		doc = frappe.get_doc({
			"doctype": "Technician Skill",
			"skill_name": skill_name,
			"is_active": 1,
			"scarcity_weight": scarcity_weight,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def _create_temp_employee(self, suffix: str):
		manifest = self.get_manifest()
		emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": f"{TEST_PREFIX}Tech",
			"last_name": suffix,
			"employee_name": f"{TEST_PREFIX}Tech {suffix}",
			"status": "Active",
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2023-01-01",
			"company": manifest.company,
			"designation": "Technician",
			"holiday_list": manifest.holiday_list,
		}).insert(ignore_permissions=True)
		return self.track(emp)

	def test_schema_fields_and_leave_custom_field_present(self):
		"""All new DocTypes, extended fields, and Leave Application.ism_half_day_period exist."""
		for dt in (
			"Technician Skill",
			"Technician Skill Competency",
			"Technician Shift Day",
			"Technician Profile",
			"Service Package Skill",
		):
			self.assertTrue(frappe.db.exists("DocType", dt), f"Missing DocType {dt}")

		self.assertTrue(frappe.get_meta("Service Package").has_field("required_skills"))
		self.assertTrue(frappe.get_meta("Schedule Entry Type").has_field("default_skill"))
		self.assertTrue(frappe.get_meta("Schedule Entry Type").has_field("default_difficulty_level"))

		ros_meta = frappe.get_meta("Repair Order Service")
		for fname in ("required_skill", "difficulty_level", "is_comeback", "original_technician"):
			self.assertTrue(ros_meta.has_field(fname), f"Missing Repair Order Service.{fname}")

		se_meta = frappe.get_meta("Schedule Entry")
		for fname in (
			"assignment_mode",
			"assignment_locked",
			"assignment_reason",
			"is_comeback",
			"required_skills_json",
		):
			self.assertTrue(se_meta.has_field(fname), f"Missing Schedule Entry.{fname}")

		settings_meta = frappe.get_meta("ISM Settings")
		for fname in (
			"sb_technician_assignment",
			"assignment_policy_mode",
			"fairness_window_days",
			"tier_spillover_threshold_pct",
			"fairness_weight",
			"skill_conservation_weight",
			"skill_preference_weight",
			"schedule_packing_weight",
			"enable_morning_auto_rebalance",
			"auto_rebalance_skew_threshold_pct",
		):
			self.assertTrue(settings_meta.has_field(fname), f"Missing ISM Settings.{fname}")

		setup_leave_custom_fields()
		self.assertTrue(frappe.db.has_column("Leave Application", "ism_half_day_period"))

	def test_canonical_seeds_settings_and_layer1_profiles(self):
		"""Canonical 6 skills, Schedule Entry Types, ISM Settings, and Layer 1 profiles/packages seed cleanly."""
		seed_technician_skills()
		seed_schedule_entry_types()
		setup_default_ism_settings()

		for spec in DEFAULT_TECHNICIAN_SKILLS:
			self.assertTrue(
				frappe.db.exists("Technician Skill", spec["skill_name"]),
				f"Missing canonical skill {spec['skill_name']}",
			)

		self.assertEqual(
			frappe.db.get_value("Schedule Entry Type", "Diagnostic", "default_skill"),
			"General Diagnostics",
		)
		self.assertEqual(
			int(frappe.db.get_value("Schedule Entry Type", "Diagnostic", "default_difficulty_level") or 0),
			2,
		)
		self.assertEqual(
			frappe.db.get_value("Schedule Entry Type", "Repair", "default_skill"),
			"Brakes & Suspension",
		)
		self.assertEqual(
			frappe.db.get_value("Schedule Entry Type", "Maintenance", "default_skill"),
			"Tires & Maintenance",
		)
		self.assertFalse(frappe.db.get_value("Schedule Entry Type", "Meeting", "default_skill"))

		# Verify get_shop_settings() hydrates all 9 keys and preserves explicit zero values
		shop = get_shop_settings()
		self.assertEqual(shop["assignment_policy_mode"], "Tiered Waterfall")
		self.assertEqual(shop["fairness_window_days"], 7)
		self.assertAlmostEqual(flt(shop["tier_spillover_threshold_pct"]), 85.0, places=2)
		self.assertAlmostEqual(flt(shop["fairness_weight"]), 0.55, places=2)
		self.assertAlmostEqual(flt(shop["skill_conservation_weight"]), 0.25, places=2)
		self.assertAlmostEqual(flt(shop["skill_preference_weight"]), 0.12, places=2)
		self.assertAlmostEqual(flt(shop["schedule_packing_weight"]), 0.08, places=2)
		self.assertEqual(shop["enable_morning_auto_rebalance"], 0)
		self.assertAlmostEqual(flt(shop["auto_rebalance_skew_threshold_pct"]), 25.0, places=2)

		settings_doc = frappe.get_single("ISM Settings")
		orig_pack = settings_doc.schedule_packing_weight
		try:
			settings_doc.schedule_packing_weight = 0.0
			settings_doc.save(ignore_permissions=True)
			hydrated = get_shop_settings()
			self.assertEqual(hydrated["schedule_packing_weight"], 0.0)
		finally:
			settings_doc.schedule_packing_weight = orig_pack
			settings_doc.save(ignore_permissions=True)

		# Verify Layer 1 canonical Technician Profiles and Service Package skills
		manifest = self.get_manifest()
		profiles = load_active_technician_profiles([
			manifest.resolve("techs.marcus"),
			manifest.resolve("techs.elena"),
			manifest.resolve("techs.devon"),
			manifest.resolve("techs.noah"),
		])
		self.assertEqual(len(profiles), 4)
		self.assertEqual(profiles[manifest.resolve("techs.marcus")]["assignment_tier"], 1)
		self.assertEqual(profiles[manifest.resolve("techs.noah")]["assignment_tier"], 2)
		self.assertAlmostEqual(profiles[manifest.resolve("techs.noah")]["target_load_weight"], 0.5, places=2)
		self.assertEqual(
			profiles[manifest.resolve("techs.marcus")]["skills"]["High Voltage & Battery"]["max_difficulty"],
			0,
		)
		self.assertEqual(
			profiles[manifest.resolve("techs.noah")]["skills"]["High Voltage & Battery"]["max_difficulty"],
			3,
		)

		hv_pkg = manifest.resolve("packages.hv_pyro_fuse_replace")
		self.assertEqual(get_required_skills_for_packages([hv_pkg]), [("High Voltage & Battery", 3)])

	def test_technician_skill_validation_and_delete_protection(self):
		"""Technician Skill validates scarcity_weight > 0 and blocks deletion while referenced."""
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Skill",
				"skill_name": f"{TEST_PREFIX}BadWeight",
				"scarcity_weight": 0,
			}).insert(ignore_permissions=True)

		skill = self._create_skill("DelGuard")
		emp = self._create_temp_employee("DelGuard")
		profile = self.track(
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"assignment_tier": 1,
				"target_load_weight": 1.0,
				"skills": [{"skill": skill.name, "max_difficulty": 2, "skill_role": "Standard"}],
			}).insert(ignore_permissions=True)
		)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Technician Skill", skill.name, ignore_permissions=True)

		# Removing profile frees the skill for clean deletion
		frappe.delete_doc("Technician Profile", profile.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Technician Skill", skill.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Technician Skill", skill.name))

	def test_technician_profile_and_service_package_validations(self):
		"""Technician Profile and Service Package enforce strict bounds and uniqueness rules."""
		emp = self._create_temp_employee("ValCheck")
		skill_a = self._create_skill("ValA")

		# Duplicate skills rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"skills": [
					{"skill": skill_a.name, "max_difficulty": 1, "skill_role": "Standard"},
					{"skill": skill_a.name, "max_difficulty": 2, "skill_role": "Preferred"},
				],
			}).insert(ignore_permissions=True)

		# max_difficulty outside 0..3 rejected
		for bad_diff in (-1, 4):
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({
					"doctype": "Technician Profile",
					"employee": emp.name,
					"skills": [{"skill": skill_a.name, "max_difficulty": bad_diff, "skill_role": "Standard"}],
				}).insert(ignore_permissions=True)

		# target_load_weight <= 0 rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"target_load_weight": 0,
			}).insert(ignore_permissions=True)

		# assignment_tier < 1 rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"assignment_tier": 0,
			}).insert(ignore_permissions=True)

		# Duplicate day_of_week rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"weekly_schedule": [
					{"day_of_week": "Monday", "is_working_day": 1, "shift_start": "08:00:00", "shift_end": "17:00:00"},
					{"day_of_week": "Monday", "is_working_day": 0},
				],
			}).insert(ignore_permissions=True)

		# shift_start >= shift_end rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Technician Profile",
				"employee": emp.name,
				"weekly_schedule": [
					{"day_of_week": "Tuesday", "is_working_day": 1, "shift_start": "17:00:00", "shift_end": "08:00:00"},
				],
			}).insert(ignore_permissions=True)

		# Service Package duplicate required_skills and invalid difficulty_level rejected
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Service Package",
				"package_name": f"{TEST_PREFIX}DupSkillPkg",
				"required_skills": [
					{"skill": skill_a.name, "difficulty_level": 1},
					{"skill": skill_a.name, "difficulty_level": 2},
				],
			}).insert(ignore_permissions=True)

		for bad_pkg_diff in (0, 4):
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({
					"doctype": "Service Package",
					"package_name": f"{TEST_PREFIX}BadDiffPkg_{bad_pkg_diff}",
					"required_skills": [{"skill": skill_a.name, "difficulty_level": bad_pkg_diff}],
				}).insert(ignore_permissions=True)

	def test_skills_contract_helpers(self):
		"""skills.py normalizes unions, resolves RO/SE/package requirements, and checks qualification/role."""
		self.assertEqual(
			normalize_skill_requirements([("Alignment", 1), ("Alignment", 3), ("Brakes & Suspension", 2)]),
			[("Alignment", 3), ("Brakes & Suspension", 2)],
		)
		self.assertEqual(
			normalize_skill_requirements(json.dumps([{"skill": "Alignment", "difficulty_level": 2}])),
			[("Alignment", 2)],
		)

		manifest = self.get_manifest()
		pkg_brake = manifest.resolve("packages.front_brake_overhaul")
		pkg_hv_diag = manifest.resolve("packages.hv_isolation_diagnostic")
		pkg_hv_pyro = manifest.resolve("packages.hv_pyro_fuse_replace")

		pkg_map = load_package_skills_map([pkg_brake, pkg_hv_diag])
		self.assertEqual(pkg_map[pkg_brake], [("Brakes & Suspension", 2)])
		self.assertEqual(
			pkg_map[pkg_hv_diag],
			[("General Diagnostics", 2), ("High Voltage & Battery", 2)],
		)

		# Union takes max difficulty across packages
		union = get_required_skills_for_packages([pkg_hv_diag, pkg_hv_pyro])
		self.assertEqual(union, [("General Diagnostics", 2), ("High Voltage & Battery", 3)])

		# RO resolution respects explicit service skill and skips Declined rows
		ro_dict = {
			"services": [
				{"name": "s1", "status": "Approved", "required_skill": "Alignment", "difficulty_level": 2},
				{"name": "s2", "status": "Declined", "required_skill": "High Voltage & Battery", "difficulty_level": 3},
				{"name": "s3", "status": "Estimating", "service_package": pkg_brake},
			]
		}
		self.assertEqual(
			get_required_skills_for_repair_order(ro_dict),
			[("Alignment", 2), ("Brakes & Suspension", 2)],
		)
		self.assertEqual(
			get_required_skills_for_repair_order(ro_dict, selected_service_names=["s1"]),
			[("Alignment", 2)],
		)

		# Schedule Entry resolution priority: required_skills_json > booked_packages_json > entry_type
		self.assertEqual(
			get_required_skills_for_schedule_entry({
				"required_skills_json": json.dumps([["Drive Unit & Thermal", 3]]),
				"entry_type": "Diagnostic",
			}),
			[("Drive Unit & Thermal", 3)],
		)
		self.assertEqual(
			get_required_skills_for_schedule_entry({
				"booked_packages_json": json.dumps([pkg_hv_pyro]),
				"entry_type": "Diagnostic",
			}),
			[("High Voltage & Battery", 3)],
		)
		self.assertEqual(
			get_required_skills_for_schedule_entry({"entry_type": "Diagnostic"}),
			[("General Diagnostics", 2)],
		)
		self.assertEqual(get_required_skills_for_schedule_entry({"entry_type": "Meeting"}), [])

		# Qualification & worst_skill_role checks on canonical profiles
		profiles = load_active_technician_profiles([
			manifest.resolve("techs.marcus"),
			manifest.resolve("techs.noah"),
		])
		marcus = profiles[manifest.resolve("techs.marcus")]
		noah = profiles[manifest.resolve("techs.noah")]

		self.assertFalse(is_technician_qualified(marcus, [("High Voltage & Battery", 1)]))
		self.assertIsNone(worst_skill_role(marcus, [("High Voltage & Battery", 1)]))

		self.assertTrue(is_technician_qualified(marcus, [("Tires & Maintenance", 3), ("Brakes & Suspension", 3)]))
		self.assertEqual(
			worst_skill_role(marcus, [("Tires & Maintenance", 3), ("Brakes & Suspension", 3)]),
			"Preferred",
		)
		self.assertEqual(
			worst_skill_role(marcus, [("Tires & Maintenance", 2), ("Alignment", 2)]),
			"Standard",
		)
		self.assertEqual(
			worst_skill_role(marcus, [("Tires & Maintenance", 2), ("Drive Unit & Thermal", 1)]),
			"Backup",
		)
		self.assertTrue(is_technician_qualified(noah, [("High Voltage & Battery", 3)]))
		self.assertEqual(worst_skill_role(noah, [("High Voltage & Battery", 3)]), "Preferred")
		self.assertTrue(is_technician_qualified(marcus, []))
		self.assertEqual(worst_skill_role(marcus, []), "Standard")

	def test_shifts_and_half_day_leave_helpers(self):
		"""shifts.py handles default hours, custom shifts, off-days, full-day & AM/PM half-day leave."""
		shop = dict(DEFAULT_SHOP_SETTINGS)
		profile = {
			"weekly_schedule": {
				"Monday": {
					"day_of_week": "Monday",
					"is_working_day": 1,
					"shift_start": "09:00:00",
					"shift_end": "18:00:00",
					"break_start": "13:00:00",
					"break_end": "13:30:00",
				},
				"Wednesday": {
					"day_of_week": "Wednesday",
					"is_working_day": 0,
				},
			}
		}

		# 2026-11-23 is Monday, 2026-11-24 is Tuesday, 2026-11-25 is Wednesday
		mon = get_technician_shift_window(profile, "2026-11-23", settings=shop)
		self.assertIsNotNone(mon)
		self.assertEqual(mon["start"], timedelta(hours=9))
		self.assertEqual(mon["end"], timedelta(hours=18))
		self.assertEqual(mon["net_minutes"], 510)

		tue = get_technician_shift_window(profile, "2026-11-24", settings=shop)
		self.assertIsNotNone(tue)
		self.assertEqual(tue["start"], timedelta(hours=8))
		self.assertEqual(tue["end"], timedelta(hours=17))
		self.assertEqual(tue["net_minutes"], 510)

		wed = get_technician_shift_window(profile, "2026-11-25", settings=shop)
		self.assertIsNone(wed)

		# Interval checks
		self.assertTrue(is_interval_within_shift(timedelta(hours=9), timedelta(hours=10), mon))
		self.assertFalse(is_interval_within_shift(timedelta(hours=8), timedelta(hours=9), mon))
		self.assertFalse(is_interval_within_shift(timedelta(hours=9), timedelta(hours=10), wed))

		# Full-day leave -> None
		full_leave = [{"from_date": "2026-11-24", "to_date": "2026-11-24", "half_day": 0}]
		self.assertIsNone(apply_leave_to_shift_window(tue, full_leave, "2026-11-24"))

		# AM half-day leave blocks [08:00, 12:00), leaving afternoon [12:30, 17:00) (270 min)
		am_leave = [{
			"from_date": "2026-11-24",
			"to_date": "2026-11-24",
			"half_day": 1,
			"half_day_date": "2026-11-24",
			"ism_half_day_period": "AM",
		}]
		after_am = apply_leave_to_shift_window(tue, am_leave, "2026-11-24")
		self.assertIsNotNone(after_am)
		self.assertEqual(after_am["start"], timedelta(hours=12, minutes=30))
		self.assertEqual(after_am["end"], timedelta(hours=17))
		self.assertEqual(after_am["net_minutes"], 270)
		self.assertFalse(is_interval_within_shift(timedelta(hours=9), timedelta(hours=10), after_am))
		self.assertTrue(is_interval_within_shift(timedelta(hours=13), timedelta(hours=14), after_am))

		# PM half-day leave blocks [12:30, 17:00), leaving morning [08:00, 12:00) (240 min)
		pm_leave = [{
			"from_date": "2026-11-24",
			"to_date": "2026-11-24",
			"half_day": 1,
			"half_day_date": "2026-11-24",
			"ism_half_day_period": "PM",
		}]
		after_pm = apply_leave_to_shift_window(tue, pm_leave, "2026-11-24")
		self.assertIsNotNone(after_pm)
		self.assertEqual(after_pm["start"], timedelta(hours=8))
		self.assertEqual(after_pm["end"], timedelta(hours=12))
		self.assertEqual(after_pm["net_minutes"], 240)
		self.assertTrue(is_interval_within_shift(timedelta(hours=9), timedelta(hours=11), after_pm))
		self.assertFalse(is_interval_within_shift(timedelta(hours=13), timedelta(hours=14), after_pm))

		# Blank half-day period conservatively blocks full day
		blank_half_leave = [{
			"from_date": "2026-11-24",
			"to_date": "2026-11-24",
			"half_day": 1,
			"half_day_date": "2026-11-24",
			"ism_half_day_period": "",
		}]
		self.assertIsNone(apply_leave_to_shift_window(tue, blank_half_leave, "2026-11-24"))

		# Available hours across Mon (8.5h) + Tue AM leave (4.5h) + Wed off (0h) + Thu holiday (0h) = 13.0h
		avail_hours = calculate_technician_available_hours(
			profile,
			am_leave,
			"2026-11-23",
			"2026-11-26",
			is_holiday_fn=lambda d: str(d) == "2026-11-26",
			settings=shop,
		)
		self.assertAlmostEqual(avail_hours, 13.0, places=2)

# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 3B tests for Technician Assignment Matrix REST API, Cockpit View enrichment, and Agent Scheduling tools."""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from ism.api.agent_monitoring import get_cockpit_snapshot
from ism.api.agent_scheduling import (
	_serialize_schedule_entry,
	get_tech_overview,
	rebalance_schedule_day,
)
from ism.api.technician_matrix import (
	apply_rebalance,
	get_assignment_matrix,
	preview_rebalance,
	save_assignment_policy,
	save_matrix_cell,
	toggle_assignment_lock,
	update_technician_profile,
)
from ism.board.cockpit_view import build_cockpit_view
from ism.scheduling.entry_queries import build_holiday_checker
from ism.scheduling.time_utils import get_shop_settings, invalidate_shop_settings_cache
from ism.tests.base import ISMTestCase


class TestTechnicianMatrixAPI(ISMTestCase):
	"""Integration tests for technician_matrix.py, cockpit_view/agent_monitoring enrichment, and agent_scheduling."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.manifest = self.get_manifest()
		self.marcus = self.manifest.resolve("techs.marcus")
		self.elena = self.manifest.resolve("techs.elena")
		self.devon = self.manifest.resolve("techs.devon")
		self.noah = self.manifest.resolve("techs.noah")
		self.bay_main = self.manifest.resolve("bays.main_lift")
		self.bay_align = self.manifest.resolve("bays.alignment")
		self._saved_settings: dict[str, object] = {}
		self._saved_profiles: dict[str, dict[str, object]] = {}

		# Snapshot ISM Settings assignment policy fields so tests always restore cleanly
		settings_doc = frappe.get_single("ISM Settings")
		for field in (
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
			self._saved_settings[field] = getattr(settings_doc, field, None)

	def tearDown(self):
		frappe.set_user("Administrator")
		# Restore any mutated Technician Profiles
		for emp_id, snap in self._saved_profiles.items():
			if frappe.db.exists("Technician Profile", emp_id):
				doc = frappe.get_doc("Technician Profile", emp_id)
				doc.assignment_tier = snap["assignment_tier"]
				doc.target_load_weight = snap["target_load_weight"]
				doc.daily_soft_cap_pct = snap["daily_soft_cap_pct"]
				doc.set("skills", snap["skills"])
				doc.set("weekly_schedule", snap["weekly_schedule"])
				doc.save(ignore_permissions=True)
		self._saved_profiles.clear()

		# Restore ISM Settings
		if self._saved_settings:
			settings_doc = frappe.get_single("ISM Settings")
			for key, val in self._saved_settings.items():
				setattr(settings_doc, key, val)
			settings_doc.save(ignore_permissions=True)
			invalidate_shop_settings_cache()
			self._saved_settings.clear()

		super().tearDown()

	def _snapshot_profile(self, emp_id: str) -> None:
		if emp_id in self._saved_profiles or not frappe.db.exists("Technician Profile", emp_id):
			return
		doc = frappe.get_doc("Technician Profile", emp_id)
		self._saved_profiles[emp_id] = {
			"assignment_tier": doc.assignment_tier,
			"target_load_weight": doc.target_load_weight,
			"daily_soft_cap_pct": doc.daily_soft_cap_pct,
			"skills": [
				{
					"skill": r.skill,
					"max_difficulty": r.max_difficulty,
					"skill_role": r.skill_role,
				}
				for r in (doc.skills or [])
			],
			"weekly_schedule": [
				{
					"day_of_week": r.day_of_week,
					"is_working_day": r.is_working_day,
					"shift_start": r.shift_start,
					"shift_end": r.shift_end,
					"break_start": r.break_start,
					"break_end": r.break_end,
				}
				for r in (doc.weekly_schedule or [])
			],
		}

	def _create_user_with_role(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"last_name": "Tester",
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		}).insert(ignore_permissions=True)
		self.track("User", email)
		return email

	def _next_workday(self, offset_days: int = 12) -> str:
		shop = get_shop_settings()
		is_holiday = build_holiday_checker(shop.get("holiday_list"), min_date=today())
		cursor = add_days(getdate(today()), offset_days)
		while cursor.weekday() >= 5 or is_holiday(cursor):
			cursor = add_days(cursor, 1)
		return str(cursor)

	def _make_schedule_entry(
		self,
		*,
		scheduled_date: str,
		scheduled_time: str = "08:00:00",
		estimated_duration: int = 60,
		assigned_technician: str | None = None,
		service_bay: str | None = None,
		entry_type: str = "Maintenance",
		status: str = "Scheduled",
		assignment_locked: int = 0,
		assignment_mode: str = "Auto",
		assignment_reason: str = "",
		is_comeback: int = 0,
	):
		doc = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": entry_type,
			"scheduled_date": scheduled_date,
			"scheduled_time": scheduled_time,
			"estimated_duration": estimated_duration,
			"assigned_technician": assigned_technician,
			"service_bay": service_bay or self.bay_main,
			"status": status,
			"assignment_locked": assignment_locked,
			"assignment_mode": assignment_mode,
			"assignment_reason": assignment_reason,
			"is_comeback": is_comeback,
		})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		if not doc.completion_date:
			doc.update_working_span()
			frappe.db.set_value(
				"Schedule Entry",
				doc.name,
				{
					"completion_date": doc.completion_date,
					"completion_time": doc.completion_time,
				},
				update_modified=False,
			)
		self.track("Schedule Entry", doc.name)
		return doc

	def test_rbac_enforcement_across_roles(self):
		"""Verify Guest & Technician are denied, Advisor gets read/rebalance/lock, Manager gets full CRUD."""
		workday = self._next_workday(14)
		se = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="09:00:00",
			assigned_technician=self.marcus,
		)
		self._snapshot_profile(self.marcus)

		manager = self._create_user_with_role("test_mtx_mgr", "ISM Manager")
		advisor = self._create_user_with_role("test_mtx_adv", "ISM Service Advisor")
		tech = self._create_user_with_role("test_mtx_tech", "ISM Technician")

		# 1. Guest denied everywhere
		frappe.set_user("Guest")
		for fn in (
			lambda: get_assignment_matrix(workday),
			lambda: save_matrix_cell(self.marcus, "Tires & Maintenance", 2, "Standard"),
			lambda: update_technician_profile(self.marcus, assignment_tier=2),
			lambda: save_assignment_policy(assignment_policy_mode="Equal Balance"),
			lambda: preview_rebalance(workday),
			lambda: apply_rebalance(workday),
			lambda: toggle_assignment_lock(se.name, 1),
			lambda: rebalance_schedule_day(target_date=workday, dry_run=True),
		):
			with self.assertRaises(frappe.PermissionError):
				fn()

		# 2. ISM Technician denied everywhere
		frappe.set_user(tech)
		for fn in (
			lambda: get_assignment_matrix(workday),
			lambda: save_matrix_cell(self.marcus, "Tires & Maintenance", 2, "Standard"),
			lambda: update_technician_profile(self.marcus, assignment_tier=2),
			lambda: save_assignment_policy(assignment_policy_mode="Equal Balance"),
			lambda: preview_rebalance(workday),
			lambda: apply_rebalance(workday),
			lambda: toggle_assignment_lock(se.name, 1),
			lambda: rebalance_schedule_day(target_date=workday, dry_run=True),
		):
			with self.assertRaises(frappe.PermissionError):
				fn()

		# 3. ISM Service Advisor: allowed read/rebalance/lock; denied matrix/policy writes
		frappe.set_user(advisor)
		mtx = get_assignment_matrix(workday)
		self.assertTrue(mtx["ok"])
		prev = preview_rebalance(workday)
		self.assertTrue(prev["ok"])
		app = apply_rebalance(workday)
		self.assertTrue(app["ok"])
		lock_res = toggle_assignment_lock(se.name, 1)
		self.assertTrue(lock_res["ok"])
		self.assertEqual(lock_res["assignment_locked"], 1)
		ag_res = rebalance_schedule_day(target_date=workday, dry_run=True)
		self.assertTrue(ag_res["success"])

		for write_fn in (
			lambda: save_matrix_cell(self.marcus, "Tires & Maintenance", 2, "Standard"),
			lambda: update_technician_profile(self.marcus, assignment_tier=2),
			lambda: save_assignment_policy(assignment_policy_mode="Equal Balance"),
		):
			with self.assertRaises(frappe.PermissionError):
				write_fn()

		# 4. ISM Manager: full CRUD allowed
		frappe.set_user(manager)
		cell_res = save_matrix_cell(self.marcus, "Tires & Maintenance", 3, "Preferred")
		self.assertTrue(cell_res["ok"])
		prof_res = update_technician_profile(self.marcus, assignment_tier=1, target_load_weight=1.0)
		self.assertTrue(prof_res["ok"])
		pol_res = save_assignment_policy(assignment_policy_mode="Tiered Waterfall")
		self.assertTrue(pol_res["ok"])

	def test_get_assignment_matrix_frozen_contract(self):
		"""Verify get_assignment_matrix returns exact frozen keys for policy, skills, and technicians."""
		workday = self._next_workday(15)
		res = get_assignment_matrix(workday)

		self.assertTrue(res["ok"])
		self.assertEqual(res["date"], workday)

		expected_policy_keys = {
			"assignment_policy_mode",
			"fairness_window_days",
			"tier_spillover_threshold_pct",
			"fairness_weight",
			"skill_conservation_weight",
			"skill_preference_weight",
			"schedule_packing_weight",
			"enable_morning_auto_rebalance",
			"auto_rebalance_skew_threshold_pct",
		}
		self.assertEqual(set(res["policy"].keys()), expected_policy_keys)

		self.assertGreaterEqual(len(res["skills"]), 6)
		for sk in res["skills"]:
			self.assertIn("name", sk)
			self.assertIn("scarcity_weight", sk)
			self.assertIn("description", sk)

		tech_by_emp = {t["employee"]: t for t in res["technicians"]}
		self.assertIn(self.marcus, tech_by_emp)
		self.assertIn(self.noah, tech_by_emp)

		marcus_row = tech_by_emp[self.marcus]
		self.assertEqual(marcus_row["assignment_tier"], 1)
		self.assertIn("Tires & Maintenance", marcus_row["cells"])
		self.assertEqual(
			set(marcus_row["cells"]["Tires & Maintenance"].keys()),
			{"max_difficulty", "skill_role"},
		)
		self.assertEqual(
			set(marcus_row["fairness"].keys()),
			{"assigned_frt_hours", "available_shift_hours", "normalized_load", "target_day_util_pct"},
		)
		self.assertIsInstance(marcus_row["weekly_schedule"], list)

	def test_save_matrix_cell_profile_and_policy_persistence(self):
		"""Verify matrix cell writes, profile/shift updates, policy updates, and validation errors."""
		self._snapshot_profile(self.marcus)

		# Save matrix cell
		cell_res = save_matrix_cell(self.marcus, "High Voltage & Battery", 2, "Backup")
		self.assertTrue(cell_res["ok"])
		self.assertEqual(cell_res["cell"], {"max_difficulty": 2, "skill_role": "Backup"})

		# Validation errors on cell
		with self.assertRaises(frappe.ValidationError):
			save_matrix_cell(self.marcus, "High Voltage & Battery", 4, "Standard")
		with self.assertRaises(frappe.ValidationError):
			save_matrix_cell(self.marcus, "High Voltage & Battery", 2, "InvalidRole")

		# Update profile + weekly schedule (JSON string)
		schedule_json = json.dumps([
			{
				"day_of_week": "Monday",
				"is_working_day": 1,
				"shift_start": "08:30",
				"shift_end": "17:00",
				"break_start": "12:00",
				"break_end": "12:30",
			},
			{
				"day_of_week": "Saturday",
				"is_working_day": 0,
			},
		])
		prof_res = update_technician_profile(
			self.marcus,
			assignment_tier=2,
			target_load_weight=0.75,
			daily_soft_cap_pct=80.0,
			weekly_schedule=schedule_json,
		)
		self.assertTrue(prof_res["ok"])
		self.assertEqual(prof_res["profile"]["assignment_tier"], 2)
		self.assertAlmostEqual(flt(prof_res["profile"]["target_load_weight"]), 0.75, places=2)
		self.assertAlmostEqual(flt(prof_res["profile"]["daily_soft_cap_pct"]), 80.0, places=1)
		self.assertEqual(len(prof_res["profile"]["weekly_schedule"]), 2)
		self.assertEqual(prof_res["profile"]["weekly_schedule"][0]["shift_start"], "08:30:00")

		# Clear daily_soft_cap_pct back to None
		cleared_res = update_technician_profile(self.marcus, daily_soft_cap_pct="")
		self.assertIsNone(cleared_res["profile"]["daily_soft_cap_pct"])

		# Save assignment policy
		pol_res = save_assignment_policy(
			assignment_policy_mode="Weighted Share",
			fairness_window_days=14,
			tier_spillover_threshold_pct=90.0,
			fairness_weight=0.60,
			skill_conservation_weight=0.20,
			skill_preference_weight=0.10,
			schedule_packing_weight=0.10,
			enable_morning_auto_rebalance=1,
			auto_rebalance_skew_threshold_pct=20.0,
		)
		self.assertTrue(pol_res["ok"])
		self.assertEqual(pol_res["policy"]["assignment_policy_mode"], "Weighted Share")
		self.assertEqual(pol_res["policy"]["fairness_window_days"], 14)
		self.assertEqual(pol_res["policy"]["enable_morning_auto_rebalance"], 1)
		self.assertAlmostEqual(pol_res["policy"]["tier_spillover_threshold_pct"], 90.0, places=1)

		# Validation errors on policy
		with self.assertRaises(frappe.ValidationError):
			save_assignment_policy(assignment_policy_mode="Unknown Mode")
		with self.assertRaises(frappe.ValidationError):
			save_assignment_policy(fairness_window_days=0)

	def test_preview_apply_rebalance_and_toggle_lock(self):
		"""Verify preview_rebalance, apply_rebalance, and toggle_assignment_lock end-to-end."""
		workday = self._next_workday(18)

		# Stack two unlocked 120m Maintenance entries on marcus while elena & devon are completely free
		se1 = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=0,
		)
		se2 = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="10:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_align,
			assignment_locked=0,
		)

		# Preview should propose moving at least one entry off marcus without mutating DB
		prev = preview_rebalance(workday)
		self.assertTrue(prev["ok"])
		self.assertEqual(prev["date"], workday)
		self.assertGreaterEqual(len(prev["changes"]), 1)
		self.assertEqual(frappe.db.get_value("Schedule Entry", se1.name, "assigned_technician"), self.marcus)
		self.assertEqual(frappe.db.get_value("Schedule Entry", se2.name, "assigned_technician"), self.marcus)

		# Lock se1 via toggle_assignment_lock -> se1 must stay on marcus
		lock_res = toggle_assignment_lock(se1.name, 1)
		self.assertTrue(lock_res["ok"])
		self.assertEqual(lock_res["assignment_locked"], 1)
		self.assertEqual(cint(frappe.db.get_value("Schedule Entry", se1.name, "assignment_locked")), 1)

		# Apply rebalance targeting only se2 via JSON entry_names
		apply_res = apply_rebalance(workday, entry_names=json.dumps([se2.name]))
		self.assertTrue(apply_res["ok"])
		self.assertEqual(apply_res["applied_count"], 1)
		self.assertEqual(frappe.db.get_value("Schedule Entry", se1.name, "assigned_technician"), self.marcus)
		self.assertNotEqual(frappe.db.get_value("Schedule Entry", se2.name, "assigned_technician"), self.marcus)
		self.assertEqual(frappe.db.get_value("Schedule Entry", se2.name, "assignment_mode"), "Rebalanced")

		# Unlock se1
		unlock_res = toggle_assignment_lock(se1.name, 0)
		self.assertEqual(unlock_res["assignment_locked"], 0)
		self.assertEqual(cint(frappe.db.get_value("Schedule Entry", se1.name, "assignment_locked")), 0)

	def test_cockpit_snapshot_and_view_enrichment(self):
		"""Verify get_cockpit_snapshot and build_cockpit_view expose policy mode, fairness, tiers, and block lock/reason."""
		workday = self._next_workday(20)
		se = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="09:00:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=1,
			assignment_mode="Manual Override",
			assignment_reason="Pinned by advisor for customer request",
			is_comeback=1,
		)

		snap = get_cockpit_snapshot(date=workday)
		self.assertTrue(snap["success"])
		data = snap["data"]
		self.assertIn("assignment_policy_mode", data)
		self.assertIn("fairness_ledger", data)
		self.assertIn("technician_profiles", data)
		self.assertIn(self.marcus, data["fairness_ledger"])

		se_rows = [r for r in data["schedule_entries"] if r["name"] == se.name]
		self.assertEqual(len(se_rows), 1)
		self.assertEqual(se_rows[0]["assignment_mode"], "Manual Override")
		self.assertEqual(se_rows[0]["assignment_locked"], 1)
		self.assertEqual(se_rows[0]["assignment_reason"], "Pinned by advisor for customer request")
		self.assertEqual(se_rows[0]["is_comeback"], 1)

		view = build_cockpit_view(snap)
		self.assertEqual(view["assignment_policy_mode"], data["assignment_policy_mode"])
		self.assertEqual(view["swimlane"]["assignment_policy_mode"], data["assignment_policy_mode"])

		# Verify tech_lanes enrichment
		tech_lanes_by_id = {lane["id"]: lane for lane in view["swimlane"]["tech_lanes"]}
		self.assertIn(self.marcus, tech_lanes_by_id)
		self.assertIn(self.noah, tech_lanes_by_id)
		marcus_lane = tech_lanes_by_id[self.marcus]
		self.assertEqual(marcus_lane["assignment_tier"], 1)
		self.assertEqual(marcus_lane["tier_label"], "Workhorse")
		self.assertIn("fairness_load_pct", marcus_lane)
		self.assertIn("assigned_frt_hours_7d", marcus_lane)
		self.assertIn("available_shift_hours_7d", marcus_lane)

		noah_lane = tech_lanes_by_id[self.noah]
		self.assertEqual(noah_lane["assignment_tier"], 2)
		self.assertEqual(noah_lane["tier_label"], "Buffer")

		# Verify block enrichment in both bay_lanes and tech_lanes
		tech_blocks = [b for b in marcus_lane["blocks"] if b["name"] == se.name]
		self.assertEqual(len(tech_blocks), 1)
		blk = tech_blocks[0]
		self.assertEqual(blk["assignment_mode"], "Manual Override")
		self.assertIs(blk["assignment_locked"], True)
		self.assertEqual(blk["assignment_reason"], "Pinned by advisor for customer request")
		self.assertIs(blk["is_comeback"], True)

		bay_blocks = [
			b
			for lane in view["swimlane"]["bay_lanes"]
			for b in lane["blocks"]
			if b["name"] == se.name
		]
		self.assertEqual(len(bay_blocks), 1)
		self.assertIs(bay_blocks[0]["assignment_locked"], True)
		self.assertEqual(bay_blocks[0]["assignment_mode"], "Manual Override")

	def test_agent_scheduling_enrichment_and_rebalance_tool(self):
		"""Verify _serialize_schedule_entry, get_tech_overview, and rebalance_schedule_day agent facade."""
		workday = self._next_workday(22)
		se1 = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=0,
			assignment_mode="Auto",
			assignment_reason="Tier 1 fairness leader",
		)
		se2 = self._make_schedule_entry(
			scheduled_date=workday,
			scheduled_time="10:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_align,
			assignment_locked=0,
		)

		serialized = _serialize_schedule_entry(frappe.get_doc("Schedule Entry", se1.name))
		self.assertEqual(serialized["assignment_mode"], "Auto")
		self.assertEqual(serialized["assignment_locked"], 0)
		self.assertEqual(serialized["assignment_reason"], "Tier 1 fairness leader")

		overview_res = get_tech_overview(date=workday, include_summary=True)
		self.assertTrue(overview_res["success"])
		self.assertIn("fairness_ledger", overview_res["data"])
		self.assertIn("assignment_policy_mode", overview_res["data"])
		tech_rows = {t["employee"]: t for t in overview_res["data"].get("technicians") or []}
		self.assertIn(self.marcus, tech_rows)
		self.assertEqual(tech_rows[self.marcus]["tier_label"], "Workhorse")
		self.assertIn("fairness_load_pct", tech_rows[self.marcus])
		self.assertIn("assigned_frt_hours_7d", tech_rows[self.marcus])

		# Dry-run via agent_scheduling.rebalance_schedule_day
		dry_res = rebalance_schedule_day(target_date=workday, dry_run=True, include_summary=True)
		self.assertTrue(dry_res["success"])
		self.assertTrue(dry_res["data"]["dry_run"])
		self.assertGreaterEqual(len(dry_res["data"]["changes"]), 1)
		self.assertIn("summary_card", dry_res)

		# Live apply via agent_scheduling.rebalance_schedule_day
		live_res = rebalance_schedule_day(target_date=workday, dry_run=False, include_summary=True)
		self.assertTrue(live_res["success"])
		self.assertFalse(live_res["data"]["dry_run"])
		self.assertGreaterEqual(live_res["data"]["applied_count"], 1)
		# Verify at least one of se1/se2 was moved off marcus
		t1 = frappe.db.get_value("Schedule Entry", se1.name, "assigned_technician")
		t2 = frappe.db.get_value("Schedule Entry", se2.name, "assigned_technician")
		self.assertTrue(t1 != self.marcus or t2 != self.marcus)

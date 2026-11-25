# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 2B tests for fairness ledger, 4-stage technician assignment, auto-split, and rebalancer."""

from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe.utils import add_days, cint, flt, getdate, now_datetime, today

from ism.scheduling.assignment import plan_multi_service_split, select_technician_for_slot
from ism.scheduling.entry_queries import build_holiday_checker
from ism.scheduling.fairness import compute_fairness_ledger
from ism.scheduling.rebalancer import (
	apply_rebalance_day,
	on_leave_application_change,
	on_repair_order_lock_assignments,
	on_schedule_entry_change,
	preview_rebalance_day,
	run_morning_auto_rebalance,
)
from ism.scheduling.time_utils import get_shop_settings
from ism.setup_tasks import setup_leave_custom_fields
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestTechnicianAssignment(ISMTestCase):
	"""End-to-end unit & DB tests for Wave 2B fairness, assignment, split, and rebalancer."""

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
		self._saved_settings: dict[str, object] = {}

	def tearDown(self):
		if self._saved_settings:
			settings_doc = frappe.get_single("ISM Settings")
			for key, val in self._saved_settings.items():
				setattr(settings_doc, key, val)
			settings_doc.save(ignore_permissions=True)
			self._saved_settings.clear()
		frappe.flags.ism_rebalancing = False
		frappe.set_user("Administrator")
		super().tearDown()

	def _set_ism_settings(self, **kwargs) -> None:
		settings_doc = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			if key not in self._saved_settings:
				self._saved_settings[key] = getattr(settings_doc, key, None)
			setattr(settings_doc, key, val)
		settings_doc.save(ignore_permissions=True)

	def _next_workdays(self, count: int = 5, start_offset: int = 10) -> list[str]:
		"""Return `count` consecutive future non-holiday weekdays (Mon-Fri)."""
		shop = get_shop_settings()
		is_holiday = build_holiday_checker(shop.get("holiday_list"), min_date=today())
		cursor = add_days(getdate(today()), start_offset)
		while True:
			days = [add_days(cursor, i) for i in range(count)]
			if all(d.weekday() < 5 and not is_holiday(d) for d in days):
				return [str(d) for d in days]
			cursor = add_days(cursor, 1)

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
		repair_order: str | None = None,
		assignment_locked: int = 0,
		is_comeback: int = 0,
		waiter_preference: str = "Drop-Off",
		required_skills: list[tuple[str, int]] | None = None,
	):
		payload = {
			"doctype": "Schedule Entry",
			"entry_type": entry_type,
			"scheduled_date": scheduled_date,
			"scheduled_time": scheduled_time,
			"estimated_duration": estimated_duration,
			"assigned_technician": assigned_technician,
			"service_bay": service_bay,
			"status": status,
			"repair_order": repair_order,
			"assignment_locked": assignment_locked,
			"is_comeback": is_comeback,
			"waiter_preference": waiter_preference,
		}
		if required_skills is not None:
			payload["required_skills_json"] = json.dumps(required_skills)
		doc = frappe.get_doc(payload)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		if not doc.completion_date:
			# Ensure completion_date/time are populated for clean interval math
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
			doc.reload()
		return self.track(doc)

	def _make_repair_order(
		self,
		*,
		status: str = "Draft Estimate",
		approval_status: str = "Estimating",
		manual_parts_hold: int = 0,
		services: list[dict] | None = None,
	):
		customer = self.manifest.resolve("customers.retail_1")
		vehicle = self.manifest.resolve("vehicles.veh_model3_liam")
		ro_payload = {
			"doctype": "Repair Order",
			"company": self.manifest.company,
			"customer": customer,
			"vehicle": vehicle,
			"odometer_in": 25000,
			"status": status,
			"approval_status": approval_status,
			"manual_parts_hold": manual_parts_hold,
			"services": services or [],
		}
		ro = frappe.get_doc(ro_payload)
		ro.insert(ignore_permissions=True)
		return self.track(ro)

	def _make_approved_leave(
		self,
		employee: str,
		from_date: str,
		to_date: str,
		*,
		half_day: int = 0,
		half_day_date: str | None = None,
		ism_half_day_period: str = "",
	):
		leave_type = frappe.db.get_value("Leave Type", {}, "name")
		if not leave_type:
			lt = frappe.get_doc({
				"doctype": "Leave Type",
				"leave_type_name": f"{TEST_PREFIX}Annual_{frappe.generate_hash(length=4)}",
				"allow_negative": 1,
			}).insert(ignore_permissions=True)
			self.track(lt)
			leave_type = lt.name

		doc = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": employee,
			"company": self.manifest.company,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date,
			"half_day": half_day,
			"half_day_date": half_day_date or (from_date if half_day else None),
			"ism_half_day_period": ism_half_day_period,
			"status": "Approved",
			"description": f"{TEST_PREFIX}TestLeave",
		})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			"Leave Application",
			doc.name,
			{
				"docstatus": 1,
				"status": "Approved",
				"ism_half_day_period": ism_half_day_period,
			},
			update_modified=False,
		)
		doc.reload()
		return self.track(doc)

	def _make_custom_tech(
		self,
		suffix: str,
		*,
		assignment_tier: int = 1,
		target_load_weight: float = 1.0,
		daily_soft_cap_pct: float | None = None,
		skills: list[dict] | None = None,
		weekly_schedule: list[dict] | None = None,
	) -> tuple[str, str]:
		emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": f"{TEST_PREFIX}Tech",
			"last_name": suffix,
			"employee_name": f"{TEST_PREFIX}Tech {suffix}",
			"status": "Active",
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2023-01-01",
			"company": self.manifest.company,
			"designation": "Technician",
			"holiday_list": self.manifest.holiday_list,
		}).insert(ignore_permissions=True)
		self.track(emp)

		prof_payload = {
			"doctype": "Technician Profile",
			"employee": emp.name,
			"is_active": 1,
			"assignment_tier": assignment_tier,
			"target_load_weight": target_load_weight,
			"skills": skills or [],
			"weekly_schedule": weekly_schedule or [],
		}
		if daily_soft_cap_pct is not None:
			prof_payload["daily_soft_cap_pct"] = daily_soft_cap_pct
		prof = frappe.get_doc(prof_payload).insert(ignore_permissions=True)
		self.track(prof)
		return emp.name, prof.name

	def _make_custom_skill(self, suffix: str, scarcity_weight: float = 1.0) -> str:
		doc = frappe.get_doc({
			"doctype": "Technician Skill",
			"skill_name": f"{TEST_PREFIX}Skill_{suffix}_{frappe.generate_hash(length=5)}",
			"is_active": 1,
			"scarcity_weight": scarcity_weight,
		}).insert(ignore_permissions=True)
		self.track(doc)
		return doc.name

	# ------------------------------------------------------------------
	# 1. Fairness Ledger Tests
	# ------------------------------------------------------------------

	def test_fairness_ledger_frt_comeback_and_leave_normalization(self):
		"""Rolling 7d ledger uses RO FRT, excludes comebacks/cancelled, and normalizes by leave-adjusted hours."""
		days = self._next_workdays(count=5, start_offset=14)
		day0, day1, day2, day3, target_day = days[0], days[1], days[2], days[3], days[4]
		old_day = str(add_days(getdate(target_day), -10))  # Outside 7-day window

		# RO for Marcus with 2.5h approved FRT + 3.0h declined FRT (declined must be excluded)
		ro_marcus = self._make_repair_order(
			services=[
				{
					"service_title": "Brake Service",
					"status": "Approved",
					"labor_hours": 2.5,
					"required_skill": "Brakes & Suspension",
					"difficulty_level": 2,
				},
				{
					"service_title": "Declined Alignment",
					"status": "Declined",
					"labor_hours": 3.0,
					"required_skill": "Alignment",
					"difficulty_level": 2,
				},
			]
		)
		se_marcus_ro = self._make_schedule_entry(
			scheduled_date=day2,
			scheduled_time="08:00:00",
			estimated_duration=60,  # Should use RO FRT 2.5h instead of 1.0h fallback
			assigned_technician=self.marcus,
			repair_order=ro_marcus.name,
			status="Completed",
		)
		# Comeback entry on target_day: 0 FRT credit, but counts 120m toward target_day_minutes!
		self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			is_comeback=1,
			status="Scheduled",
		)
		# Cancelled entry & out-of-window entry on Marcus: excluded completely
		self._make_schedule_entry(
			scheduled_date=day3,
			scheduled_time="10:00:00",
			estimated_duration=180,
			assigned_technician=self.marcus,
			status="Cancelled",
		)
		self._make_schedule_entry(
			scheduled_date=old_day,
			scheduled_time="08:00:00",
			estimated_duration=240,
			assigned_technician=self.marcus,
			status="Completed",
		)

		# Devon: 2 full days off + 1 AM half-day leave in window, plus 2.5h fallback duration job
		self._make_approved_leave(self.devon, day0, day1, half_day=0)
		self._make_approved_leave(
			self.devon,
			day2,
			day2,
			half_day=1,
			half_day_date=day2,
			ism_half_day_period="AM",
		)
		self._make_schedule_entry(
			scheduled_date=day3,
			scheduled_time="09:00:00",
			estimated_duration=150,  # 2.5h fallback (no RO)
			assigned_technician=self.devon,
			status="Scheduled",
		)

		ledger = compute_fairness_ledger(
			target_day,
			window_days=7,
			employee_ids=[self.marcus, self.devon],
		)

		m_row = ledger[self.marcus]
		d_row = ledger[self.devon]

		# Marcus: 2.5h FRT (declined, cancelled, old, and comeback all excluded)
		self.assertAlmostEqual(m_row["assigned_frt_hours"], 2.5, places=2)
		self.assertEqual(m_row["target_day_minutes"], 120)
		self.assertEqual(m_row["target_day_shift_minutes"], 510)
		self.assertAlmostEqual(m_row["target_day_util_pct"], round((120 / 510) * 100.0, 2), places=2)
		self.assertIsNotNone(m_row["last_assigned_at"])

		# Devon: same 2.5h FRT, fewer available shift hours due to full + AM half-day leaves -> higher normalized_load!
		self.assertAlmostEqual(d_row["assigned_frt_hours"], 2.5, places=2)
		self.assertLess(d_row["available_shift_hours"], m_row["available_shift_hours"])
		self.assertAlmostEqual(
			m_row["available_shift_hours"] - d_row["available_shift_hours"],
			8.5 + 8.5 + 4.0,  # 2 full days (17h) + AM half-day (4h blocked: 08:00-12:00) = 21.0h less
			places=2,
		)
		self.assertGreater(d_row["normalized_load"], m_row["normalized_load"])

		# exclude_entry omits Marcus's RO entry from FRT total
		ledger_excl = compute_fairness_ledger(
			target_day,
			window_days=7,
			employee_ids=[self.marcus],
			exclude_entry=se_marcus_ro.name,
		)
		self.assertAlmostEqual(ledger_excl[self.marcus]["assigned_frt_hours"], 0.0, places=2)

	# ------------------------------------------------------------------
	# 2. Stage 1 Hard Gate (Skills, Custom Shifts, AM/PM Leave, Overlap)
	# ------------------------------------------------------------------

	def test_hard_gate_skills_shifts_leaves_and_overlaps(self):
		"""Stage 1 enforces skill difficulty, custom weekly shifts, AM/PM half-day leave, and zero overlap."""
		target_day = self._next_workdays(count=1, start_offset=28)[0]
		day_name = [
			"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
		][getdate(target_day).weekday()]

		# High Voltage L3: only Noah qualifies among canonical techs
		hv_res = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="09:00:00",
			duration_minutes=60,
			required_skills=[("High Voltage & Battery", 3)],
			candidate_employee_ids=self.canonical_techs,
		)
		self.assertEqual(hv_res["assigned_technician"], self.noah)
		self.assertEqual(hv_res["assignment_mode"], "Auto")

		# Custom shift tech: starts at 10:00 on target_day
		skill_custom = self._make_custom_skill("ShiftGate")
		tech_late, _ = self._make_custom_tech(
			"LateStart",
			skills=[{"skill": skill_custom, "max_difficulty": 2, "skill_role": "Preferred"}],
			weekly_schedule=[{
				"day_of_week": day_name,
				"is_working_day": 1,
				"shift_start": "10:00:00",
				"shift_end": "17:00:00",
				"break_start": "12:00:00",
				"break_end": "12:30:00",
			}],
		)
		tech_off, _ = self._make_custom_tech(
			"OffDay",
			skills=[{"skill": skill_custom, "max_difficulty": 2, "skill_role": "Preferred"}],
			weekly_schedule=[{"day_of_week": day_name, "is_working_day": 0}],
		)

		# 08:30-09:30 blocked for both (late start + off-day) -> Unassigned
		early_res = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="08:30:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late, tech_off],
		)
		self.assertIsNone(early_res["assigned_technician"])
		self.assertEqual(early_res["assignment_mode"], "Unassigned")

		# 10:00-11:00 works for tech_late
		ok_res = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="10:00:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late, tech_off],
		)
		self.assertEqual(ok_res["assigned_technician"], tech_late)

		# AM half-day leave on tech_late blocks 10:00-11:00, allows 13:00-14:00
		self._make_approved_leave(
			tech_late,
			target_day,
			target_day,
			half_day=1,
			half_day_date=target_day,
			ism_half_day_period="AM",
		)
		am_blocked = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="10:00:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late],
		)
		self.assertIsNone(am_blocked["assigned_technician"])

		pm_allowed = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="13:00:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late],
		)
		self.assertEqual(pm_allowed["assigned_technician"], tech_late)

		# Blocking overlap excludes tech_late unless exclude_entry is passed
		existing = self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="13:00:00",
			estimated_duration=60,
			assigned_technician=tech_late,
		)
		overlap_res = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="13:30:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late],
		)
		self.assertIsNone(overlap_res["assigned_technician"])

		reschedule_self = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="13:30:00",
			duration_minutes=60,
			required_skills=[(skill_custom, 2)],
			candidate_employee_ids=[tech_late],
			exclude_entry=existing.name,
		)
		self.assertEqual(reschedule_self["assigned_technician"], tech_late)

	# ------------------------------------------------------------------
	# 3. Policy Modes: Tiered Waterfall (85% spillover), Equal Balance, Weighted Share
	# ------------------------------------------------------------------

	def test_policy_modes_tiered_waterfall_equal_balance_and_weighted_share(self):
		"""Verifies Tiered Waterfall 85% spillover, Equal Balance cross-tier competition, and Weighted Share."""
		target_day = self._next_workdays(count=1, start_offset=35)[0]
		base_shop = get_shop_settings()

		# Use Alignment L3: Elena is Tier 1 (Preferred, weight 1.0), Noah is Tier 2 (Standard, weight 0.5)
		# Give Elena 6h (360m = 70.6% < 85%) on target_day -> still stays in Tier 1 under Tiered Waterfall!
		self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="08:00:00",
			estimated_duration=360,
			assigned_technician=self.elena,
		)

		wf_shop = dict(base_shop, assignment_policy_mode="Tiered Waterfall", tier_spillover_threshold_pct=85.0)
		res_under_cap = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="15:00:00",
			duration_minutes=60,
			required_skills=[("Alignment", 3)],
			candidate_employee_ids=[self.elena, self.noah],
			settings=wf_shop,
		)
		self.assertEqual(res_under_cap["assigned_technician"], self.elena)
		self.assertFalse(res_under_cap["reason_details"]["spillover_used"])

		# Push Elena to 450m (88.2% >= 85%) on target_day -> Tier 1 spills over to Tier 2 (Noah)!
		self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="14:30:00",
			estimated_duration=90,
			assigned_technician=self.elena,
		)
		res_spillover = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="16:00:00",
			duration_minutes=60,
			required_skills=[("Alignment", 3)],
			candidate_employee_ids=[self.elena, self.noah],
			settings=wf_shop,
		)
		self.assertEqual(res_spillover["assigned_technician"], self.noah)
		self.assertTrue(res_spillover["reason_details"]["spillover_used"])

		# Equal Balance: Primary techs compete across tiers -> Noah (0h load) beats Elena (7.5h load)
		eq_shop = dict(base_shop, assignment_policy_mode="Equal Balance")
		res_eq = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="16:00:00",
			duration_minutes=60,
			required_skills=[("Alignment", 3)],
			candidate_employee_ids=[self.elena, self.noah],
			settings=eq_shop,
		)
		self.assertEqual(res_eq["assigned_technician"], self.noah)

		# Weighted Share: Elena has 7.5h (weight 1.0 -> 7.5/avail); give Noah 5.0h (weight 0.5 -> 10.0/avail)
		# Even though Noah has fewer raw hours (5.0 < 7.5), his weighted load is higher -> Elena wins!
		self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="08:00:00",
			estimated_duration=300,  # 5.0h
			assigned_technician=self.noah,
		)
		ws_shop = dict(
			base_shop,
			assignment_policy_mode="Weighted Share",
			fairness_weight=0.90,
			skill_conservation_weight=0.05,
			skill_preference_weight=0.05,
			schedule_packing_weight=0.0,
		)
		res_ws = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="16:00:00",
			duration_minutes=60,
			required_skills=[("Alignment", 3)],
			candidate_employee_ids=[self.elena, self.noah],
			settings=ws_shop,
		)
		self.assertEqual(res_ws["assigned_technician"], self.elena)

		# Role Gate: Backup tech excluded when a Primary tech is feasible (even if Backup has 0 load)
		# Drive Unit L1: Elena is Standard (Primary), Marcus is Backup -> Elena wins despite 7.5h load
		res_role_gate = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="16:00:00",
			duration_minutes=60,
			required_skills=[("Drive Unit & Thermal", 1)],
			candidate_employee_ids=[self.marcus, self.elena],
			settings=eq_shop,
		)
		self.assertEqual(res_role_gate["assigned_technician"], self.elena)
		self.assertEqual(res_role_gate["reason_details"]["role_group"], "Primary")

	# ------------------------------------------------------------------
	# 4. Skill Conservation, Preference & Deterministic Tie-Break
	# ------------------------------------------------------------------

	def test_skill_conservation_and_deterministic_tiebreak(self):
		"""Routine jobs conserve scarce specialists; identical candidates break ties deterministically."""
		target_day = self._next_workdays(count=1, start_offset=42)[0]

		# Routine Tires & Maintenance L1 across Tier 1 Workhorses at 0 load:
		# Devon (Tires L2 Pref, no HV/Drive Unit) must beat Marcus (L3 Tires/Brakes) and Elena (HV/Drive Unit)
		res = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="09:00:00",
			duration_minutes=60,
			required_skills=[("Tires & Maintenance", 1)],
			candidate_employee_ids=[self.marcus, self.elena, self.devon],
		)
		self.assertEqual(res["assigned_technician"], self.devon)
		self.assertIn("Tier 1", res["assignment_reason"])

		# Deterministic tie-break between two identical custom profiles: lower employee_id wins first
		skill_tie = self._make_custom_skill("TieCheck")
		t1, _ = self._make_custom_tech(
			"TieA",
			skills=[{"skill": skill_tie, "max_difficulty": 2, "skill_role": "Preferred"}],
		)
		t2, _ = self._make_custom_tech(
			"TieB",
			skills=[{"skill": skill_tie, "max_difficulty": 2, "skill_role": "Preferred"}],
		)
		expected_first = min(t1, t2)
		res_tie1 = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="10:00:00",
			duration_minutes=60,
			required_skills=[(skill_tie, 2)],
			candidate_employee_ids=[t1, t2],
		)
		self.assertEqual(res_tie1["assigned_technician"], expected_first)

	# ------------------------------------------------------------------
	# 5. Sticky Continuity (Comeback, Parts-Hold, Diagnostic -> Repair)
	# ------------------------------------------------------------------

	def test_sticky_continuity_all_three_rules_and_bypass_when_unqualified(self):
		"""Sticky Continuity prioritizes Comeback > Parts-Hold > Diagnostic->Repair, bypassing if unqualified."""
		days = self._next_workdays(count=2, start_offset=49)
		day0, day1 = days[0], days[1]

		# Heavily load Marcus so fairness would NEVER pick him over Devon
		self._make_schedule_entry(
			scheduled_date=day0,
			scheduled_time="08:00:00",
			estimated_duration=420,
			assigned_technician=self.marcus,
			status="Completed",
		)

		# (a) Comeback -> sticks to Marcus despite heavy load
		cb_res = select_technician_for_slot(
			scheduled_date=day1,
			scheduled_time="09:00:00",
			duration_minutes=60,
			required_skills=[("Tires & Maintenance", 1)],
			is_comeback=True,
			original_technician=self.marcus,
			candidate_employee_ids=[self.marcus, self.devon],
		)
		self.assertEqual(cb_res["assigned_technician"], self.marcus)
		self.assertEqual(cb_res["assignment_mode"], "Sticky Continuity")
		self.assertEqual(cb_res["reason_details"]["sticky_type"], "comeback")

		# (b) Parts-Hold Resume -> RO Waiting on Parts with prior Marcus entry sticks to Marcus
		ro_parts = self._make_repair_order(
			status="Waiting on Parts",
			manual_parts_hold=1,
			services=[{
				"service_title": "Brake Overhaul",
				"status": "Approved",
				"labor_hours": 2.0,
				"required_skill": "Brakes & Suspension",
				"difficulty_level": 2,
				"assigned_technician": self.marcus,
			}],
		)
		self._make_schedule_entry(
			scheduled_date=day0,
			scheduled_time="15:00:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			repair_order=ro_parts.name,
			entry_type="Repair",
			status="Completed",
		)
		ph_res = select_technician_for_slot(
			scheduled_date=day1,
			scheduled_time="10:00:00",
			duration_minutes=60,
			required_skills=[("Brakes & Suspension", 2)],
			repair_order=ro_parts.name,
			candidate_employee_ids=[self.marcus, self.devon],
		)
		self.assertEqual(ph_res["assigned_technician"], self.marcus)
		self.assertEqual(ph_res["assignment_mode"], "Sticky Continuity")
		self.assertEqual(ph_res["reason_details"]["sticky_type"], "parts_hold_resume")

		# (c) Diagnostic -> Repair follow-up -> sticks to Marcus from Completed Diagnostic SE
		ro_diag = self._make_repair_order(
			status="Authorized - In Progress",
			services=[{
				"service_title": "Brake Repair",
				"status": "Approved",
				"labor_hours": 1.5,
				"required_skill": "Brakes & Suspension",
				"difficulty_level": 2,
			}],
		)
		self._make_schedule_entry(
			scheduled_date=day0,
			scheduled_time="16:00:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			repair_order=ro_diag.name,
			entry_type="Diagnostic",
			status="Completed",
		)
		diag_res = select_technician_for_slot(
			scheduled_date=day1,
			scheduled_time="11:00:00",
			duration_minutes=60,
			required_skills=[("Brakes & Suspension", 2)],
			repair_order=ro_diag.name,
			candidate_employee_ids=[self.marcus, self.devon],
		)
		self.assertEqual(diag_res["assigned_technician"], self.marcus)
		self.assertEqual(diag_res["assignment_mode"], "Sticky Continuity")
		self.assertEqual(diag_res["reason_details"]["sticky_type"], "diagnostic_followup")

		# Bypass check: Marcus did Diagnostic, but follow-up requires HV L3 (Marcus is L0 Blocked) -> Noah wins!
		bypass_res = select_technician_for_slot(
			scheduled_date=day1,
			scheduled_time="13:00:00",
			duration_minutes=60,
			required_skills=[("High Voltage & Battery", 3)],
			repair_order=ro_diag.name,
			candidate_employee_ids=[self.marcus, self.noah],
		)
		self.assertEqual(bypass_res["assigned_technician"], self.noah)
		self.assertEqual(bypass_res["assignment_mode"], "Auto")
		self.assertTrue(bypass_res["reason_details"]["sticky_bypassed"])

	# ------------------------------------------------------------------
	# 6. Multi-Service RO Auto-Split
	# ------------------------------------------------------------------

	def test_multi_service_ro_auto_split(self):
		"""Detects needs_split=True when no single active tech holds union of skills, and partitions minimally."""
		target_day = self._next_workdays(count=1, start_offset=56)[0]
		skill_a = self._make_custom_skill("SplitAlpha")
		skill_b = self._make_custom_skill("SplitBeta")

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

		sel = select_technician_for_slot(
			scheduled_date=target_day,
			scheduled_time="09:00:00",
			duration_minutes=150,
			repair_order=ro.name,
		)
		self.assertIsNone(sel["assigned_technician"])
		self.assertEqual(sel["assignment_mode"], "Unassigned")
		self.assertTrue(sel["needs_split"])

		groups = plan_multi_service_split(ro.name)
		self.assertEqual(len(groups), 2)
		self.assertTrue(all(g["assignable"] for g in groups))
		self.assertEqual(set(groups[0]["qualified_technicians"] + groups[1]["qualified_technicians"]), {tech_a, tech_b})
		all_svc_names = {s.name for s in ro.services}
		grouped_svc_names = {n for g in groups for n in g["service_names"]}
		self.assertEqual(grouped_svc_names, all_svc_names)

		# Single-tech-compatible multi-service RO stays in 1 group
		ro_single = self._make_repair_order(
			services=[
				{
					"service_title": "Tire Rotation",
					"status": "Approved",
					"labor_hours": 0.5,
					"required_skill": "Tires & Maintenance",
					"difficulty_level": 1,
				},
				{
					"service_title": "Brake Pads",
					"status": "Approved",
					"labor_hours": 1.5,
					"required_skill": "Brakes & Suspension",
					"difficulty_level": 2,
				},
			]
		)
		single_groups = plan_multi_service_split(ro_single.name)
		self.assertEqual(len(single_groups), 1)
		self.assertTrue(single_groups[0]["assignable"])

	# ------------------------------------------------------------------
	# 7. Waiter P80 Spillover Avoidance & Packing
	# ------------------------------------------------------------------

	def test_waiter_p80_spillover_avoidance(self):
		"""Waiter bookings avoid slots immediately following a job whose P80 end time overlaps start_time."""
		days = self._next_workdays(count=2, start_offset=63)
		day0, day1 = days[0], days[1]
		skill_w = self._make_custom_skill("WaiterSkill")

		tech_risky, _ = self._make_custom_tech(
			"WaiterRisky",
			skills=[{"skill": skill_w, "max_difficulty": 2, "skill_role": "Preferred"}],
		)
		tech_safe, _ = self._make_custom_tech(
			"WaiterSafe",
			skills=[{"skill": skill_w, "max_difficulty": 2, "skill_role": "Preferred"}],
		)

		# Give both techs identical 1.0h load in the 7-day window:
		# tech_risky has a 60m Diagnostic 08:00-09:00 on day1 (P80 = 84m -> ends 09:24, overlapping 09:00!)
		# tech_safe had a 60m Diagnostic on day0 (free before 09:00 on day1)
		self._make_schedule_entry(
			scheduled_date=day1,
			scheduled_time="08:00:00",
			estimated_duration=60,
			assigned_technician=tech_risky,
			entry_type="Diagnostic",
			status="Scheduled",
		)
		self._make_schedule_entry(
			scheduled_date=day0,
			scheduled_time="08:00:00",
			estimated_duration=60,
			assigned_technician=tech_safe,
			entry_type="Diagnostic",
			status="Completed",
		)

		res = select_technician_for_slot(
			scheduled_date=day1,
			scheduled_time="09:00:00",
			duration_minutes=60,
			required_skills=[(skill_w, 2)],
			waiter_preference="Waiter",
			candidate_employee_ids=[tech_risky, tech_safe],
		)
		self.assertEqual(res["assigned_technician"], tech_safe)
		cand_map = {c["employee"]: c for c in res["reason_details"]["candidates"]}
		self.assertTrue(cand_map[tech_risky]["waiter_spillover_risk"])
		self.assertFalse(cand_map[tech_safe]["waiter_spillover_risk"])
		self.assertGreater(cand_map[tech_risky]["pack_cost"], cand_map[tech_safe]["pack_cost"])

	# ------------------------------------------------------------------
	# 8. Rebalancer: Preview, Apply, Locking & Bay/Time Preservation
	# ------------------------------------------------------------------

	def test_rebalancer_preview_apply_preserves_bay_time_and_locked_entries(self):
		"""Rebalancer balances unlocked future entries, never moves locked entries, bays, or times, and syncs ROS."""
		target_day = self._next_workdays(count=1, start_offset=70)[0]

		ro_sync = self._make_repair_order(
			services=[{
				"service_title": "Tire Service",
				"status": "Approved",
				"labor_hours": 2.0,
				"required_skill": "Tires & Maintenance",
				"difficulty_level": 1,
				"assigned_technician": self.marcus,
			}]
		)

		# Stack 4 jobs on Marcus (1 locked + 3 unlocked) -> huge Tier 1 skew
		se_locked = self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=1,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_u1 = self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="10:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_align,
			repair_order=ro_sync.name,
			assignment_locked=0,
			required_skills=[("Tires & Maintenance", 1)],
		)
		frappe.db.set_value(
			"Repair Order Service",
			ro_sync.services[0].name,
			"schedule_entry",
			se_u1.name,
			update_modified=False,
		)
		se_u2 = self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="12:30:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_hv,
			assignment_locked=0,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_u3 = self._make_schedule_entry(
			scheduled_date=target_day,
			scheduled_time="14:30:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=0,
			required_skills=[("Tires & Maintenance", 1)],
		)

		preview = preview_rebalance_day(target_day)
		self.assertEqual(preview["date"], target_day)
		self.assertEqual(preview["locked_count"], 1)
		self.assertEqual(preview["unlocked_count"], 3)
		self.assertGreater(preview["before_spread_pct"], preview["after_spread_pct"])
		self.assertTrue(len(preview["changes"]) >= 2)
		changed_names = {c["schedule_entry"] for c in preview["changes"]}
		self.assertNotIn(se_locked.name, changed_names)

		# Apply rebalance
		applied = apply_rebalance_day(target_day)
		self.assertEqual(applied["applied_count"], len(preview["changes"]))

		# Verify locked entry untouched
		se_locked.reload()
		self.assertEqual(se_locked.assigned_technician, self.marcus)

		# Verify bay & time preserved identically on all unlocked entries, mode='Rebalanced', and ROS synced
		for se, orig_bay, orig_time in (
			(se_u1, self.bay_align, "10:00:00"),
			(se_u2, self.bay_hv, "12:30:00"),
			(se_u3, self.bay_main, "14:30:00"),
		):
			se.reload()
			self.assertEqual(se.service_bay, orig_bay)
			self.assertEqual(str(se.scheduled_time).zfill(8), orig_time)
			if se.name in changed_names:
				self.assertEqual(se.assignment_mode, "Rebalanced")
				self.assertNotEqual(se.assigned_technician, self.marcus)

		se_u1.reload()
		ros_tech = frappe.db.get_value("Repair Order Service", ro_sync.services[0].name, "assigned_technician")
		self.assertEqual(ros_tech, se_u1.assigned_technician)

		# Past date is a clean no-op
		past_day = str(add_days(getdate(today()), -5))
		past_preview = preview_rebalance_day(past_day)
		self.assertEqual(past_preview["changes"], [])

	# ------------------------------------------------------------------
	# 9. Leave, Cancel, RO Lock & Morning Cron Triggers
	# ------------------------------------------------------------------

	def test_leave_cancel_ro_lock_and_morning_cron_triggers(self):
		"""Verifies approved leave reassignment + action items, cancel skew rebalance, RO lock, and morning cron."""
		days = self._next_workdays(count=2, start_offset=77)
		leave_day, cancel_day = days[0], days[1]

		# --- A. Leave Application Trigger (PM half-day leave + Full-day unresolvable) ---
		# Marcus gets PM half-day leave: morning entry stays; unlocked afternoon entry reassigns; locked afternoon creates SAI
		se_am_safe = self._make_schedule_entry(
			scheduled_date=leave_day,
			scheduled_time="09:00:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			service_bay=self.bay_main,
			assignment_locked=0,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_pm_unlocked = self._make_schedule_entry(
			scheduled_date=leave_day,
			scheduled_time="13:00:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			service_bay=self.bay_align,
			assignment_locked=0,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_pm_locked = self._make_schedule_entry(
			scheduled_date=leave_day,
			scheduled_time="14:30:00",
			estimated_duration=60,
			assigned_technician=self.marcus,
			service_bay=self.bay_hv,
			assignment_locked=1,
			required_skills=[("Tires & Maintenance", 1)],
		)
		leave_pm = self._make_approved_leave(
			self.marcus,
			leave_day,
			leave_day,
			half_day=1,
			half_day_date=leave_day,
			ism_half_day_period="PM",
		)
		on_leave_application_change(leave_pm, "on_submit")
		# Second call must deduplicate Shop Action Items
		on_leave_application_change(leave_pm, "on_submit")

		se_am_safe.reload()
		se_pm_unlocked.reload()
		se_pm_locked.reload()
		self.assertEqual(se_am_safe.assigned_technician, self.marcus)  # Morning unaffected by PM leave
		self.assertNotEqual(se_pm_unlocked.assigned_technician, self.marcus)
		self.assertEqual(se_pm_unlocked.assignment_mode, "Rebalanced")
		self.assertEqual(se_pm_unlocked.service_bay, self.bay_align)
		self.assertEqual(se_pm_locked.assigned_technician, self.marcus)

		locked_sais = frappe.get_all(
			"Shop Action Item",
			filters={
				"reference_doctype": "Schedule Entry",
				"reference_name": se_pm_locked.name,
				"category": "General",
				"source": "Agent",
			},
			pluck="name",
		)
		self.assertEqual(len(locked_sais), 1)
		for sai in locked_sais:
			self.track("Shop Action Item", sai)

		# Unresolvable leave conflict: Noah is the ONLY HV L3 tech; full-day leave creates deduplicated SAI
		se_hv_only = self._make_schedule_entry(
			scheduled_date=leave_day,
			scheduled_time="10:00:00",
			estimated_duration=60,
			assigned_technician=self.noah,
			service_bay=self.bay_hv,
			assignment_locked=0,
			required_skills=[("High Voltage & Battery", 3)],
		)
		leave_noah = self._make_approved_leave(self.noah, leave_day, leave_day, half_day=0)
		on_leave_application_change(leave_noah, "on_submit")
		unres_sais = frappe.get_all(
			"Shop Action Item",
			filters={
				"reference_doctype": "Schedule Entry",
				"reference_name": se_hv_only.name,
				"category": "General",
				"source": "Agent",
			},
			pluck="name",
		)
		self.assertEqual(len(unres_sais), 1)
		for sai in unres_sais:
			self.track("Shop Action Item", sai)

		# --- B. Cancellation Skew Trigger (on_schedule_entry_change) ---
		self._set_ism_settings(auto_rebalance_skew_threshold_pct=20.0)
		# Marcus has 2 x 120m (47%), Elena has 1 x 120m (23.5%), Devon has 1 x 120m (23.5%) -> cancel Devon's!
		se_m1 = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_m2 = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="10:00:00",
			estimated_duration=120,
			assigned_technician=self.marcus,
			required_skills=[("Tires & Maintenance", 1)],
		)
		self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.elena,
			required_skills=[("Tires & Maintenance", 1)],
		)
		se_dev_cancel = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="08:00:00",
			estimated_duration=120,
			assigned_technician=self.devon,
			required_skills=[("Tires & Maintenance", 1)],
		)
		frappe.db.set_value("Schedule Entry", se_dev_cancel.name, "status", "Cancelled")
		se_dev_cancel.reload()
		on_schedule_entry_change(se_dev_cancel, "on_update")

		se_m1.reload()
		se_m2.reload()
		self.assertIn(self.devon, {se_m1.assigned_technician, se_m2.assigned_technician})

		# --- C. Repair Order Auto-Lock Trigger ---
		ro_lock = self._make_repair_order(status="Draft Estimate")
		se_active = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="13:00:00",
			estimated_duration=60,
			assigned_technician=self.elena,
			repair_order=ro_lock.name,
			assignment_locked=0,
			status="Scheduled",
		)
		se_cancelled = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="14:00:00",
			estimated_duration=60,
			assigned_technician=self.elena,
			repair_order=ro_lock.name,
			assignment_locked=0,
			status="Cancelled",
		)
		ro_lock.status = "Checked In"
		on_repair_order_lock_assignments(ro_lock, "on_update")
		se_active.reload()
		se_cancelled.reload()
		self.assertEqual(cint(se_active.assignment_locked), 1)
		self.assertEqual(cint(se_cancelled.assignment_locked), 0)

		# Also verify picked_up_at locks an entry even if RO status stayed Draft Estimate
		ro_pickup = self._make_repair_order(status="Draft Estimate")
		se_pickup = self._make_schedule_entry(
			scheduled_date=cancel_day,
			scheduled_time="15:00:00",
			estimated_duration=60,
			assigned_technician=self.elena,
			repair_order=ro_pickup.name,
			assignment_locked=0,
			status="Confirmed",
		)
		ro_pickup.picked_up_at = now_datetime()
		on_repair_order_lock_assignments(ro_pickup, "on_update")
		se_pickup.reload()
		self.assertEqual(cint(se_pickup.assignment_locked), 1)

		# --- D. Morning Auto-Rebalance Cron Gate ---
		self._set_ism_settings(enable_morning_auto_rebalance=0)
		run_morning_auto_rebalance()  # Gated off: no-op
		self._set_ism_settings(enable_morning_auto_rebalance=1)
		run_morning_auto_rebalance()  # Enabled: runs cleanly without error

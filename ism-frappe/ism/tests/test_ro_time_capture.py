# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import flt, get_datetime

from ism.scheduling.time_utils import calculate_net_working_session, subtract_intervals
from ism.tests.base import ISMTestCase
from ism.time_calibration.contracts import SESSION_CLOSED, SESSION_OPEN


class TestROTimeCapture(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.customer = self.create_test_customer("Time Capture Customer")
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def _default_shop_cfg(self):
		return {
			"operating_hours_start": timedelta(hours=8),
			"operating_hours_end": timedelta(hours=17),
			"break_start": timedelta(hours=12),
			"break_end": timedelta(hours=12, minutes=30),
			"holiday_list": None,
		}

	def _create_approved_ro(self, services=None, odometer_in=42000):
		svc_rows = services or [
			{
				"service_title": "Front Control Arm Replacement",
				"status": "Approved",
				"labor_hours": 1.5,
				"labor_rate": 150.0,
			}
		]
		ro = frappe.get_doc(
			{
				"doctype": "Repair Order",
				"customer": self.customer.name,
				"vehicle": self.vehicle.name,
				"odometer_in": odometer_in,
				"approval_status": "Approved",
				"services": svc_rows,
			}
		)
		ro.insert()
		self.track(ro)
		return ro

	def _get_two_employees(self):
		manifest = self.get_manifest()
		techs = [v for k, v in (manifest.techs or {}).items() if k != "inactive_tech"]
		if len(techs) >= 2:
			return techs[0], techs[1]
		emps = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name", limit=2)
		self.assertGreaterEqual(len(emps), 2, "Golden DB must provide at least 2 active Employees")
		return emps[0], emps[1]

	# ------------------------------------------------------------------
	# Pure interval helper tests
	# ------------------------------------------------------------------

	def test_subtract_intervals_handles_unsorted_overlapping_and_bounds(self):
		base_s = datetime(2026, 11, 2, 8, 0)
		base_e = datetime(2026, 11, 2, 17, 0)

		remaining = subtract_intervals(
			base_s,
			base_e,
			[
				(datetime(2026, 11, 2, 12, 0), datetime(2026, 11, 2, 13, 0)),
				(datetime(2026, 11, 2, 9, 30), datetime(2026, 11, 2, 10, 30)),
				(datetime(2026, 11, 2, 10, 0), datetime(2026, 11, 2, 11, 0)),
				(datetime(2026, 11, 2, 7, 0), datetime(2026, 11, 2, 8, 30)),
				(datetime(2026, 11, 2, 18, 0), datetime(2026, 11, 2, 19, 0)),
			],
		)
		self.assertEqual(
			remaining,
			[
				(datetime(2026, 11, 2, 8, 30), datetime(2026, 11, 2, 9, 30)),
				(datetime(2026, 11, 2, 11, 0), datetime(2026, 11, 2, 12, 0)),
				(datetime(2026, 11, 2, 13, 0), datetime(2026, 11, 2, 17, 0)),
			],
		)

		# Full coverage leaves empty list
		self.assertEqual(
			subtract_intervals(base_s, base_e, [(datetime(2026, 11, 2, 7, 0), datetime(2026, 11, 2, 18, 0))]),
			[],
		)

	def test_same_day_lunch_deduction_and_overtime_preservation(self):
		cfg = self._default_shop_cfg()

		# Normal same-day spanning lunch: 10:00 -> 14:00 (4h raw, 3.5h net)
		res = calculate_net_working_session(
			datetime(2026, 11, 2, 10, 0),
			datetime(2026, 11, 2, 14, 0),
			settings=cfg,
		)
		self.assertAlmostEqual(res["raw_hours"], 4.0, places=3)
		self.assertAlmostEqual(res["net_hours"], 3.5, places=3)
		self.assertAlmostEqual(res["deducted_overlap_hours"], 0.0, places=3)
		self.assertFalse(res["spans_overnight"])
		self.assertFalse(res["needs_review"])
		self.assertEqual(res["flag_reason"], "")

		# Early start (06:30) and past closing (18:30) preserved on same-day, plus Oversized flag
		overtime = calculate_net_working_session(
			datetime(2026, 11, 2, 6, 30),
			datetime(2026, 11, 2, 18, 30),
			settings=cfg,
			max_single_session_hours=8.5,
		)
		self.assertAlmostEqual(overtime["raw_hours"], 12.0, places=3)
		self.assertAlmostEqual(overtime["net_hours"], 11.5, places=3)
		self.assertFalse(overtime["spans_overnight"])
		self.assertTrue(overtime["needs_review"])
		self.assertEqual(overtime["flag_reason"], "Oversized Session")

		# Weekend / holiday overtime on same-day is preserved
		weekend = calculate_net_working_session(
			datetime(2026, 11, 8, 9, 0),
			datetime(2026, 11, 8, 11, 30),
			settings=cfg,
			is_holiday_fn=lambda d: True,
		)
		self.assertAlmostEqual(weekend["raw_hours"], 2.5, places=3)
		self.assertAlmostEqual(weekend["net_hours"], 2.5, places=3)
		self.assertFalse(weekend["spans_overnight"])
		self.assertFalse(weekend["needs_review"])

	def test_multiday_clipping_holiday_exclusion_and_oversized_flag(self):
		cfg = self._default_shop_cfg()

		# Overnight: Day 1 16:00 -> Day 2 10:00 => Day 1 (16-17 = 1h) + Day 2 (8-10 = 2h) = 3h net
		overnight = calculate_net_working_session(
			datetime(2026, 11, 2, 16, 0),
			datetime(2026, 11, 3, 10, 0),
			settings=cfg,
			is_holiday_fn=lambda d: False,
			max_single_session_hours=8.5,
		)
		self.assertAlmostEqual(overnight["raw_hours"], 18.0, places=3)
		self.assertAlmostEqual(overnight["net_hours"], 3.0, places=3)
		self.assertTrue(overnight["spans_overnight"])
		self.assertTrue(overnight["needs_review"])
		self.assertEqual(overnight["flag_reason"], "Overnight / Multi-Day Session")

		# Multi-day with Day 2 holiday + exceeding max_single_session_hours
		# Day 1 13:00 -> 17:00 = 4.0h; Day 2 holiday = 0h; Day 3 08:00 -> 14:30 minus lunch (0.5h) = 6.0h => 10.0h
		holiday_day = date(2026, 11, 3)
		multiday_oversized = calculate_net_working_session(
			datetime(2026, 11, 2, 13, 0),
			datetime(2026, 11, 4, 14, 30),
			settings=cfg,
			is_holiday_fn=lambda d: d == holiday_day,
			max_single_session_hours=8.5,
		)
		self.assertAlmostEqual(multiday_oversized["net_hours"], 10.0, places=3)
		self.assertTrue(multiday_oversized["spans_overnight"])
		self.assertTrue(multiday_oversized["needs_review"])
		self.assertIn("Overnight / Multi-Day Session", multiday_oversized["flag_reason"])
		self.assertIn("Oversized Session", multiday_oversized["flag_reason"])

	# ------------------------------------------------------------------
	# Integration tests (real MariaDB via ISMTestCase)
	# ------------------------------------------------------------------

	def test_multi_session_accumulation_and_no_double_counted_service_duration(self):
		ro = self._create_approved_ro()

		# Session 1: 09:00 -> 10:30 (1.5h net), temporary park
		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 9, 0)):
			ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(len(ro.time_logs), 1)
		self.assertEqual(ro.time_logs[0].session_status, SESSION_OPEN)
		self.assertEqual(ro.time_logs[0].actor_user, frappe.session.user)
		self.assertEqual(flt(ro.time_logs[0].time_in_hours), 0.0)

		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 10, 30)):
			ro.park_vehicle(temporary=True)
		ro.reload()
		self.assertEqual(ro.vehicle_location, "In Customer Lot")
		self.assertEqual(ro.time_logs[0].session_status, SESSION_CLOSED)
		self.assertAlmostEqual(flt(ro.time_logs[0].raw_hours), 1.5, places=3)
		self.assertAlmostEqual(flt(ro.time_logs[0].time_in_hours), 1.5, places=3)
		self.assertAlmostEqual(flt(ro.total_bay_hours), 1.5, places=3)

		# Session 2: 11:30 -> 13:30 (2.0h raw, crosses 12:00..12:30 lunch -> 1.5h net)
		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 11, 30)):
			ro.pick_up_vehicle()

		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 13, 0)):
			ro.toggle_service_completion(ro.services[0].name, 1)
		ro.reload()
		self.assertEqual(ro.services[0].is_completed, 1)
		self.assertIsNotNone(ro.services[0].completed_at)
		self.assertEqual(ro.services[0].completed_by, frappe.session.user)
		# toggle_service_completion must NOT write double-counted completed_at - picked_up_at
		self.assertEqual(flt(ro.services[0].actual_duration_hours), 0.0)

		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 13, 30)):
			ro.park_vehicle()
		ro.reload()
		self.assertEqual(ro.status, "Work Completed")
		self.assertEqual(len(ro.time_logs), 2)
		self.assertAlmostEqual(flt(ro.time_logs[1].raw_hours), 2.0, places=3)
		self.assertAlmostEqual(flt(ro.time_logs[1].time_in_hours), 1.5, places=3)
		self.assertAlmostEqual(flt(ro.total_bay_hours), 3.0, places=3)

	def test_duplicate_open_session_guard_and_technician_handoff(self):
		emp1, emp2 = self._get_two_employees()
		ro = self._create_approved_ro(
			services=[
				{
					"service_title": "Brake Fluid Flush",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"assigned_technician": emp1,
				}
			]
		)

		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 9, 0)):
			ro.pick_up_vehicle()
		# Repeated pickup by same actor/tech must NOT append duplicate Open row
		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 9, 15)):
			ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(len(ro.time_logs), 1)
		self.assertEqual(ro.time_logs[0].session_status, SESSION_OPEN)
		self.assertEqual(ro.time_logs[0].technician, emp1)

		# Handoff to emp2: closes emp1's open row and opens emp2's row
		with (
			patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 10, 0)),
			patch("ism.ism.doctype.repair_order.fulfillment.get_employee_for_user", return_value=emp2),
		):
			ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(len(ro.time_logs), 2)
		self.assertEqual(ro.time_logs[0].session_status, SESSION_CLOSED)
		self.assertAlmostEqual(flt(ro.time_logs[0].time_in_hours), 1.0, places=3)
		self.assertEqual(ro.time_logs[1].session_status, SESSION_OPEN)
		self.assertEqual(ro.time_logs[1].technician, emp2)

	def test_cross_ro_technician_overlap_subtraction(self):
		emp1, _emp2 = self._get_two_employees()
		ro1 = self._create_approved_ro(
			services=[
				{
					"service_title": "Upper Control Arm",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 150.0,
					"assigned_technician": emp1,
				}
			]
		)
		ro2 = self._create_approved_ro(
			services=[
				{
					"service_title": "Cabin Air Filter",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"assigned_technician": emp1,
				}
			]
		)

		with patch("ism.ism.doctype.repair_order.fulfillment.get_employee_for_user", return_value=emp1):
			# 09:00 Tech picks up RO-1 and leaves it open
			with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 9, 0)):
				ro1.pick_up_vehicle()

			# 10:00 Tech picks up RO-2 and parks RO-2 at 11:00 (1.0h)
			with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 10, 0)):
				ro2.pick_up_vehicle()
			with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 11, 0)):
				ro2.park_vehicle(temporary=True)

			# 12:00 Tech parks RO-1 (3.0h raw minus 1.0h overlap on RO-2 => 2.0h net)
			with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 12, 0)):
				ro1.park_vehicle(temporary=True)

		ro1.reload()
		ro2.reload()
		self.assertAlmostEqual(flt(ro2.time_logs[0].time_in_hours), 1.0, places=3)
		self.assertAlmostEqual(flt(ro2.time_logs[0].deducted_overlap_hours), 0.0, places=3)

		self.assertAlmostEqual(flt(ro1.time_logs[0].raw_hours), 3.0, places=3)
		self.assertAlmostEqual(flt(ro1.time_logs[0].deducted_overlap_hours), 1.0, places=3)
		self.assertAlmostEqual(flt(ro1.time_logs[0].time_in_hours), 2.0, places=3)
		self.assertAlmostEqual(flt(ro1.total_bay_hours), 2.0, places=3)

	def test_admin_unlinked_pickup_and_single_assigned_tech_fallback(self):
		emp1, emp2 = self._get_two_employees()

		# Case 1: Unlinked admin, no assigned tech -> technician is None, actor_user recorded
		ro_unassigned = self._create_approved_ro()
		with patch("ism.ism.doctype.repair_order.fulfillment.get_employee_for_user", return_value=None):
			ro_unassigned.pick_up_vehicle()
		ro_unassigned.reload()
		self.assertIsNone(ro_unassigned.time_logs[0].technician)
		self.assertEqual(ro_unassigned.time_logs[0].actor_user, frappe.session.user)

		# Case 2: Unlinked admin, single assigned tech on RO -> falls back to emp1
		ro_single = self._create_approved_ro(
			services=[
				{
					"service_title": "Tire Rotation",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 150.0,
					"assigned_technician": emp1,
				}
			]
		)
		with patch("ism.ism.doctype.repair_order.fulfillment.get_employee_for_user", return_value=None):
			ro_single.pick_up_vehicle()
		ro_single.reload()
		self.assertEqual(ro_single.time_logs[0].technician, emp1)
		self.assertEqual(ro_single.time_logs[0].actor_user, frappe.session.user)

		# Case 3: Unlinked admin, two different assigned techs -> ambiguous, technician stays None
		ro_multi = self._create_approved_ro(
			services=[
				{
					"service_title": "Service A",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 150.0,
					"assigned_technician": emp1,
				},
				{
					"service_title": "Service B",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 150.0,
					"assigned_technician": emp2,
				},
			]
		)
		with patch("ism.ism.doctype.repair_order.fulfillment.get_employee_for_user", return_value=None):
			ro_multi.pick_up_vehicle()
		ro_multi.reload()
		self.assertIsNone(ro_multi.time_logs[0].technician)
		self.assertEqual(ro_multi.time_logs[0].actor_user, frappe.session.user)

	def test_legacy_ro_park_synthesizes_time_log_and_validate_recalculates_edits(self):
		ro = self._create_approved_ro()
		# Simulate legacy in-flight RO with picked_up_at set directly and no time_logs
		ro.db_set("picked_up_at", datetime(2026, 11, 2, 9, 0))
		ro.db_set("picked_up_by", frappe.session.user)
		ro.reload()

		with patch("ism.ism.doctype.repair_order.fulfillment.now_datetime", return_value=datetime(2026, 11, 2, 11, 0)):
			ro.park_vehicle(temporary=True)
		ro.reload()
		self.assertEqual(len(ro.time_logs), 1)
		self.assertAlmostEqual(flt(ro.time_logs[0].time_in_hours), 2.0, places=3)
		self.assertAlmostEqual(flt(ro.total_bay_hours), 2.0, places=3)

		# Manager edits to_time to 13:30 (crosses 30m lunch -> 4.0h net, 4.5h raw)
		ro.time_logs[0].to_time = datetime(2026, 11, 2, 13, 30)
		ro.save()
		ro.reload()
		self.assertAlmostEqual(flt(ro.time_logs[0].raw_hours), 4.5, places=3)
		self.assertAlmostEqual(flt(ro.time_logs[0].time_in_hours), 4.0, places=3)
		self.assertAlmostEqual(flt(ro.total_bay_hours), 4.0, places=3)

		# Manually seeded row with only time_in_hours (no from_time/to_time) is preserved
		ro.append("time_logs", {"session_status": SESSION_CLOSED, "time_in_hours": 1.25})
		ro.save()
		ro.reload()
		self.assertAlmostEqual(flt(ro.time_logs[1].time_in_hours), 1.25, places=3)
		self.assertAlmostEqual(flt(ro.total_bay_hours), 5.25, places=3)

	def test_authorized_at_stamped_on_initial_supplement_and_manual_approval(self):
		ro = frappe.get_doc(
			{
				"doctype": "Repair Order",
				"customer": self.customer.name,
				"vehicle": self.vehicle.name,
				"odometer_in": 42000,
				"approval_status": "Pending Approval",
				"services": [
					{
						"service_title": "Diagnostic Check",
						"status": "Pending Approval",
						"labor_hours": 1.0,
						"labor_rate": 150.0,
					},
					{
						"service_title": "Wiper Blades",
						"status": "Pending Approval",
						"labor_hours": 0.5,
						"labor_rate": 150.0,
					},
				],
			}
		)
		ro.insert()
		self.track(ro)

		t1 = datetime(2026, 11, 2, 9, 15, 0)
		with patch("ism.ism.doctype.repair_order.authorization.now_datetime", return_value=t1):
			ro.authorize_jobs(
				approvals={
					ro.services[0].name: "Approved",
					ro.services[1].name: "Declined",
				},
				channel="In-Person Signature",
				signer_name="Jane Owner",
			)
		ro.reload()
		self.assertEqual(get_datetime(ro.services[0].authorized_at), t1)
		self.assertIsNone(ro.services[1].authorized_at)

		# Add supplement and approve at t2 -> supplement gets t2, initial row retains t1
		ro.add_supplement(service_title="Front Brake Pads", labor_hours=1.5, labor_rate=150.0)
		ro.reload()
		supp_row = ro.services[2]
		self.assertIsNone(supp_row.authorized_at)

		t2 = datetime(2026, 11, 2, 10, 45, 0)
		with patch("ism.ism.doctype.repair_order.authorization.now_datetime", return_value=t2):
			ro.authorize_jobs(
				approvals={supp_row.name: "Approved"},
				channel="Phone Verbal",
				signer_name="Jane Owner",
			)
		ro.reload()
		self.assertEqual(get_datetime(ro.services[0].authorized_at), t1)
		self.assertEqual(get_datetime(ro.services[2].authorized_at), t2)

		# Manual advisor approval via sync_approval_status_to_services stamps authorized_at when empty
		ro_manual = frappe.get_doc(
			{
				"doctype": "Repair Order",
				"customer": self.customer.name,
				"vehicle": self.vehicle.name,
				"odometer_in": 43000,
				"approval_status": "Estimating",
				"services": [
					{
						"service_title": "Wheel Alignment",
						"status": "Estimating",
						"labor_hours": 1.0,
						"labor_rate": 150.0,
					}
				],
			}
		)
		ro_manual.insert()
		self.track(ro_manual)
		self.assertIsNone(ro_manual.services[0].authorized_at)

		t3 = datetime(2026, 11, 2, 14, 0, 0)
		ro_manual.approval_status = "Approved"
		with patch("ism.ism.doctype.repair_order.authorization.now_datetime", return_value=t3):
			ro_manual.save()
		ro_manual.reload()
		self.assertEqual(ro_manual.services[0].status, "Approved")
		self.assertEqual(get_datetime(ro_manual.services[0].authorized_at), t3)

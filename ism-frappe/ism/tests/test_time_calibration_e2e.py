# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""End-to-end MariaDB integration tests for the ISM Time Calibration Engine (zero DB mocks)."""

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import cint, flt

from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.time_calibration.allocator import recompute_observations_for_ro
from ism.time_calibration.bayesian import recalibrate_overlap_group, recalibrate_service_package
from ism.time_calibration.contracts import (
	ALLOC_RO_WIDE,
	ALLOC_SINGLE_JOB,
	ALLOC_SUPPLEMENT_POST_APPROVAL,
	ALLOC_TECH_SCOPED,
	MODE_DISABLED,
	MODE_SHADOW,
	REVIEW_CLEAN,
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_NEEDS_REVIEW,
	REVIEW_VOIDED,
	SESSION_CLOSED,
	recompute_ro_observations,
)
from ism.time_calibration.deconvolution import run_nightly_calibration_job
from ism.time_calibration.overlap import (
	ensure_overlap_group,
	get_effective_overlap_factors,
	resolve_service_overlap_bucket,
)
from ism.time_calibration.service import (
	on_observation_update,
	on_repair_order_update,
	recalibrate_all,
)


class TestTimeCalibrationE2E(ISMTestCase):
	"""Real database integration tests covering overlap, allocation, Bayesian calibration, and deconvolution."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Ensure ISM Settings defaults to Shadow mode for observation & calibration tests
		if frappe.db.exists("DocType", "ISM Settings"):
			frappe.db.set_single_value("ISM Settings", "time_calibration_mode", MODE_SHADOW)
			frappe.clear_cache(doctype="ISM Settings")
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(
			customer=self.customer.name,
			model="Model 3",
			model_year=2023,
		)

	def _create_package(
		self,
		name_suffix: str,
		*,
		correction_code: str = "31011002",
		standard_labor_hours: float = 1.0,
		overlap_group: str | None = None,
		exclude_from_time_calibration: int = 0,
		is_symmetrical: int = 0,
	):
		pkg = frappe.get_doc(
			{
				"doctype": "Service Package",
				"package_name": f"{TEST_PREFIX}Pkg_{name_suffix}_{frappe.generate_hash(length=6)}",
				"applicable_model": "Model 3",
				"correction_code": correction_code,
				"standard_labor_hours": standard_labor_hours,
				"overlap_group": overlap_group,
				"exclude_from_time_calibration": exclude_from_time_calibration,
				"is_symmetrical": is_symmetrical,
			}
		).insert(ignore_permissions=True)
		return self.track(pkg)

	def _create_employee(self, tag: str):
		company = (
			frappe.defaults.get_user_default("Company")
			or frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {}, "name")
		)
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"{TEST_PREFIX}Tech_{tag}_{frappe.generate_hash(length=5)}",
				"gender": "Prefer not to say",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2024-01-01",
				"status": "Active",
				"company": company,
			}
		).insert(ignore_permissions=True)
		return self.track(emp)

	def _create_completed_ro(
		self,
		services: list[dict],
		time_logs: list[dict],
		*,
		status: str = "Work Completed",
		approval_logs: list[dict] | None = None,
		manual_parts_hold: int = 0,
	):
		ro = self.create_test_repair_order(
			customer=self.customer.name,
			vehicle=self.vehicle.name,
			status=status,
			approval_status="Approved",
			manual_parts_hold=manual_parts_hold,
			services=services,
			time_logs=time_logs,
			approval_logs=approval_logs or [],
		)
		return ro

	def _get_observations(self, ro_name: str) -> list[dict]:
		return frappe.get_all(
			"Service Time Observation",
			filters={"repair_order": ro_name},
			fields=["*"],
			order_by="creation asc",
		)

	# -----------------------------------------------------------------------
	# 1. Overlap Helpers & Solo RO Lifecycle
	# -----------------------------------------------------------------------

	def test_overlap_helpers_idempotent_and_same_corner_partition(self):
		"""ensure_overlap_group is idempotent and resolve_service_overlap_bucket partitions by corner."""
		code1 = ensure_overlap_group("3101", "Front Suspension", is_auto_derived=True)
		code2 = ensure_overlap_group(" 3101 ", "Front Suspension", is_auto_derived=True)
		self.assertEqual(code1, "3101")
		self.assertEqual(code2, "3101")

		grp_lh, bkt_lh = resolve_service_overlap_bucket(
			{"correction_code": "31011002", "service_position": "LH"}
		)
		grp_rh, bkt_rh = resolve_service_overlap_bucket(
			{"correction_code": "31011002", "service_position": "RH"}
		)
		grp_none, bkt_none = resolve_service_overlap_bucket(
			{"correction_code": "31011002", "service_position": None}
		)
		self.assertEqual((grp_lh, bkt_lh), ("3101", "3101:LH"))
		self.assertEqual((grp_rh, bkt_rh), ("3101", "3101:RH"))
		self.assertEqual((grp_none, bkt_none), ("3101", "3101:None"))
		self.assertNotEqual(bkt_lh, bkt_rh)

		# Explicit package override takes precedence over correction_code
		ensure_overlap_group("CUSTOM_SUSP", "Custom Suspension", is_auto_derived=False)
		grp_ov, bkt_ov = resolve_service_overlap_bucket(
			{"correction_code": "31011002", "service_position": "LH"},
			pkg_meta={"overlap_group": "CUSTOM_SUSP"},
		)
		self.assertEqual((grp_ov, bkt_ov), ("CUSTOM_SUSP", "CUSTOM_SUSP:LH"))

		factors = get_effective_overlap_factors(["3101", "CUSTOM_SUSP", "NON_EXISTENT"])
		self.assertEqual(factors["3101"], 1.0)
		self.assertEqual(factors["CUSTOM_SUSP"], 1.0)
		self.assertEqual(factors["NON_EXISTENT"], 1.0)

	def test_solo_ro_updates_package_calibration_without_touching_frt(self):
		"""Solo completed RO allocates 1.0 confidence, syncs RO Service, and calibrates package without altering FRT."""
		pkg = self._create_package("SoloUCA", correction_code="31011002", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 20, 9, 0, 0)
		t1 = datetime(2026, 11, 20, 10, 30, 0)

		ro = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "31011002",
					"service_title": "Front Upper Control Arm LH",
					"service_position": "LH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t1,
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t1,
					"time_in_hours": 1.5,
					"session_status": SESSION_CLOSED,
				}
			],
		)

		res = on_repair_order_update(ro)
		self.assertFalse(res["skipped"])
		self.assertEqual(len(res["observations"]), 1)

		obs_list = self._get_observations(ro.name)
		self.assertEqual(len(obs_list), 1)
		obs = obs_list[0]
		self.assertEqual(obs["allocation_method"], ALLOC_SINGLE_JOB)
		self.assertEqual(flt(obs["confidence_score"], 3), 1.0)
		self.assertAlmostEqual(flt(obs["allocated_hours"]), 1.5, places=3)
		self.assertAlmostEqual(flt(obs["allocated_minutes"]), 90.0, places=1)
		self.assertEqual(obs["review_status"], REVIEW_CLEAN)
		self.assertEqual(cint(obs["exclude_from_calibration"]), 0)
		self.assertEqual(obs["overlap_group"], "3101")
		self.assertEqual(obs["overlap_bucket_key"], "3101:LH")
		self.assertEqual(cint(obs["same_group_job_count"]), 1)

		# Repair Order Service actual_duration_hours synced via db.set_value
		svc_actual = flt(frappe.db.get_value("Repair Order Service", ro.services[0].name, "actual_duration_hours"))
		self.assertAlmostEqual(svc_actual, 1.5, places=3)

		# Service Package calibrated stats updated; factory FRT untouched
		pkg.reload()
		self.assertEqual(flt(pkg.standard_labor_hours), 1.0)
		self.assertGreater(flt(pkg.calibrated_median_minutes), 60.0)
		self.assertGreater(flt(pkg.calibrated_sigma), 0.0)
		self.assertGreater(flt(pkg.frt_realization_ratio), 1.0)
		self.assertGreater(flt(pkg.effective_sample_count), 0.0)

		# Idempotent re-run via contracts late-binding produces no duplicates
		obs_names_again = recompute_ro_observations(ro.name)
		self.assertEqual(len(obs_names_again), 1)
		self.assertEqual(len(self._get_observations(ro.name)), 1)

	# -----------------------------------------------------------------------
	# 2. Same-Corner vs Opposite-Side vs Unrelated Bundling
	# -----------------------------------------------------------------------

	def test_same_corner_bundle_protects_solo_baseline_and_learns_alpha_while_opposite_sides_stay_independent(self):
		"""Same-corner LH+LH learns alpha_g < 1.0 without dragging solo median; LH+RH stay independent."""
		group_code = f"T{frappe.generate_hash(length=5).upper()}"
		ensure_overlap_group(group_code, "Test Front Suspension", is_auto_derived=False)

		pkg_primary = self._create_package(
			"FrontUCA",
			correction_code="31011002",
			standard_labor_hours=1.5,
			overlap_group=group_code,
			is_symmetrical=1,
		)
		pkg_secondary = self._create_package(
			"FrontKnuckle",
			correction_code="31012004",
			standard_labor_hours=1.0,
			overlap_group=group_code,
		)
		pkg_unrelated = self._create_package(
			"HVACFilter",
			correction_code="18101001",
			standard_labor_hours=0.5,
		)

		base_dt = datetime(2026, 11, 18, 9, 0, 0)

		# Seed 2 clean solo ROs for pkg_primary (1.5h each) so solo baseline = 90.0 min
		for i in range(2):
			ro_solo = self._create_completed_ro(
				services=[
					{
						"service_package": pkg_primary.name,
						"correction_code": "31011002",
						"service_title": "Front UCA LH",
						"service_position": "LH",
						"status": "Approved",
						"is_completed": 1,
						"labor_hours": 1.5,
						"completed_at": base_dt + timedelta(days=i, hours=1, minutes=30),
					}
				],
				time_logs=[
					{
						"from_time": base_dt + timedelta(days=i),
						"to_time": base_dt + timedelta(days=i, hours=1, minutes=30),
						"time_in_hours": 1.5,
						"session_status": SESSION_CLOSED,
					}
				],
			)
			on_repair_order_update(ro_solo)

		pkg_primary.reload()
		solo_median_before = flt(pkg_primary.calibrated_median_minutes)
		self.assertAlmostEqual(solo_median_before, 90.0, delta=0.5)

		# Now run 3 same-corner LH+LH bundles taking 2.0h total (vs 2.5h sum -> alpha_hat = 0.5)
		for i in range(3):
			day = base_dt + timedelta(days=2 + i)
			ro_same = self._create_completed_ro(
				services=[
					{
						"service_package": pkg_primary.name,
						"correction_code": "31011002",
						"service_title": "Front UCA LH",
						"service_position": "LH",
						"status": "Approved",
						"is_completed": 1,
						"labor_hours": 1.5,
						"completed_at": day + timedelta(hours=2),
					},
					{
						"service_package": pkg_secondary.name,
						"correction_code": "31012004",
						"service_title": "Front Knuckle LH",
						"service_position": "LH",
						"status": "Approved",
						"is_completed": 1,
						"labor_hours": 1.0,
						"completed_at": day + timedelta(hours=2),
					},
				],
				time_logs=[
					{
						"from_time": day,
						"to_time": day + timedelta(hours=2),
						"time_in_hours": 2.0,
						"session_status": SESSION_CLOSED,
					}
				],
			)
			on_repair_order_update(ro_same)

			obs_same = self._get_observations(ro_same.name)
			self.assertEqual(len(obs_same), 2)
			self.assertTrue(all(o["overlap_bucket_key"] == f"{group_code}:LH" for o in obs_same))
			self.assertTrue(all(cint(o["same_group_job_count"]) == 2 for o in obs_same))
			primaries = [o for o in obs_same if cint(o["is_overlap_primary"]) == 1]
			secondaries = [o for o in obs_same if cint(o["is_overlap_primary"]) == 0]
			self.assertEqual(len(primaries), 1)
			self.assertEqual(len(secondaries), 1)
			self.assertEqual(primaries[0]["service_package"], pkg_primary.name)

		# Solo baseline of pkg_primary MUST remain untouched by same-corner bundle drag!
		pkg_primary.reload()
		self.assertAlmostEqual(flt(pkg_primary.calibrated_median_minutes), solo_median_before, delta=0.5)

		# Overlap group MUST have learned calibrated_overlap_factor < 1.0
		grp_doc = frappe.get_doc("Service Overlap Group", group_code)
		self.assertEqual(cint(grp_doc.bundle_sample_count), 3)
		self.assertLess(flt(grp_doc.calibrated_overlap_factor), 1.0)
		self.assertGreaterEqual(flt(grp_doc.calibrated_overlap_factor), 0.35)
		self.assertAlmostEqual(flt(grp_doc.effective_overlap_factor), flt(grp_doc.calibrated_overlap_factor), places=3)

		# Opposite-side LH + RH stay in separate buckets (same_group_job_count=1, is_paired_symmetrical=1)
		day_opp = base_dt + timedelta(days=6)
		ro_opp = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_primary.name,
					"correction_code": "31011002",
					"service_title": "Front UCA LH",
					"service_position": "LH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.5,
					"completed_at": day_opp + timedelta(hours=3),
				},
				{
					"service_package": pkg_primary.name,
					"correction_code": "31011002",
					"service_title": "Front UCA RH",
					"service_position": "RH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.5,
					"completed_at": day_opp + timedelta(hours=3),
				},
			],
			time_logs=[
				{
					"from_time": day_opp,
					"to_time": day_opp + timedelta(hours=3),
					"time_in_hours": 3.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_opp)
		obs_opp = self._get_observations(ro_opp.name)
		self.assertEqual(len(obs_opp), 2)
		self.assertEqual({o["overlap_bucket_key"] for o in obs_opp}, {f"{group_code}:LH", f"{group_code}:RH"})
		self.assertTrue(all(cint(o["same_group_job_count"]) == 1 for o in obs_opp))
		self.assertTrue(all(cint(o["is_paired_symmetrical"]) == 1 for o in obs_opp))
		self.assertTrue(all(flt(o["applied_overlap_factor"]) == 1.0 for o in obs_opp))

		# Unrelated groups (group_code:LH + 1810:None) stay independent
		ro_unrel = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_primary.name,
					"correction_code": "31011002",
					"service_title": "Front UCA LH",
					"service_position": "LH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.5,
					"completed_at": day_opp + timedelta(hours=2),
				},
				{
					"service_package": pkg_unrelated.name,
					"correction_code": "18101001",
					"service_title": "Cabin Filter",
					"service_position": "None",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 0.5,
					"completed_at": day_opp + timedelta(hours=2),
				},
			],
			time_logs=[
				{
					"from_time": day_opp,
					"to_time": day_opp + timedelta(hours=2),
					"time_in_hours": 2.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_unrel)
		obs_unrel = self._get_observations(ro_unrel.name)
		self.assertEqual(len(obs_unrel), 2)
		self.assertEqual({o["overlap_bucket_key"] for o in obs_unrel}, {f"{group_code}:LH", "1810:None"})
		self.assertTrue(all(cint(o["same_group_job_count"]) == 1 for o in obs_unrel))

	# -----------------------------------------------------------------------
	# 3. Diagnostic -> Supplement Post-Approval Slicing
	# -----------------------------------------------------------------------

	def test_diagnostic_to_supplement_clips_to_post_approval_window(self):
		"""Diag (09:00-10:30) excluded; concrete supplement approved at 10:30, parked 11:30 -> gets 1.0h."""
		diag_pkg = self._create_package(
			"Diag",
			correction_code="00010000",
			standard_labor_hours=1.0,
			exclude_from_time_calibration=1,
		)
		sup_pkg = self._create_package("BrakeCaliper", correction_code="33011001", standard_labor_hours=1.0)

		pickup = datetime(2026, 11, 20, 9, 0, 0)
		sup_approved = datetime(2026, 11, 20, 10, 30, 0)
		park = datetime(2026, 11, 20, 11, 30, 0)

		ro = self._create_completed_ro(
			services=[
				{
					"service_package": diag_pkg.name,
					"correction_code": "00010000",
					"service_title": "Brake Noise Diagnostic",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 0,
					"labor_hours": 1.0,
					"completed_at": sup_approved,
				},
				{
					"service_package": sup_pkg.name,
					"correction_code": "33011001",
					"service_title": "Replace Brake Caliper",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 1,
					"authorized_at": sup_approved,
					"labor_hours": 1.0,
					"completed_at": park,
				},
			],
			time_logs=[
				{
					"from_time": pickup,
					"to_time": park,
					"time_in_hours": 2.5,
					"session_status": SESSION_CLOSED,
				}
			],
		)

		recompute_observations_for_ro(ro)
		obs_list = self._get_observations(ro.name)
		# Diagnostic MUST NOT produce an observation; only concrete supplement does
		self.assertEqual(len(obs_list), 1)
		obs = obs_list[0]
		self.assertEqual(obs["service_package"], sup_pkg.name)
		self.assertEqual(obs["allocation_method"], ALLOC_SUPPLEMENT_POST_APPROVAL)
		self.assertAlmostEqual(flt(obs["allocated_hours"]), 1.0, places=3)
		self.assertAlmostEqual(flt(obs["ro_net_active_hours"]), 1.0, places=3)
		self.assertEqual(obs["review_status"], REVIEW_CLEAN)
		self.assertEqual(cint(obs["exclude_from_calibration"]), 0)

	# -----------------------------------------------------------------------
	# 4. Concrete Repair + Mid-Job Supplement & Mid-Bay Hold / Outlier / Quality
	# -----------------------------------------------------------------------

	def test_concrete_plus_mid_job_supplement_flagged_unless_temporary_park_separates(self):
		"""Bridged concrete+supplement flags Mid-Job Supplement Mixed; separated sessions stay Clean."""
		pkg_a = self._create_package("ConcreteA", correction_code="33011001", standard_labor_hours=1.0)
		pkg_b = self._create_package("SupplementB", correction_code="33021001", standard_labor_hours=1.0)

		t0 = datetime(2026, 11, 20, 9, 0, 0)
		sup_auth = datetime(2026, 11, 20, 10, 0, 0)
		t1 = datetime(2026, 11, 20, 11, 30, 0)

		# Case 1: One continuous open bay session bridging supplement approval -> flagged
		ro_mixed = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_a.name,
					"correction_code": "33011001",
					"service_title": "Brake Pads",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 0,
					"labor_hours": 1.0,
					"completed_at": t1,
				},
				{
					"service_package": pkg_b.name,
					"correction_code": "33021001",
					"service_title": "Brake Rotors",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 1,
					"authorized_at": sup_auth,
					"labor_hours": 1.0,
					"completed_at": t1,
				},
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t1,
					"time_in_hours": 2.5,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_mixed)
		obs_mixed = self._get_observations(ro_mixed.name)
		self.assertEqual(len(obs_mixed), 2)
		for o in obs_mixed:
			self.assertEqual(o["review_status"], REVIEW_NEEDS_REVIEW)
			self.assertEqual(o["flag_reason"], "Mid-Job Supplement Mixed")
			self.assertEqual(cint(o["exclude_from_calibration"]), 1)

		# Case 2: Temporary Park separates phases (09:00-10:00, sup approved at 10:05, resumed 10:15-11:30)
		ro_clean = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_a.name,
					"correction_code": "33011001",
					"service_title": "Brake Pads",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 0,
					"labor_hours": 1.0,
					"completed_at": sup_auth,
				},
				{
					"service_package": pkg_b.name,
					"correction_code": "33021001",
					"service_title": "Brake Rotors",
					"status": "Approved",
					"is_completed": 1,
					"is_supplement": 1,
					"authorized_at": datetime(2026, 11, 20, 10, 5, 0),
					"labor_hours": 1.0,
					"completed_at": t1,
				},
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": sup_auth,
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				},
				{
					"from_time": datetime(2026, 11, 20, 10, 15, 0),
					"to_time": t1,
					"time_in_hours": 1.25,
					"session_status": SESSION_CLOSED,
				},
			],
		)
		recompute_observations_for_ro(ro_clean)
		obs_clean = self._get_observations(ro_clean.name)
		self.assertEqual(len(obs_clean), 2)
		self.assertTrue(all(o["review_status"] == REVIEW_CLEAN for o in obs_clean))
		self.assertTrue(all(cint(o["exclude_from_calibration"]) == 0 for o in obs_clean))

	def test_mid_bay_hold_session_quality_and_outlier_flagging(self):
		"""Verify Mid-Bay Hold, time_log needs_review propagation, and Outlier Ratio bounds."""
		pkg = self._create_package("FlaggedJob", correction_code="33011001", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 20, 9, 0, 0)

		# 1. manual_parts_hold = 1 -> Mid-Bay Hold Detected
		ro_hold = self._create_completed_ro(
			manual_parts_hold=1,
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Service",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1),
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_hold)
		obs_hold = self._get_observations(ro_hold.name)[0]
		self.assertEqual(obs_hold["review_status"], REVIEW_NEEDS_REVIEW)
		self.assertEqual(obs_hold["flag_reason"], "Mid-Bay Hold Detected")
		self.assertEqual(cint(obs_hold["exclude_from_calibration"]), 1)

		# 2. time_log.needs_review = 1 -> flagged Needs Review + excluded
		ro_bad_sess = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Service",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(days=1, hours=1),
					"time_in_hours": 8.0,
					"spans_overnight": 1,
					"needs_review": 1,
					"flag_reason": "Overnight Session",
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_bad_sess)
		obs_sess = self._get_observations(ro_bad_sess.name)[0]
		self.assertEqual(obs_sess["review_status"], REVIEW_NEEDS_REVIEW)
		self.assertEqual(obs_sess["flag_reason"], "Overnight Session")
		self.assertEqual(cint(obs_sess["exclude_from_calibration"]), 1)

		# 3. Outlier Ratio (5.0h / 1.0h FRT = 5.0 > 4.0 max_ratio)
		ro_outlier = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Service",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=5),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=5),
					"time_in_hours": 5.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_outlier)
		obs_out = self._get_observations(ro_outlier.name)[0]
		self.assertEqual(cint(obs_out["is_outlier"]), 1)
		self.assertEqual(obs_out["review_status"], REVIEW_NEEDS_REVIEW)
		self.assertEqual(obs_out["flag_reason"], "Outlier Ratio")
		self.assertEqual(cint(obs_out["exclude_from_calibration"]), 1)

	# -----------------------------------------------------------------------
	# 5. Zero-FRT Imputation vs $0 Ad-Hoc Exclusion
	# -----------------------------------------------------------------------

	def test_zero_frt_flat_rate_imputation_vs_zero_weight_exclusion(self):
		"""Flat-rate line imputes weight from labor_amount/default_rate; $0 ad-hoc flags Zero-Weight Line Present."""
		from ism.ism.doctype.repair_order.pricing import _get_default_labor_rate

		rate = _get_default_labor_rate()
		t0 = datetime(2026, 11, 20, 9, 0, 0)

		# Flat-rate ad-hoc: labor_hours=0, labor_amount=2*rate -> imputed weight = 2.0h (vs 1.0h job -> 2:1 split)
		ro_imputed = self._create_completed_ro(
			services=[
				{
					"service_title": "Flat Rate Alignment",
					"status": "Approved",
					"is_completed": 1,
					"is_flat_rate": 1,
					"labor_hours": 0.0,
					"labor_amount": rate * 2.0,
					"completed_at": t0 + timedelta(hours=3),
				},
				{
					"service_title": "Tire Rotation",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=3),
				},
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=3),
					"time_in_hours": 3.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_imputed)
		obs_imp = {o["service_title"]: o for o in self._get_observations(ro_imputed.name)}
		self.assertAlmostEqual(flt(obs_imp["Flat Rate Alignment"]["allocation_weight"]), 2.0, places=3)
		self.assertAlmostEqual(flt(obs_imp["Flat Rate Alignment"]["allocated_hours"]), 2.0, places=3)
		self.assertAlmostEqual(flt(obs_imp["Tire Rotation"]["allocated_hours"]), 1.0, places=3)
		self.assertEqual(obs_imp["Flat Rate Alignment"]["review_status"], REVIEW_CLEAN)

		# $0 ad-hoc line -> Zero-Weight Line Present & excluded
		ro_zero = self._create_completed_ro(
			services=[
				{
					"service_title": "Goodwill Bolt Tighten",
					"status": "Approved",
					"is_completed": 1,
					"is_flat_rate": 1,
					"labor_hours": 0.0,
					"labor_amount": 0.0,
					"completed_at": t0 + timedelta(hours=1),
				},
				{
					"service_title": "Cabin Filter",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1),
				},
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1),
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro_zero)
		obs_zero = self._get_observations(ro_zero.name)
		self.assertEqual(len(obs_zero), 2)
		for o in obs_zero:
			self.assertEqual(o["review_status"], REVIEW_NEEDS_REVIEW)
			self.assertEqual(o["flag_reason"], "Zero-Weight Line Present")
			self.assertEqual(cint(o["exclude_from_calibration"]), 1)

	# -----------------------------------------------------------------------
	# 6. Multi-Technician Scoping vs RO-Wide Fallback
	# -----------------------------------------------------------------------

	def test_multi_tech_scoped_proration_vs_ro_wide_fallback(self):
		"""Clean tech->service mapping uses Tech-Scoped Prorated; incomplete mapping falls back to RO-Wide."""
		tech_a = self._create_employee("A")
		tech_b = self._create_employee("B")
		pkg_a = self._create_package("TechJobA", correction_code="31011002", standard_labor_hours=1.0)
		pkg_b = self._create_package("TechJobB", correction_code="18101001", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 20, 9, 0, 0)

		# Clean mapping: Tech A logged 2.0h for Job A; Tech B logged 1.0h for Job B
		ro_scoped = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_a.name,
					"correction_code": "31011002",
					"service_title": "Job A",
					"assigned_technician": tech_a.name,
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=2),
				},
				{
					"service_package": pkg_b.name,
					"correction_code": "18101001",
					"service_title": "Job B",
					"assigned_technician": tech_b.name,
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1),
				},
			],
			time_logs=[
				{
					"technician": tech_a.name,
					"from_time": t0,
					"to_time": t0 + timedelta(hours=2),
					"time_in_hours": 2.0,
					"session_status": SESSION_CLOSED,
				},
				{
					"technician": tech_b.name,
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1),
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				},
			],
		)
		recompute_observations_for_ro(ro_scoped)
		obs_scoped = {o["service_title"]: o for o in self._get_observations(ro_scoped.name)}
		self.assertEqual(obs_scoped["Job A"]["allocation_method"], ALLOC_TECH_SCOPED)
		self.assertEqual(obs_scoped["Job B"]["allocation_method"], ALLOC_TECH_SCOPED)
		self.assertAlmostEqual(flt(obs_scoped["Job A"]["allocated_hours"]), 2.0, places=3)
		self.assertAlmostEqual(flt(obs_scoped["Job B"]["allocated_hours"]), 1.0, places=3)
		self.assertEqual(flt(obs_scoped["Job A"]["confidence_score"]), 1.0)
		self.assertEqual(flt(obs_scoped["Job B"]["confidence_score"]), 1.0)

		# Incomplete mapping (Job B unassigned) -> RO-Wide Prorated splits 3.0h equally (1.5h each)
		# Use fresh uncalibrated packages and a non-overlapping day so prior calibration on pkg_a
		# and tech_a/tech_b sessions from ro_scoped do not affect the equal-weight RO-wide check.
		pkg_c = self._create_package("TechJobC", correction_code="31011003", standard_labor_hours=1.0)
		pkg_d = self._create_package("TechJobD", correction_code="18101002", standard_labor_hours=1.0)
		t1 = t0 + timedelta(days=1)
		ro_wide = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_c.name,
					"correction_code": "31011003",
					"service_title": "Job A",
					"assigned_technician": tech_a.name,
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t1 + timedelta(hours=2),
				},
				{
					"service_package": pkg_d.name,
					"correction_code": "18101002",
					"service_title": "Job B",
					"assigned_technician": None,
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t1 + timedelta(hours=1),
				},
			],
			time_logs=[
				{
					"technician": tech_a.name,
					"from_time": t1,
					"to_time": t1 + timedelta(hours=2),
					"time_in_hours": 2.0,
					"session_status": SESSION_CLOSED,
				},
				{
					"technician": tech_b.name,
					"from_time": t1,
					"to_time": t1 + timedelta(hours=1),
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				},
			],
		)
		recompute_observations_for_ro(ro_wide)
		obs_wide = {o["service_title"]: o for o in self._get_observations(ro_wide.name)}
		self.assertEqual(obs_wide["Job A"]["allocation_method"], ALLOC_RO_WIDE)
		self.assertEqual(obs_wide["Job B"]["allocation_method"], ALLOC_RO_WIDE)
		self.assertAlmostEqual(flt(obs_wide["Job A"]["allocated_hours"]), 1.5, places=3)
		self.assertAlmostEqual(flt(obs_wide["Job B"]["allocated_hours"]), 1.5, places=3)
		self.assertEqual(flt(obs_wide["Job A"]["confidence_score"]), 0.5)

	# -----------------------------------------------------------------------
	# 7. Manager Review Toggle, Preservation on Recompute & RO Cancellation Voiding
	# -----------------------------------------------------------------------

	def test_manager_review_toggle_recalibrates_and_cancellation_voids_observations(self):
		"""Manager exclude/confirm toggles immediately recalibrate package; RO cancellation voids obs."""
		pkg = self._create_package("ReviewTogglePkg", correction_code="33011001", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 20, 9, 0, 0)

		ro = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Flush",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=2),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=2),
					"time_in_hours": 2.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		on_repair_order_update(ro)
		obs_name = self._get_observations(ro.name)[0]["name"]

		pkg.reload()
		self.assertGreater(flt(pkg.calibrated_median_minutes), 60.0)
		self.assertGreater(flt(pkg.effective_sample_count), 0.0)

		# Manager excludes observation -> package immediately recalibrates back to 0 samples
		obs_doc = frappe.get_doc("Service Time Observation", obs_name)
		obs_doc.exclude_from_calibration = 1
		obs_doc.save(ignore_permissions=True)
		self.assertEqual(obs_doc.review_status, REVIEW_EXCLUDED)
		on_observation_update(obs_doc)

		pkg.reload()
		self.assertEqual(flt(pkg.effective_sample_count), 0.0)
		self.assertEqual(flt(pkg.calibrated_median_minutes), 0.0)

		# Recomputing RO with unchanged allocated_hours preserves manager Excluded decision!
		recompute_observations_for_ro(ro)
		obs_doc.reload()
		self.assertEqual(obs_doc.review_status, REVIEW_EXCLUDED)
		self.assertEqual(cint(obs_doc.exclude_from_calibration), 1)

		# Manager confirms observation -> package immediately recalibrates again
		obs_doc.review_status = REVIEW_CONFIRMED
		obs_doc.exclude_from_calibration = 0
		obs_doc.save(ignore_permissions=True)
		on_observation_update(obs_doc)

		pkg.reload()
		self.assertGreater(flt(pkg.effective_sample_count), 0.0)
		self.assertGreater(flt(pkg.calibrated_median_minutes), 60.0)

		# RO Cancelled -> marks observations Voided + excluded and recalibrates package
		ro.status = "Cancelled"
		on_repair_order_update(ro)

		obs_doc.reload()
		self.assertEqual(obs_doc.review_status, REVIEW_VOIDED)
		self.assertEqual(cint(obs_doc.exclude_from_calibration), 1)

		pkg.reload()
		self.assertEqual(flt(pkg.effective_sample_count), 0.0)
		self.assertEqual(flt(pkg.calibrated_median_minutes), 0.0)

	def test_disabled_mode_skips_service_hooks_and_nightly_job(self):
		"""When ISM Settings.time_calibration_mode == Disabled, hooks and nightly job are no-ops."""
		frappe.db.set_single_value("ISM Settings", "time_calibration_mode", MODE_DISABLED)
		frappe.clear_cache(doctype="ISM Settings")

		pkg = self._create_package("DisabledPkg", correction_code="33011001", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 20, 9, 0, 0)
		ro = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Flush",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1),
					"time_in_hours": 1.0,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		res = on_repair_order_update(ro)
		self.assertTrue(res["skipped"])
		self.assertEqual(len(self._get_observations(ro.name)), 0)

		nightly = run_nightly_calibration_job()
		self.assertTrue(nightly["skipped"])

		all_res = recalibrate_all()
		self.assertTrue(all_res["skipped"])

	# -----------------------------------------------------------------------
	# 8. Nightly Alternating Ridge Deconvolution E2E against MariaDB
	# -----------------------------------------------------------------------

	def test_nightly_deconvolution_job_recovers_bundled_packages_in_db(self):
		"""run_nightly_calibration_job updates Service Package stats from bundled DB observations without altering FRT."""
		pkg_a = self._create_package("DeconvA", correction_code="31011002", standard_labor_hours=1.0)
		pkg_b = self._create_package("DeconvB", correction_code="18101001", standard_labor_hours=1.0)
		pkg_c = self._create_package("DeconvC", correction_code="33011001", standard_labor_hours=1.0)

		# True times: A=1.8h, B=0.6h, C=1.2h — A & B only appear in varied bundles!
		combos = [
			(pkg_a, pkg_b, 2.4),
			(pkg_a, pkg_c, 3.0),
			(pkg_b, pkg_c, 1.8),
			(pkg_a, pkg_a, 3.6),
			(pkg_b, pkg_b, 1.2),
		]
		base_dt = datetime(2026, 10, 15, 8, 30, 0)
		idx = 0
		for _rep in range(5):
			for p1, p2, total_h in combos:
				idx += 1
				t0 = base_dt + timedelta(days=idx)
				t1 = t0 + timedelta(hours=total_h)
				pos1 = "LH" if p1 == p2 else "None"
				pos2 = "RH" if p1 == p2 else "None"
				ro = self._create_completed_ro(
					services=[
						{
							"service_package": p1.name,
							"correction_code": p1.correction_code,
							"service_title": f"Svc 1 {idx}",
							"service_position": pos1,
							"status": "Approved",
							"is_completed": 1,
							"labor_hours": 1.0,
							"completed_at": t1,
						},
						{
							"service_package": p2.name,
							"correction_code": p2.correction_code,
							"service_title": f"Svc 2 {idx}",
							"service_position": pos2,
							"status": "Approved",
							"is_completed": 1,
							"labor_hours": 1.0,
							"completed_at": t1,
						},
					],
					time_logs=[
						{
							"from_time": t0,
							"to_time": t1,
							"time_in_hours": total_h,
							"session_status": SESSION_CLOSED,
						}
					],
				)
				recompute_observations_for_ro(ro)

		summary = run_nightly_calibration_job(now=datetime(2026, 11, 25, 12, 0, 0))
		self.assertFalse(summary["skipped"])
		self.assertGreaterEqual(summary["pools_processed"], 25)
		self.assertGreaterEqual(summary["packages_updated"], 3)

		pkg_a.reload()
		pkg_b.reload()
		pkg_c.reload()

		# Factory FRT NEVER modified
		self.assertEqual(flt(pkg_a.standard_labor_hours), 1.0)
		self.assertEqual(flt(pkg_b.standard_labor_hours), 1.0)
		self.assertEqual(flt(pkg_c.standard_labor_hours), 1.0)

		# Deconvolution separates A (>> 60 min) from B (<< 60 min) cleanly!
		self.assertGreater(flt(pkg_a.calibrated_median_minutes), 95.0)
		self.assertLess(flt(pkg_b.calibrated_median_minutes), 48.0)
		self.assertGreater(flt(pkg_c.calibrated_median_minutes), 65.0)
		self.assertLess(flt(pkg_c.calibrated_median_minutes), 78.0)

	# -----------------------------------------------------------------------
	# 9. Regression Tests: Bundle-Only Preservation, Review Sync, RO Reopen
	# -----------------------------------------------------------------------

	def test_bundle_only_package_preserves_deconvolved_calibration(self):
		"""recalibrate_service_package must NOT zero out deconvolved stats when only same-corner bundles exist."""
		pkg_a = self._create_package("BundleOnlyA", correction_code="31011002", standard_labor_hours=1.0)
		pkg_b = self._create_package("BundleOnlyB", correction_code="31011005", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 18, 9, 0, 0)

		ro = self._create_completed_ro(
			services=[
				{
					"service_package": pkg_a.name,
					"correction_code": "31011002",
					"service_title": "Front Upper Control Arm LH",
					"service_position": "LH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1.6),
				},
				{
					"service_package": pkg_b.name,
					"correction_code": "31011005",
					"service_title": "Front Lower Lateral Link LH",
					"service_position": "LH",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1.6),
				},
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1.6),
					"time_in_hours": 1.6,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		recompute_observations_for_ro(ro)

		# Simulate nightly deconvolution having calibrated pkg_a
		frappe.db.set_value(
			"Service Package",
			pkg_a.name,
			{
				"calibrated_median_minutes": 72.5,
				"calibrated_sigma": 0.24,
				"frt_realization_ratio": 1.21,
				"effective_sample_count": 4.0,
			},
			update_modified=False,
		)

		res = recalibrate_service_package(pkg_a.name)
		self.assertTrue(res.get("preserved_deconvolved"))
		pkg_a.reload()
		self.assertAlmostEqual(flt(pkg_a.calibrated_median_minutes), 72.5, places=1)
		self.assertAlmostEqual(flt(pkg_a.calibrated_sigma), 0.24, places=3)

	def test_observation_review_sync_and_ro_reopen_voiding(self):
		"""Unchecking exclude on Needs Review confirms it; Clean clears exclude; reopening RO voids active obs."""
		pkg = self._create_package("ReopenSyncPkg", correction_code="33011001", standard_labor_hours=1.0)
		t0 = datetime(2026, 11, 19, 9, 0, 0)

		ro = self._create_completed_ro(
			services=[
				{
					"service_package": pkg.name,
					"correction_code": "33011001",
					"service_title": "Brake Service",
					"status": "Approved",
					"is_completed": 1,
					"labor_hours": 1.0,
					"completed_at": t0 + timedelta(hours=1.5),
				}
			],
			time_logs=[
				{
					"from_time": t0,
					"to_time": t0 + timedelta(hours=1.5),
					"time_in_hours": 1.5,
					"session_status": SESSION_CLOSED,
				}
			],
		)
		on_repair_order_update(ro)
		obs_list = self._get_observations(ro.name)
		self.assertEqual(len(obs_list), 1)
		obs = frappe.get_doc("Service Time Observation", obs_list[0]["name"])
		self.assertEqual(obs.review_status, REVIEW_CLEAN)

		# Transition to Needs Review with exclude=1, then uncheck exclude -> Confirmed
		obs.review_status = REVIEW_NEEDS_REVIEW
		obs.exclude_from_calibration = 1
		obs.save(ignore_permissions=True)
		self.assertEqual(obs.review_status, REVIEW_NEEDS_REVIEW)
		self.assertEqual(cint(obs.exclude_from_calibration), 1)

		obs.exclude_from_calibration = 0
		obs.save(ignore_permissions=True)
		self.assertEqual(obs.review_status, REVIEW_CONFIRMED)
		self.assertEqual(cint(obs.exclude_from_calibration), 0)

		# Transition to Excluded, then change review_status to Clean -> exclude cleared to 0
		obs.review_status = REVIEW_EXCLUDED
		obs.save(ignore_permissions=True)
		self.assertEqual(cint(obs.exclude_from_calibration), 1)

		obs.review_status = REVIEW_CLEAN
		obs.save(ignore_permissions=True)
		self.assertEqual(obs.review_status, REVIEW_CLEAN)
		self.assertEqual(cint(obs.exclude_from_calibration), 0)

		pkg.reload()
		self.assertGreater(flt(pkg.effective_sample_count), 0.0)

		# Reopening a Work Completed RO back to Authorized - In Progress must void active observations
		ro.status = "Authorized - In Progress"
		ro.save(ignore_permissions=True)
		obs.reload()
		self.assertEqual(obs.review_status, REVIEW_VOIDED)
		self.assertEqual(cint(obs.exclude_from_calibration), 1)

		pkg.reload()
		self.assertAlmostEqual(flt(pkg.effective_sample_count), 0.0, places=2)
		self.assertAlmostEqual(flt(pkg.calibrated_median_minutes), 0.0, places=2)

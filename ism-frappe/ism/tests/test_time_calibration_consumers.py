# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Integration tests for Wave 2C time calibration consumers:
- estimation_service.py (estimate_repair_order, get_estimate)
- bookable_catalog.py (compute_packages_booking_requirements, list_bookable_packages)
- api/time_calibration.py (permissions & endpoints)
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from ism.api.estimation_service import estimate_repair_order, get_estimate
from ism.api.time_calibration import (
	get_package_time_accuracy,
	list_flagged_time_observations,
	review_time_observation,
	run_time_calibration,
)
from ism.scheduling.bookable_catalog import (
	compute_packages_booking_requirements,
	list_bookable_packages,
)
from ism.scheduling.estimation_light import (
	estimate_duration,
	estimate_total_duration,
	get_sigma_for_item,
)
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.time_calibration.contracts import (
	DEFAULT_DRIFT_ALERT_PCT,
	DEFAULT_HALF_LIFE_DAYS,
	DEFAULT_MAX_RATIO,
	DEFAULT_MAX_SINGLE_SESSION_HOURS,
	DEFAULT_MIN_RATIO,
	DEFAULT_OVERLAP_PRIOR_STRENGTH,
	DEFAULT_PRIOR_STRENGTH,
	MODE_ACTIVE,
	MODE_DISABLED,
	MODE_SHADOW,
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_NEEDS_REVIEW,
)

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

CALIBRATION_INTERNAL_KEYS = {
	"calibrated_median_minutes",
	"calibrated_sigma",
	"overlap_group",
	"correction_code",
	"is_symmetrical",
	"exclude_from_time_calibration",
	"frt_realization_ratio",
	"effective_sample_count",
	"drift_alert",
	"last_calibrated_at",
}


class TestTimeCalibrationConsumers(ISMTestCase):
	"""End-to-end DB tests (no DB mocks) for time calibration estimator & API consumers."""

	def setUp(self):
		super().setUp()
		self.manifest = self.get_manifest()
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)
		self._orig_user = frappe.session.user
		self._orig_settings = self._snapshot_ism_settings()

	def tearDown(self):
		try:
			frappe.set_user(self._orig_user or "Administrator")
			if self._orig_settings:
				self._set_ism_settings(**self._orig_settings)
		finally:
			super().tearDown()

	def _snapshot_ism_settings(self) -> dict:
		if not frappe.db.exists("DocType", "ISM Settings"):
			return {}
		doc = frappe.get_single("ISM Settings")
		return {
			"time_calibration_mode": getattr(doc, "time_calibration_mode", MODE_SHADOW),
			"estimation_percentile": getattr(doc, "estimation_percentile", 0.80),
			"estimation_sigma": getattr(doc, "estimation_sigma", 0.30),
			"buffer_padding_minutes": getattr(doc, "buffer_padding_minutes", 0),
		}

	def _set_ism_settings(self, **kwargs):
		doc = frappe.get_single("ISM Settings")
		# Guard against migrated Single DB rows initialized to 0.0 for new calibration fields.
		cal_defaults = {
			"calibration_prior_strength": DEFAULT_PRIOR_STRENGTH,
			"calibration_overlap_prior_strength": DEFAULT_OVERLAP_PRIOR_STRENGTH,
			"calibration_half_life_days": DEFAULT_HALF_LIFE_DAYS,
			"calibration_min_ratio": DEFAULT_MIN_RATIO,
			"calibration_max_ratio": DEFAULT_MAX_RATIO,
			"calibration_drift_alert_pct": DEFAULT_DRIFT_ALERT_PCT,
			"calibration_max_single_session_hours": DEFAULT_MAX_SINGLE_SESSION_HOURS,
		}
		for field, default_val in cal_defaults.items():
			if not getattr(doc, field, None):
				setattr(doc, field, default_val)
		for key, val in kwargs.items():
			setattr(doc, key, val)
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype="ISM Settings")

	def _create_overlap_group(self, code: str, manual_factor: float = 0.50):
		clean_code = code.strip().upper()
		if frappe.db.exists("Service Overlap Group", clean_code):
			doc = frappe.get_doc("Service Overlap Group", clean_code)
			doc.manual_overlap_factor = manual_factor
			doc.save(ignore_permissions=True)
			return doc
		doc = frappe.get_doc(
			{
				"doctype": "Service Overlap Group",
				"group_code": clean_code,
				"group_label": f"Test Group {clean_code}",
				"is_auto_derived": 0,
				"calibrated_overlap_factor": 1.0,
				"manual_overlap_factor": manual_factor,
			}
		).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _create_package(self, suffix: str, **kwargs):
		pkg_name = f"{TEST_PREFIX}CalPkg_{suffix}_{frappe.generate_hash(length=5)}"
		payload = {
			"doctype": "Service Package",
			"package_name": pkg_name,
			"applicable_model": "Model 3",
			"standard_labor_hours": 1.0,
			"hourly_rate": 140.0,
			"service_rate": 140.0,
			"allow_self_serve_booking": 1,
			"customer_price_display_mode": "Exact Price",
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _create_role_user(self, suffix: str, roles: list[str]) -> str:
		email = f"{TEST_PREFIX.lower()}cal_{suffix}_{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": f"Test {suffix}",
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)
		self.track(user)
		return user.name

	# ------------------------------------------------------------------
	# 1. Shadow vs Active in estimate_repair_order & get_estimate
	# ------------------------------------------------------------------

	def test_shadow_vs_active_and_billing_labor_hours_independence(self):
		"""In Shadow/Disabled mode RO estimates follow FRT/billing hours;
		in Active mode calibrated package stats are used directly and ignore billing labor_hours edits."""
		self._set_ism_settings(
			time_calibration_mode=MODE_SHADOW,
			estimation_percentile=0.80,
			estimation_sigma=0.30,
			buffer_padding_minutes=0,
		)

		pkg = self._create_package(
			"ControlArm",
			standard_labor_hours=1.0,
			correction_code="31011002",
			calibrated_median_minutes=120.0,
			calibrated_sigma=0.20,
			frt_realization_ratio=2.0,
			effective_sample_count=10.0,
			drift_alert=1,
		)

		ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
		ro.append(
			"services",
			{
				"service_package": pkg.name,
				"service_title": "Front Upper Control Arm - LH",
				"correction_code": "31011002",
				"service_position": "LH",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro.save(ignore_permissions=True)

		# Shadow mode: uses FRT / labor_hours (60 min) with category sigma (Suspension -> 0.35)
		susp_sigma = get_sigma_for_item("Suspension", default_sigma=0.30)
		shadow_ro = estimate_repair_order(ro.name)
		expected_shadow = estimate_duration(60.0, sigma=susp_sigma, percentile=0.80)
		self.assertEqual(shadow_ro["p_pure"], expected_shadow)

		# Disabled mode: identical to Shadow
		self._set_ism_settings(time_calibration_mode=MODE_DISABLED)
		disabled_ro = estimate_repair_order(ro.name)
		self.assertEqual(disabled_ro["p_pure"], shadow_ro["p_pure"])

		# Active mode: uses calibrated_median_minutes=120.0, calibrated_sigma=0.20
		self._set_ism_settings(time_calibration_mode=MODE_ACTIVE)
		active_ro = estimate_repair_order(ro.name)
		expected_active = estimate_duration(120.0, sigma=0.20, percentile=0.80)
		self.assertEqual(active_ro["p_pure"], expected_active)
		self.assertNotEqual(active_ro["p_pure"], shadow_ro["p_pure"])

		# Advisor edits billing labor_hours to 2.5h -> Active estimate MUST stay anchored to calibrated stats
		ro.services[0].labor_hours = 2.5
		ro.save(ignore_permissions=True)
		active_after_billing_edit = estimate_repair_order(ro.name)
		self.assertEqual(active_after_billing_edit["p_pure"], expected_active)

		# In Shadow mode, that billing labor_hours edit DOES change the FRT-based estimate
		self._set_ism_settings(time_calibration_mode=MODE_SHADOW)
		shadow_after_billing_edit = estimate_repair_order(ro.name)
		self.assertEqual(
			shadow_after_billing_edit["p_pure"],
			estimate_duration(150.0, sigma=susp_sigma, percentile=0.80),
		)

		# get_estimate with service_package kwarg: Shadow vs Active
		shadow_single = get_estimate(service_package=pkg.name, flat_rate_minutes=60.0)
		self.assertEqual(shadow_single["p_pure"], estimate_duration(60.0, sigma=0.30, percentile=0.80))

		self._set_ism_settings(time_calibration_mode=MODE_ACTIVE)
		active_single = get_estimate(service_package=pkg.name, flat_rate_minutes=60.0)
		self.assertEqual(active_single["p_pure"], expected_active)
		self.assertAlmostEqual(active_single["sigma"], 0.20, places=4)

	def test_diagnostic_and_excluded_package_ignores_calibration(self):
		"""Diagnostic (00xx) or exclude_from_time_calibration=1 packages never use calibrated median."""
		self._set_ism_settings(
			time_calibration_mode=MODE_ACTIVE,
			estimation_percentile=0.80,
			estimation_sigma=0.30,
			buffer_padding_minutes=0,
		)
		diag_pkg = self._create_package(
			"HV_Diag",
			standard_labor_hours=1.0,
			correction_code="00100100",
			calibrated_median_minutes=180.0,
			calibrated_sigma=0.20,
		)
		excl_pkg = self._create_package(
			"Excluded_Job",
			standard_labor_hours=1.0,
			correction_code="31011005",
			exclude_from_time_calibration=1,
			calibrated_median_minutes=180.0,
			calibrated_sigma=0.20,
		)

		res_diag = get_estimate(service_package=diag_pkg.name, flat_rate_minutes=60.0)
		self.assertEqual(res_diag["p_pure"], estimate_duration(60.0, sigma=0.30, percentile=0.80))

		res_excl = get_estimate(service_package=excl_pkg.name, flat_rate_minutes=60.0)
		self.assertEqual(res_excl["p_pure"], estimate_duration(60.0, sigma=0.30, percentile=0.80))

	# ------------------------------------------------------------------
	# 2. Same-corner alpha_g discount vs opposite-side LH+RH independence
	# ------------------------------------------------------------------

	def test_overlap_discount_same_corner_vs_opposite_side_ro(self):
		"""Same-corner (group, LH) jobs get alpha_g discount in Active mode;
		opposite-side (LH + RH) jobs remain undiscounted; Shadow mode never discounts."""
		self._set_ism_settings(
			time_calibration_mode=MODE_ACTIVE,
			estimation_percentile=0.80,
			estimation_sigma=0.30,
			buffer_padding_minutes=0,
		)
		grp = self._create_overlap_group("9931", manual_factor=0.50)

		pkg_a = self._create_package(
			"SuspUpper",
			standard_labor_hours=1.0,
			overlap_group=grp.name,
			calibrated_median_minutes=60.0,
			calibrated_sigma=0.25,
		)
		pkg_b = self._create_package(
			"SuspLower",
			standard_labor_hours=1.0,
			overlap_group=grp.name,
			calibrated_median_minutes=60.0,
			calibrated_sigma=0.25,
		)

		# Same corner: LH + LH
		ro_same = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
		ro_same.append(
			"services",
			{
				"service_package": pkg_a.name,
				"service_title": "Upper Link - LH",
				"service_position": "LH",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro_same.append(
			"services",
			{
				"service_package": pkg_b.name,
				"service_title": "Lower Link - LH",
				"service_position": "LH",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro_same.save(ignore_permissions=True)

		# Opposite side: LH + RH
		ro_opp = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
		ro_opp.append(
			"services",
			{
				"service_package": pkg_a.name,
				"service_title": "Upper Link - LH",
				"service_position": "LH",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro_opp.append(
			"services",
			{
				"service_package": pkg_b.name,
				"service_title": "Lower Link - RH",
				"service_position": "RH",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro_opp.save(ignore_permissions=True)

		est_same = estimate_repair_order(ro_same.name)
		est_opp = estimate_repair_order(ro_opp.name)

		expected_discounted = estimate_total_duration(
			[
				{"flat_rate_minutes": 60.0, "sigma": 0.25},
				{"flat_rate_minutes": 30.0, "sigma": 0.25},
			],
			percentile=0.80,
		)
		expected_undiscounted = estimate_total_duration(
			[
				{"flat_rate_minutes": 60.0, "sigma": 0.25},
				{"flat_rate_minutes": 60.0, "sigma": 0.25},
			],
			percentile=0.80,
		)

		self.assertEqual(est_same["p_pure"], expected_discounted)
		self.assertEqual(est_opp["p_pure"], expected_undiscounted)
		self.assertLess(est_same["p_pure"], est_opp["p_pure"])

		# Shadow mode: no overlap discount applied
		self._set_ism_settings(time_calibration_mode=MODE_SHADOW)
		est_same_shadow = estimate_repair_order(ro_same.name)
		est_opp_shadow = estimate_repair_order(ro_opp.name)
		self.assertEqual(est_same_shadow["p_pure"], est_opp_shadow["p_pure"])

	# ------------------------------------------------------------------
	# 3. Bookable Catalog (/book): Active vs Shadow, override precedence,
	#    same-corner overlap discount, and zero public field leakage
	# ------------------------------------------------------------------

	def test_bookable_catalog_active_mode_override_overlap_and_zero_leakage(self):
		"""compute_packages_booking_requirements uses calibrated medians + overlap discounts in Active mode,
		respects manual self_serve_duration_minutes override, and leaks zero internal fields."""
		self._set_ism_settings(
			time_calibration_mode=MODE_SHADOW,
			estimation_percentile=0.80,
			estimation_sigma=0.30,
			buffer_padding_minutes=0,
		)
		grp = self._create_overlap_group("9932", manual_factor=0.50)

		pkg_lh_1 = self._create_package(
			"BookLH1",
			package_name=f"{TEST_PREFIX}Front Upper Link - LH",
			customer_title="Front Upper Link - LH",
			standard_labor_hours=1.0,
			overlap_group=grp.name,
			calibrated_median_minutes=90.0,
			calibrated_sigma=0.20,
			self_serve_duration_minutes=0,
		)
		pkg_lh_2 = self._create_package(
			"BookLH2",
			package_name=f"{TEST_PREFIX}Front Lower Link - LH",
			customer_title="Front Lower Link - LH",
			standard_labor_hours=1.0,
			overlap_group=grp.name,
			calibrated_median_minutes=90.0,
			calibrated_sigma=0.20,
			self_serve_duration_minutes=0,
		)
		pkg_rh_2 = self._create_package(
			"BookRH2",
			package_name=f"{TEST_PREFIX}Front Lower Link - RH",
			customer_title="Front Lower Link - RH",
			standard_labor_hours=1.0,
			overlap_group=grp.name,
			calibrated_median_minutes=90.0,
			calibrated_sigma=0.20,
			self_serve_duration_minutes=0,
		)
		pkg_override = self._create_package(
			"BookOverride",
			package_name=f"{TEST_PREFIX}Manual Override Service",
			customer_title="Manual Override Service",
			standard_labor_hours=1.0,
			calibrated_median_minutes=180.0,
			calibrated_sigma=0.20,
			self_serve_duration_minutes=45,
		)

		# Shadow mode: single package uses FRT=60, sigma=0.30
		req_shadow = compute_packages_booking_requirements([pkg_lh_1.name], vehicle_model="Model 3")
		self.assertEqual(
			req_shadow["duration_minutes"],
			estimate_total_duration([60.0], sigma=0.30, percentile=0.80),
		)

		# Active mode: single package uses calibrated median=90, sigma=0.20
		self._set_ism_settings(time_calibration_mode=MODE_ACTIVE)
		req_active = compute_packages_booking_requirements([pkg_lh_1.name], vehicle_model="Model 3")
		self.assertEqual(
			req_active["duration_minutes"],
			estimate_total_duration([{"flat_rate_minutes": 90.0, "sigma": 0.20}], percentile=0.80),
		)

		# Manual self_serve_duration_minutes=45 takes highest precedence over calibrated_median_minutes=180
		req_override = compute_packages_booking_requirements([pkg_override.name], vehicle_model="Model 3")
		self.assertEqual(req_override["duration_minutes"], 45)

		# Same-corner (LH + LH) gets discounted vs opposite-side (LH + RH) undiscounted
		req_same_corner = compute_packages_booking_requirements(
			[pkg_lh_1.name, pkg_lh_2.name],
			vehicle_model="Model 3",
		)
		req_opp_side = compute_packages_booking_requirements(
			[pkg_lh_1.name, pkg_rh_2.name],
			vehicle_model="Model 3",
		)
		expected_same = estimate_total_duration(
			[
				{"flat_rate_minutes": 90.0, "sigma": 0.20},
				{"flat_rate_minutes": 45.0, "sigma": 0.20},
			],
			percentile=0.80,
		)
		expected_opp = estimate_total_duration(
			[
				{"flat_rate_minutes": 90.0, "sigma": 0.20},
				{"flat_rate_minutes": 90.0, "sigma": 0.20},
			],
			percentile=0.80,
		)
		self.assertEqual(req_same_corner["duration_minutes"], expected_same)
		self.assertEqual(req_opp_side["duration_minutes"], expected_opp)
		self.assertLess(req_same_corner["duration_minutes"], req_opp_side["duration_minutes"])

		# Zero public field leakage in list_bookable_packages & booking response cards
		cards = list_bookable_packages(vehicle_model="Model 3")
		our_cards = [c for c in cards if c["package_id"] in {pkg_lh_1.name, pkg_override.name}]
		self.assertEqual(len(our_cards), 2)
		for card in our_cards + req_same_corner["packages"]:
			self.assertEqual(set(card.keys()), PUBLIC_CARD_KEYS)
			self.assertTrue(CALIBRATION_INTERNAL_KEYS.isdisjoint(card.keys()))

	# ------------------------------------------------------------------
	# 4. Manager/Advisor APIs & Permission Enforcement
	# ------------------------------------------------------------------

	def test_time_calibration_api_permissions_and_workflow(self):
		"""Guest and ISM Technician are denied with PermissionError;
		Advisor can read/review but not run calibration; Manager/Admin can run all endpoints."""
		pkg = self._create_package(
			"ApiPkg",
			standard_labor_hours=1.0,
			calibrated_median_minutes=85.0,
			calibrated_sigma=0.22,
			frt_realization_ratio=1.42,
			effective_sample_count=8.5,
			drift_alert=1,
		)
		ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
		ro.append(
			"services",
			{
				"service_package": pkg.name,
				"service_title": "Test Calibration Service",
				"labor_hours": 1.0,
				"status": "Approved",
			},
		)
		ro.save(ignore_permissions=True)

		obs = frappe.get_doc(
			{
				"doctype": "Service Time Observation",
				"repair_order": ro.name,
				"repair_order_service": ro.services[0].name,
				"service_package": pkg.name,
				"service_title": "Test Calibration Service",
				"observed_at": now_datetime(),
				"frt_hours": 1.0,
				"allocated_hours": 1.5,
				"allocated_minutes": 90.0,
				"ro_net_active_hours": 1.5,
				"allocation_method": "Single-Job Direct",
				"confidence_score": 1.0,
				"frt_ratio": 1.5,
				"review_status": REVIEW_NEEDS_REVIEW,
				"flag_reason": "Oversized Session",
				"exclude_from_calibration": 1,
			}
		).insert(ignore_permissions=True)
		self.track(obs)

		tech_user = self._create_role_user("tech", ["ISM Technician"])
		advisor_user = self._create_role_user("advisor", ["ISM Service Advisor"])
		manager_user = self._create_role_user("manager", ["ISM Manager"])

		# 1. Guest denied on all 4 endpoints
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_package_time_accuracy(service_package=pkg.name)
		with self.assertRaises(frappe.PermissionError):
			list_flagged_time_observations(repair_order=ro.name)
		with self.assertRaises(frappe.PermissionError):
			review_time_observation(observation=obs.name, action="confirm")
		with self.assertRaises(frappe.PermissionError):
			run_time_calibration(repair_order=ro.name)

		# 2. ISM Technician denied on all 4 endpoints
		frappe.set_user(tech_user)
		with self.assertRaises(frappe.PermissionError):
			get_package_time_accuracy(service_package=pkg.name)
		with self.assertRaises(frappe.PermissionError):
			list_flagged_time_observations(repair_order=ro.name)
		with self.assertRaises(frappe.PermissionError):
			review_time_observation(observation=obs.name, action="confirm")
		with self.assertRaises(frappe.PermissionError):
			run_time_calibration(repair_order=ro.name)

		# 3. ISM Service Advisor allowed on read & review, denied on run_time_calibration
		frappe.set_user(advisor_user)
		acc = get_package_time_accuracy(service_package=pkg.name, only_drift_alerts=1)
		self.assertEqual(acc["count"], 1)
		self.assertEqual(acc["packages"][0]["name"], pkg.name)
		self.assertAlmostEqual(acc["packages"][0]["calibrated_median_minutes"], 85.0, places=1)
		self.assertEqual(acc["packages"][0]["drift_alert"], 1)

		flagged = list_flagged_time_observations(repair_order=ro.name, review_status=REVIEW_NEEDS_REVIEW)
		self.assertEqual(flagged["count"], 1)
		self.assertEqual(flagged["observations"][0]["name"], obs.name)

		confirm_res = review_time_observation(
			observation=obs.name,
			action="confirm",
			notes="Verified session duration with tech.",
		)
		self.assertEqual(confirm_res["review_status"], REVIEW_CONFIRMED)
		self.assertEqual(confirm_res["exclude_from_calibration"], 0)

		with self.assertRaises(frappe.PermissionError):
			run_time_calibration(repair_order=ro.name)

		# 4. ISM Manager allowed on all endpoints including exclude + run_time_calibration
		frappe.set_user(manager_user)
		exclude_res = review_time_observation(
			observation=obs.name,
			action="exclude",
			notes="Exclude anomalous bay pause.",
		)
		self.assertEqual(exclude_res["review_status"], REVIEW_EXCLUDED)
		self.assertEqual(exclude_res["exclude_from_calibration"], 1)

		run_res = run_time_calibration(repair_order=ro.name)
		self.assertIn(run_res["status"], ("ok", "unavailable"))
		self.assertEqual(run_res["repair_order"], ro.name)

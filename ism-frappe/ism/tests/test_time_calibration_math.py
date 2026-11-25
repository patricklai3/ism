# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Pure mathematical tests for Bayesian calibration, overlap factors, and alternating ridge deconvolution."""

from __future__ import annotations

import math

from ism.tests.base import ISMTestCase
from ism.time_calibration.bayesian import (
	compute_bayesian_package_posterior,
	compute_overlap_group_posterior,
)
from ism.time_calibration.contracts import (
	ALPHA_CLAMP_MAX,
	ALPHA_CLAMP_MIN,
	SIGMA_CLAMP_MAX,
	SIGMA_CLAMP_MIN,
)
from ism.time_calibration.deconvolution import solve_alternating_ridge_deconvolution


class TestTimeCalibrationMath(ISMTestCase):
	"""Verify pure-Python Bayesian shrinkage, overlap learning, and alternating ridge deconvolution."""

	def test_bayesian_shrinkage_convergence_from_w0_to_w50(self):
		"""Posterior median starts at FRT prior and converges smoothly toward observed median as W grows."""
		frt_min = 60.0
		obs_min = 90.0  # 1.5x FRT

		res_0 = compute_bayesian_package_posterior(
			frt_minutes=frt_min,
			observations=[],
			prior_sigma=0.30,
			prior_strength=5.0,
		)
		self.assertEqual(res_0["effective_sample_count"], 0.0)
		self.assertEqual(res_0["calibrated_median_minutes"], 0.0)
		self.assertEqual(res_0["drift_alert"], 0)

		obs_5 = [{"allocated_minutes": obs_min, "confidence_score": 1.0, "age_days": 0.0} for _ in range(5)]
		res_5 = compute_bayesian_package_posterior(
			frt_minutes=frt_min,
			observations=obs_5,
			prior_sigma=0.30,
			prior_strength=5.0,
		)
		# With n0=5 and W=5, log-space midpoint is geometric mean sqrt(60 * 90) ~= 73.48 min
		expected_5 = math.sqrt(frt_min * obs_min)
		self.assertAlmostEqual(res_5["calibrated_median_minutes"], expected_5, delta=0.2)
		self.assertEqual(res_5["effective_sample_count"], 5.0)
		self.assertGreater(res_5["frt_realization_ratio"], 1.15)
		self.assertLess(res_5["frt_realization_ratio"], 1.30)

		obs_50 = [{"allocated_minutes": obs_min, "confidence_score": 1.0, "age_days": 0.0} for _ in range(50)]
		res_50 = compute_bayesian_package_posterior(
			frt_minutes=frt_min,
			observations=obs_50,
			prior_sigma=0.30,
			prior_strength=5.0,
		)
		self.assertAlmostEqual(res_50["effective_sample_count"], 50.0, places=2)
		self.assertGreater(res_50["calibrated_median_minutes"], 85.0)
		self.assertLessEqual(res_50["calibrated_median_minutes"], 90.0)
		self.assertEqual(res_50["drift_alert"], 1)

	def test_bayesian_recency_half_life_weighting(self):
		"""Recent observations carry 4x the weight of observations 2 half-lives old."""
		half_life = 180
		# 4 old observations (360 days old -> decay 0.25 each -> total weight 1.0) at 60m
		# 4 recent observations (0 days old -> decay 1.0 each -> total weight 4.0) at 120m
		observations = [
			{"allocated_minutes": 60.0, "confidence_score": 1.0, "age_days": 360.0} for _ in range(4)
		] + [
			{"allocated_minutes": 120.0, "confidence_score": 1.0, "age_days": 0.0} for _ in range(4)
		]

		res = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=observations,
			prior_sigma=0.30,
			prior_strength=1.0,
			half_life_days=half_life,
		)
		# Total effective sample weight = 4*0.25 + 4*1.0 = 5.0
		self.assertAlmostEqual(res["effective_sample_count"], 5.0, places=2)
		# Expected log-space mean: (1*ln(60) + 1*ln(60) + 4*ln(120)) / 6 = ln(60) + (4/6)*ln(2)
		expected_min = 60.0 * (2.0 ** (4.0 / 6.0))
		self.assertAlmostEqual(res["calibrated_median_minutes"], expected_min, delta=0.2)

	def test_bayesian_sigma_clamping_bounds(self):
		"""Posterior sigma clamps strictly to [SIGMA_CLAMP_MIN, SIGMA_CLAMP_MAX]."""
		# Zero variance observations with tiny prior -> clamps at 0.10
		identical = [{"allocated_minutes": 60.0, "confidence_score": 1.0, "age_days": 0.0} for _ in range(200)]
		res_low = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=identical,
			prior_sigma=0.10,
			prior_strength=1.0,
		)
		self.assertEqual(res_low["calibrated_sigma"], SIGMA_CLAMP_MIN)

		# Extreme spread observations -> clamps at 0.80
		wild = [
			{"allocated_minutes": 15.0 if i % 2 == 0 else 240.0, "confidence_score": 1.0, "age_days": 0.0}
			for i in range(60)
		]
		res_high = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=wild,
			prior_sigma=0.50,
			prior_strength=1.0,
		)
		self.assertEqual(res_high["calibrated_sigma"], SIGMA_CLAMP_MAX)

	def test_bayesian_drift_alert_requires_min_effective_samples(self):
		"""Drift alert stays 0 when W < 3.0 even under large deviation, and triggers when W >= 3.0."""
		two_obs = [{"allocated_minutes": 150.0, "confidence_score": 1.0, "age_days": 0.0} for _ in range(2)]
		res_small = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=two_obs,
			prior_strength=1.0,
			drift_alert_pct=25.0,
		)
		self.assertEqual(res_small["effective_sample_count"], 2.0)
		self.assertGreater(res_small["frt_realization_ratio"], 1.5)
		self.assertEqual(res_small["drift_alert"], 0)

		four_obs = [{"allocated_minutes": 150.0, "confidence_score": 1.0, "age_days": 0.0} for _ in range(4)]
		res_enough = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=four_obs,
			prior_strength=1.0,
			drift_alert_pct=25.0,
		)
		self.assertEqual(res_enough["effective_sample_count"], 4.0)
		self.assertEqual(res_enough["drift_alert"], 1)

	def test_bayesian_solo_protection_excludes_same_group_bundles(self):
		"""Same-corner bundled observations (same_group_job_count >= 2) are excluded from solo averaging."""
		observations = [
			# Clean solo jobs at 60m
			{"allocated_minutes": 60.0, "confidence_score": 1.0, "age_days": 0.0, "same_group_job_count": 1},
			{"allocated_minutes": 60.0, "confidence_score": 1.0, "age_days": 0.0, "same_group_job_count": 1},
			# Same-corner bundled jobs that look much faster (30m) due to shared teardown
			{"allocated_minutes": 30.0, "confidence_score": 0.5, "age_days": 0.0, "same_group_job_count": 2},
			{"allocated_minutes": 30.0, "confidence_score": 0.5, "age_days": 0.0, "same_group_job_count": 2},
			{"allocated_minutes": 30.0, "confidence_score": 0.5, "age_days": 0.0, "same_group_job_count": 3},
		]
		res = compute_bayesian_package_posterior(
			frt_minutes=60.0,
			observations=observations,
			prior_strength=5.0,
		)
		self.assertEqual(res["effective_sample_count"], 2.0)
		self.assertAlmostEqual(res["calibrated_median_minutes"], 60.0, places=1)

	def test_overlap_group_alpha_learning_and_clamping(self):
		"""Overlap factor alpha_g starts at neutral 1.0 prior, shrinks smoothly, and clamps to [0.35, 1.0]."""
		empty = compute_overlap_group_posterior(buckets=[], overlap_prior_strength=5.0)
		self.assertEqual(empty["calibrated_overlap_factor"], ALPHA_CLAMP_MAX)
		self.assertEqual(empty["bundle_sample_count"], 0)

		# Primary baseline 1.0h, secondary baseline 1.0h, observed bundle 1.6h -> alpha_hat = 0.60
		buckets_5 = [
			{
				"observed_bucket_hours": 1.6,
				"primary_baseline_hours": 1.0,
				"secondary_baseline_hours": 1.0,
				"age_days": 0.0,
			}
			for _ in range(5)
		]
		res_5 = compute_overlap_group_posterior(buckets=buckets_5, overlap_prior_strength=5.0)
		# (5*1.0 + 5*0.6) / 10 = 0.80
		self.assertAlmostEqual(res_5["calibrated_overlap_factor"], 0.80, places=3)
		self.assertEqual(res_5["bundle_sample_count"], 5)

		# Extreme savings (observed = primary -> raw alpha = 0.0) clamps per-bucket to 0.35
		buckets_extreme = [
			{
				"observed_bucket_hours": 1.0,
				"primary_baseline_hours": 1.0,
				"secondary_baseline_hours": 1.0,
				"age_days": 0.0,
			}
			for _ in range(100)
		]
		res_low = compute_overlap_group_posterior(buckets=buckets_extreme, overlap_prior_strength=1.0)
		self.assertGreaterEqual(res_low["calibrated_overlap_factor"], ALPHA_CLAMP_MIN)
		self.assertLess(res_low["calibrated_overlap_factor"], 0.37)

		# Slow bundle (observed > primary + secondary) clamps to 1.0
		buckets_slow = [
			{
				"observed_bucket_hours": 2.8,
				"primary_baseline_hours": 1.0,
				"secondary_baseline_hours": 1.0,
				"age_days": 0.0,
			}
			for _ in range(10)
		]
		res_high = compute_overlap_group_posterior(buckets=buckets_slow, overlap_prior_strength=5.0)
		self.assertEqual(res_high["calibrated_overlap_factor"], ALPHA_CLAMP_MAX)

	def test_alternating_ridge_deconvolution_recovers_bundle_only_packages(self):
		"""Solver accurately recovers true times of packages A and B that NEVER appear solo."""
		# True times: A = 1.80h (FRT 1.0), B = 0.60h (FRT 1.0), C = 1.20h (FRT 1.0)
		# Neither A nor B EVER appears solo — only in varied unrelated bundles!
		true_times = {"PKG-A": 1.80, "PKG-B": 0.60, "PKG-C": 1.20}
		package_frts = {"PKG-A": 1.0, "PKG-B": 1.0, "PKG-C": 1.0}

		bundles = [
			("PKG-A", "PKG-B"),  # 2.40h
			("PKG-A", "PKG-C"),  # 3.00h
			("PKG-B", "PKG-C"),  # 1.80h
			("PKG-A", "PKG-A"),  # 3.60h (e.g. LH + RH opposite sides, independent!)
			("PKG-B", "PKG-B"),  # 1.20h (LH + RH opposite sides, independent!)
		]
		pools = []
		for _rep in range(12):
			for p1, p2 in bundles:
				pools.append(
					{
						"total_hours": true_times[p1] + true_times[p2],
						"age_days": 0.0,
						"items": [
							{"package": p1, "frt_hours": 1.0, "overlap_group": None, "bucket_key": None},
							{"package": p2, "frt_hours": 1.0, "overlap_group": None, "bucket_key": None},
						],
					}
				)

		result = solve_alternating_ridge_deconvolution(
			pools=pools,
			package_frts=package_frts,
			prior_strength=2.0,
			outer_iterations=3,
		)
		pkgs = result["packages"]
		self.assertAlmostEqual(pkgs["PKG-A"]["calibrated_hours"], 1.80, delta=0.10)
		self.assertAlmostEqual(pkgs["PKG-B"]["calibrated_hours"], 0.60, delta=0.10)
		self.assertAlmostEqual(pkgs["PKG-C"]["calibrated_hours"], 1.20, delta=0.10)
		self.assertAlmostEqual(pkgs["PKG-A"]["calibrated_median_minutes"], 108.0, delta=6.0)
		self.assertAlmostEqual(pkgs["PKG-B"]["calibrated_median_minutes"], 36.0, delta=6.0)

	def test_alternating_ridge_deconvolution_learns_same_corner_alpha_and_respects_manual_override(self):
		"""Alternating solver jointly estimates solo mu_j and same-corner alpha_g, honoring manual overrides."""
		# True solo: UCA = 1.5h, Knuckle = 1.0h; same-corner 3101 alpha = 0.60 -> bundle = 1.5 + 0.6*1.0 = 2.1h
		package_frts = {"UCA": 1.5, "KNUCKLE": 1.0}
		pools = []
		for _ in range(15):
			pools.append(
				{
					"total_hours": 1.5,
					"age_days": 0.0,
					"items": [
						{
							"package": "UCA",
							"frt_hours": 1.5,
							"overlap_group": "3101",
							"bucket_key": "3101:LH",
							"is_overlap_primary": 1,
						}
					],
				}
			)
			pools.append(
				{
					"total_hours": 1.0,
					"age_days": 0.0,
					"items": [
						{
							"package": "KNUCKLE",
							"frt_hours": 1.0,
							"overlap_group": "3101",
							"bucket_key": "3101:LH",
							"is_overlap_primary": 1,
						}
					],
				}
			)
			pools.append(
				{
					"total_hours": 2.1,
					"age_days": 0.0,
					"items": [
						{
							"package": "UCA",
							"frt_hours": 1.5,
							"overlap_group": "3101",
							"bucket_key": "3101:LH",
							"is_overlap_primary": 1,
						},
						{
							"package": "KNUCKLE",
							"frt_hours": 1.0,
							"overlap_group": "3101",
							"bucket_key": "3101:LH",
							"is_overlap_primary": 0,
						},
					],
				}
			)

		res = solve_alternating_ridge_deconvolution(
			pools=pools,
			package_frts=package_frts,
			prior_strength=3.0,
			overlap_prior_strength=3.0,
		)
		alpha_3101 = res["overlap_groups"]["3101"]["calibrated_overlap_factor"]
		self.assertLess(alpha_3101, 0.75)
		self.assertGreater(alpha_3101, 0.55)
		self.assertAlmostEqual(res["packages"]["UCA"]["calibrated_hours"], 1.5, delta=0.08)
		self.assertAlmostEqual(res["packages"]["KNUCKLE"]["calibrated_hours"], 1.0, delta=0.08)

		# Manual override must be preserved untouched
		res_manual = solve_alternating_ridge_deconvolution(
			pools=pools,
			package_frts=package_frts,
			manual_alphas={"3101": 0.50},
			prior_strength=3.0,
			overlap_prior_strength=3.0,
		)
		self.assertEqual(res_manual["overlap_groups"]["3101"]["effective_overlap_factor"], 0.50)

	def test_alternating_ridge_deconvolution_enforces_ratio_bounds(self):
		"""Extreme pool totals clamp mu_j strictly to [min_ratio * FRT, max_ratio * FRT]."""
		pools = [
			{
				"total_hours": 20.0,
				"age_days": 0.0,
				"items": [{"package": "SLOW-PKG", "frt_hours": 1.0}],
			}
			for _ in range(30)
		] + [
			{
				"total_hours": 0.05,
				"age_days": 0.0,
				"items": [{"package": "FAST-PKG", "frt_hours": 1.0}],
			}
			for _ in range(30)
		]

		res = solve_alternating_ridge_deconvolution(
			pools=pools,
			package_frts={"SLOW-PKG": 1.0, "FAST-PKG": 1.0},
			prior_strength=1.0,
			min_ratio=0.20,
			max_ratio=4.00,
		)
		self.assertAlmostEqual(res["packages"]["SLOW-PKG"]["calibrated_hours"], 4.00, places=3)
		self.assertAlmostEqual(res["packages"]["FAST-PKG"]["calibrated_hours"], 0.20, places=3)

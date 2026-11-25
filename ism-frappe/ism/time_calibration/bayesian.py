# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Pure-Python log-space Bayesian shrinkage calibration for Service Packages & Overlap Groups."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Sequence

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from ism.scheduling.estimation_light import DEFAULT_SIGMA, get_sigma_for_item
from ism.time_calibration.contracts import (
	ALPHA_CLAMP_MAX,
	ALPHA_CLAMP_MIN,
	DEFAULT_DRIFT_ALERT_PCT,
	DEFAULT_HALF_LIFE_DAYS,
	DEFAULT_OVERLAP_PRIOR_STRENGTH,
	DEFAULT_PRIOR_STRENGTH,
	DRIFT_ALERT_MIN_EFFECTIVE_SAMPLES,
	REVIEW_CLEAN,
	REVIEW_CONFIRMED,
	SIGMA_CLAMP_MAX,
	SIGMA_CLAMP_MIN,
	CalibrationSettings,
	_get_attr,
	get_calibration_settings,
)
from ism.utils.tesla.catalog import get_category_for_correction_code

_SIGMA_KEYWORDS = (
	"tire",
	"brake",
	"suspension",
	"electrical",
	"diagnostic",
	"body",
	"hvac",
	"drivetrain",
)


def resolve_package_prior_sigma(package_doc_or_dict: Any) -> float:
	"""Resolve category prior sigma_0 for a Service Package via estimation_light.get_sigma_for_item."""
	code = str(_get_attr(package_doc_or_dict, "correction_code", "") or "").strip()
	if code:
		category, subcategory = get_category_for_correction_code(code)
		for candidate in (category, subcategory):
			if not candidate:
				continue
			sig = get_sigma_for_item(candidate, default_sigma=DEFAULT_SIGMA)
			if sig != DEFAULT_SIGMA:
				return sig
			low = candidate.lower()
			for kw in _SIGMA_KEYWORDS:
				if kw in low:
					return get_sigma_for_item(kw, default_sigma=DEFAULT_SIGMA)

	pkg_name = str(_get_attr(package_doc_or_dict, "package_name", "") or "").lower()
	for kw in _SIGMA_KEYWORDS:
		if kw in pkg_name:
			return get_sigma_for_item(kw, default_sigma=DEFAULT_SIGMA)

	return DEFAULT_SIGMA


# ---------------------------------------------------------------------------
# Pure Math Functions (Zero DB / Scipy / Numpy Dependencies)
# ---------------------------------------------------------------------------


def compute_bayesian_package_posterior(
	*,
	frt_minutes: float,
	observations: Sequence[dict[str, Any]],
	prior_sigma: float = DEFAULT_SIGMA,
	prior_strength: float = DEFAULT_PRIOR_STRENGTH,
	half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
	drift_alert_pct: float = DEFAULT_DRIFT_ALERT_PCT,
	drift_min_samples: float = DRIFT_ALERT_MIN_EFFECTIVE_SAMPLES,
) -> dict[str, Any]:
	"""Compute log-space Bayesian posterior median, sigma, realization ratio, and drift alert.

	Same-corner bundled observations (`same_group_job_count >= 2`) are excluded to protect
	solo package baselines from teardown overlap drag.
	"""
	frt_min = flt(frt_minutes)
	if frt_min <= 0:
		return {
			"calibrated_median_minutes": 0.0,
			"calibrated_sigma": 0.0,
			"frt_realization_ratio": 0.0,
			"effective_sample_count": 0.0,
			"drift_alert": 0,
		}

	n0 = max(0.0, flt(prior_strength))
	half_life = max(1.0, float(half_life_days))
	sigma_0 = max(SIGMA_CLAMP_MIN, min(SIGMA_CLAMP_MAX, flt(prior_sigma) or DEFAULT_SIGMA))

	weighted_logs: list[tuple[float, float]] = []
	total_weight = 0.0

	for obs in observations or ():
		# Solo protection: never average same-corner bundled observations into solo median
		if cint(obs.get("same_group_job_count", 1)) >= 2:
			continue

		t_min = flt(obs.get("allocated_minutes"))
		if t_min <= 0:
			t_hours = flt(obs.get("allocated_hours"))
			t_min = t_hours * 60.0 if t_hours > 0 else 0.0
		if t_min <= 0:
			continue

		conf = flt(obs.get("confidence_score", 1.0))
		if conf <= 0:
			continue

		age_days = max(0.0, flt(obs.get("age_days", 0.0)))
		decay = 2.0 ** (-age_days / half_life)
		w_r = conf * decay
		if w_r <= 0:
			continue

		weighted_logs.append((w_r, math.log(t_min)))
		total_weight += w_r

	if total_weight <= 0:
		return {
			"calibrated_median_minutes": 0.0,
			"calibrated_sigma": 0.0,
			"frt_realization_ratio": 0.0,
			"effective_sample_count": 0.0,
			"drift_alert": 0,
		}

	log_frt = math.log(frt_min)
	denom = n0 + total_weight
	mu_post = (n0 * log_frt + sum(w * log_t for w, log_t in weighted_logs)) / denom
	calibrated_median_minutes = math.exp(mu_post)

	sq_dev = sum(w * ((log_t - mu_post) ** 2) for w, log_t in weighted_logs)
	var_post = (n0 * (sigma_0**2) + sq_dev) / denom
	sigma_post = math.sqrt(max(0.0, var_post))
	calibrated_sigma = max(SIGMA_CLAMP_MIN, min(SIGMA_CLAMP_MAX, sigma_post))

	ratio = calibrated_median_minutes / frt_min
	drift_alert = 1 if (abs(ratio - 1.0) * 100.0 >= flt(drift_alert_pct) and total_weight >= drift_min_samples) else 0

	return {
		"calibrated_median_minutes": flt(calibrated_median_minutes, 1),
		"calibrated_sigma": flt(calibrated_sigma, 3),
		"frt_realization_ratio": flt(ratio, 2),
		"effective_sample_count": flt(total_weight, 2),
		"drift_alert": drift_alert,
	}


def compute_overlap_group_posterior(
	*,
	buckets: Sequence[dict[str, Any]],
	overlap_prior_strength: float = DEFAULT_OVERLAP_PRIOR_STRENGTH,
	half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, Any]:
	"""Compute shrunk same-corner overlap factor alpha_g in [0.35, 1.0] from clean bundle buckets."""
	m0 = max(0.0, flt(overlap_prior_strength))
	half_life = max(1.0, float(half_life_days))

	weighted_sum = 0.0
	total_weight = 0.0
	valid_count = 0

	for bucket in buckets or ():
		obs_h = flt(bucket.get("observed_bucket_hours"))
		pri_h = flt(bucket.get("primary_baseline_hours"))
		sec_h = flt(bucket.get("secondary_baseline_hours"))
		if obs_h <= 0 or pri_h <= 0 or sec_h <= 0:
			continue

		raw_alpha = (obs_h - pri_h) / sec_h
		alpha_hat = max(ALPHA_CLAMP_MIN, min(ALPHA_CLAMP_MAX, raw_alpha))

		age_days = max(0.0, flt(bucket.get("age_days", 0.0)))
		decay = 2.0 ** (-age_days / half_life)
		if decay <= 0:
			continue

		weighted_sum += decay * alpha_hat
		total_weight += decay
		valid_count += 1

	if total_weight <= 0:
		return {
			"calibrated_overlap_factor": ALPHA_CLAMP_MAX,
			"bundle_sample_count": 0,
			"effective_bundle_weight": 0.0,
		}

	alpha_g = (m0 * ALPHA_CLAMP_MAX + weighted_sum) / (m0 + total_weight)
	alpha_g = max(ALPHA_CLAMP_MIN, min(ALPHA_CLAMP_MAX, alpha_g))

	return {
		"calibrated_overlap_factor": flt(alpha_g, 3),
		"bundle_sample_count": valid_count,
		"effective_bundle_weight": flt(total_weight, 2),
	}


# ---------------------------------------------------------------------------
# Database Recalibration Entrypoints
# ---------------------------------------------------------------------------


def recalibrate_service_package(
	package_name: str,
	now: datetime | str | None = None,
	*,
	settings: CalibrationSettings | None = None,
) -> dict[str, Any]:
	"""Incrementally recalibrate a Service Package from clean solo observations.

	Never modifies `Service Package.standard_labor_hours`.
	"""
	pkg_id = str(package_name or "").strip()
	if not pkg_id or not frappe.db.exists("Service Package", pkg_id):
		return {}

	pkg = frappe.db.get_value(
		"Service Package",
		pkg_id,
		[
			"name",
			"package_name",
			"correction_code",
			"standard_labor_hours",
			"exclude_from_time_calibration",
		],
		as_dict=True,
	)
	if not pkg or cint(pkg.get("exclude_from_time_calibration")) == 1:
		return {}

	frt_hours = flt(pkg.get("standard_labor_hours"))
	if frt_hours <= 0:
		return {}

	cfg = settings or get_calibration_settings()
	ref_now = get_datetime(now) if now else now_datetime()

	obs_rows = frappe.get_all(
		"Service Time Observation",
		filters={
			"service_package": pkg_id,
			"review_status": ["in", [REVIEW_CLEAN, REVIEW_CONFIRMED]],
			"exclude_from_calibration": 0,
			"is_outlier": 0,
			"allocated_hours": [">", 0],
			"same_group_job_count": ["<", 2],
		},
		fields=[
			"allocated_hours",
			"allocated_minutes",
			"confidence_score",
			"observed_at",
			"same_group_job_count",
		],
	)

	obs_inputs: list[dict[str, Any]] = []
	for row in obs_rows:
		obs_dt = get_datetime(row.get("observed_at")) if row.get("observed_at") else ref_now
		age_days = max(0.0, (ref_now - obs_dt).total_seconds() / 86400.0)
		obs_inputs.append(
			{
				"allocated_hours": flt(row.get("allocated_hours")),
				"allocated_minutes": flt(row.get("allocated_minutes")),
				"confidence_score": flt(row.get("confidence_score", 1.0)),
				"same_group_job_count": cint(row.get("same_group_job_count", 1)),
				"age_days": age_days,
			}
		)

	prior_sigma = resolve_package_prior_sigma(pkg)
	posterior = compute_bayesian_package_posterior(
		frt_minutes=frt_hours * 60.0,
		observations=obs_inputs,
		prior_sigma=prior_sigma,
		prior_strength=cfg.prior_strength,
		half_life_days=cfg.half_life_days,
		drift_alert_pct=cfg.drift_alert_pct,
	)

	update_fields = {
		"calibrated_median_minutes": posterior["calibrated_median_minutes"],
		"calibrated_sigma": posterior["calibrated_sigma"],
		"frt_realization_ratio": posterior["frt_realization_ratio"],
		"effective_sample_count": posterior["effective_sample_count"],
		"drift_alert": posterior["drift_alert"],
		"last_calibrated_at": ref_now if posterior["effective_sample_count"] > 0 else None,
	}
	frappe.db.set_value("Service Package", pkg_id, update_fields, update_modified=False)
	return posterior


def recalibrate_overlap_group(
	group_code: str,
	now: datetime | str | None = None,
	*,
	settings: CalibrationSettings | None = None,
) -> dict[str, Any]:
	"""Incrementally recalibrate a Service Overlap Group's alpha_g from clean same-corner bundles."""
	code = str(group_code or "").strip().upper()
	if not code or not frappe.db.exists("Service Overlap Group", code):
		return {}

	cfg = settings or get_calibration_settings()
	ref_now = get_datetime(now) if now else now_datetime()

	rows = frappe.get_all(
		"Service Time Observation",
		filters={
			"overlap_group": code,
			"review_status": ["in", [REVIEW_CLEAN, REVIEW_CONFIRMED]],
			"exclude_from_calibration": 0,
			"is_outlier": 0,
			"allocated_hours": [">", 0],
			"same_group_job_count": [">=", 2],
		},
		fields=[
			"repair_order",
			"overlap_bucket_key",
			"service_package",
			"is_overlap_primary",
			"allocated_hours",
			"frt_hours",
			"allocation_weight",
			"observed_at",
		],
	)

	grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
	pkg_names: set[str] = set()
	for row in rows:
		key = (str(row.get("repair_order") or ""), str(row.get("overlap_bucket_key") or ""))
		if not key[0] or not key[1]:
			continue
		grouped.setdefault(key, []).append(row)
		if row.get("service_package"):
			pkg_names.add(row["service_package"])

	pkg_baselines: dict[str, float] = {}
	if pkg_names:
		pkgs = frappe.get_all(
			"Service Package",
			filters={"name": ["in", sorted(pkg_names)]},
			fields=["name", "calibrated_median_minutes", "standard_labor_hours"],
		)
		for p in pkgs:
			cal_min = flt(p.get("calibrated_median_minutes"))
			frt_h = flt(p.get("standard_labor_hours"))
			pkg_baselines[p["name"]] = (cal_min / 60.0) if cal_min > 0 else frt_h

	def _row_baseline_hours(r: dict[str, Any]) -> float:
		pkg_id = r.get("service_package")
		if pkg_id and flt(pkg_baselines.get(pkg_id)) > 0:
			return flt(pkg_baselines[pkg_id])
		if flt(r.get("frt_hours")) > 0:
			return flt(r["frt_hours"])
		return flt(r.get("allocation_weight"))

	bucket_inputs: list[dict[str, Any]] = []
	for _key, members in grouped.items():
		if len(members) < 2:
			continue

		primaries = [m for m in members if cint(m.get("is_overlap_primary")) == 1]
		if len(primaries) == 1:
			primary_row = primaries[0]
		else:
			primary_row = max(members, key=_row_baseline_hours)

		pri_h = _row_baseline_hours(primary_row)
		sec_h = sum(_row_baseline_hours(m) for m in members if m is not primary_row)
		obs_h = sum(flt(m.get("allocated_hours")) for m in members)

		obs_dts = [get_datetime(m.get("observed_at")) for m in members if m.get("observed_at")]
		latest_dt = max(obs_dts) if obs_dts else ref_now
		age_days = max(0.0, (ref_now - latest_dt).total_seconds() / 86400.0)

		bucket_inputs.append(
			{
				"observed_bucket_hours": obs_h,
				"primary_baseline_hours": pri_h,
				"secondary_baseline_hours": sec_h,
				"age_days": age_days,
			}
		)

	posterior = compute_overlap_group_posterior(
		buckets=bucket_inputs,
		overlap_prior_strength=cfg.overlap_prior_strength,
		half_life_days=cfg.half_life_days,
	)

	manual = flt(frappe.db.get_value("Service Overlap Group", code, "manual_overlap_factor"))
	effective = flt(manual, 3) if manual > 0 else posterior["calibrated_overlap_factor"]

	frappe.db.set_value(
		"Service Overlap Group",
		code,
		{
			"calibrated_overlap_factor": posterior["calibrated_overlap_factor"],
			"effective_overlap_factor": effective,
			"bundle_sample_count": posterior["bundle_sample_count"],
			"effective_bundle_weight": posterior["effective_bundle_weight"],
			"last_calibrated_at": ref_now if posterior["bundle_sample_count"] > 0 else None,
		},
		update_modified=False,
	)

	return {**posterior, "effective_overlap_factor": effective}

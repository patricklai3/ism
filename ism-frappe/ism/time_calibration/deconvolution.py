# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Pure-Python nightly alternating regularized least-squares (ridge) deconvolution solver."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Sequence

import frappe
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

from ism.scheduling.estimation_light import DEFAULT_SIGMA
from ism.time_calibration.bayesian import resolve_package_prior_sigma
from ism.time_calibration.contracts import (
	ALLOC_TECH_SCOPED,
	ALPHA_CLAMP_MAX,
	ALPHA_CLAMP_MIN,
	DEFAULT_DRIFT_ALERT_PCT,
	DEFAULT_HALF_LIFE_DAYS,
	DEFAULT_MAX_RATIO,
	DEFAULT_MIN_RATIO,
	DEFAULT_OVERLAP_PRIOR_STRENGTH,
	DEFAULT_PRIOR_STRENGTH,
	DRIFT_ALERT_MIN_EFFECTIVE_SAMPLES,
	REVIEW_CLEAN,
	REVIEW_CONFIRMED,
	SIGMA_CLAMP_MAX,
	SIGMA_CLAMP_MIN,
	get_calibration_settings,
)


def solve_alternating_ridge_deconvolution(
	*,
	pools: Sequence[dict[str, Any]],
	package_frts: dict[str, float],
	package_prior_sigmas: dict[str, float] | None = None,
	initial_alphas: dict[str, float] | None = None,
	manual_alphas: dict[str, float] | None = None,
	prior_strength: float = DEFAULT_PRIOR_STRENGTH,
	overlap_prior_strength: float = DEFAULT_OVERLAP_PRIOR_STRENGTH,
	half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
	min_ratio: float = DEFAULT_MIN_RATIO,
	max_ratio: float = DEFAULT_MAX_RATIO,
	drift_alert_pct: float = DEFAULT_DRIFT_ALERT_PCT,
	outer_iterations: int = 10,
	inner_sweeps: int = 25,
) -> dict[str, Any]:
	"""Alternating Gauss-Seidel ridge deconvolution solver for bundled RO pools.

	Models each clean RO/tech pool `r` as:
		sum_{b in buckets(r)} (mu_{b,1} + alpha_{g(b)} * sum_{m >= 2} mu_{b,m}) ~= T_r
	with recency decay d_r = 2^(-age_days / H), package ridge penalty lambda_j = prior_strength
	toward FRT_j, and overlap scalar ridge penalty m_0 = overlap_prior_strength toward 1.0.

	Pure Python — zero numpy/scipy dependencies.
	"""
	valid_frts = {str(pkg): flt(h) for pkg, h in (package_frts or {}).items() if flt(h) > 0}
	prior_sigmas = {
		pkg: max(SIGMA_CLAMP_MIN, min(SIGMA_CLAMP_MAX, flt((package_prior_sigmas or {}).get(pkg, DEFAULT_SIGMA))))
		for pkg in valid_frts
	}
	lam = max(1e-6, flt(prior_strength))
	m0 = max(1e-6, flt(overlap_prior_strength))
	half_life = max(1.0, float(half_life_days))
	min_r = max(0.05, flt(min_ratio))
	max_r = max(min_r, flt(max_ratio))

	manual_map = {
		str(g).strip().upper(): flt(v)
		for g, v in (manual_alphas or {}).items()
		if str(g or "").strip() and flt(v) > 0
	}
	alphas: dict[str, float] = {}
	for g, v in (initial_alphas or {}).items():
		code = str(g or "").strip().upper()
		if code:
			alphas[code] = max(ALPHA_CLAMP_MIN, min(ALPHA_CLAMP_MAX, flt(v) or ALPHA_CLAMP_MAX))
	for g, v in manual_map.items():
		alphas[g] = v

	# Normalize and prepare pool structures
	prepared_pools: list[dict[str, Any]] = []
	observed_packages: set[str] = set()
	observed_groups: set[str] = set()

	for pool in pools or ():
		total_h = flt(pool.get("total_hours"))
		raw_items = list(pool.get("items") or [])
		if total_h <= 0 or not raw_items:
			continue

		if pool.get("decay_weight") is not None:
			decay = flt(pool["decay_weight"])
		else:
			age_days = max(0.0, flt(pool.get("age_days", 0.0)))
			decay = 2.0 ** (-age_days / half_life)
		if decay <= 0:
			continue

		# Group items by same-corner bucket_key to determine primary vs secondary roles
		buckets: dict[str, list[int]] = {}
		norm_items: list[dict[str, Any]] = []
		for idx, it in enumerate(raw_items):
			pkg = str(it.get("package") or "").strip() or None
			if pkg and pkg not in valid_frts:
				pkg = None
			if pkg:
				observed_packages.add(pkg)

			grp = str(it.get("overlap_group") or "").strip().upper() or None
			bkey = str(it.get("bucket_key") or "").strip() or None
			if bkey and not grp and ":" in bkey:
				grp = bkey.split(":", 1)[0].strip().upper() or None

			item_frt = valid_frts[pkg] if pkg else flt(it.get("frt_hours"))
			entry = {
				"package": pkg,
				"frt_hours": item_frt,
				"overlap_group": grp,
				"bucket_key": bkey,
				"explicit_primary": it.get("is_overlap_primary"),
				"is_secondary": False,
			}
			norm_items.append(entry)
			if bkey:
				buckets.setdefault(bkey, []).append(idx)

		for bkey, indices in buckets.items():
			if len(indices) < 2:
				continue
			grp = norm_items[indices[0]]["overlap_group"]
			if grp:
				observed_groups.add(grp)
				alphas.setdefault(grp, manual_map.get(grp, ALPHA_CLAMP_MAX))

			explicit_primaries = [i for i in indices if cint(norm_items[i]["explicit_primary"]) == 1]
			if len(explicit_primaries) == 1:
				primary_idx = explicit_primaries[0]
			else:
				primary_idx = max(indices, key=lambda i: (norm_items[i]["frt_hours"], -i))

			for i in indices:
				norm_items[i]["is_secondary"] = i != primary_idx

		prepared_pools.append(
			{
				"total_hours": total_h,
				"decay": decay,
				"items": norm_items,
			}
		)

	if not prepared_pools or not observed_packages:
		return {
			"packages": {},
			"overlap_groups": {},
			"pools_processed": len(prepared_pools),
		}

	active_packages = sorted(observed_packages)
	mu: dict[str, float] = {pkg: valid_frts[pkg] for pkg in active_packages}

	# Build per-package pool index for O(nnz) Gauss-Seidel updates
	pkg_to_pools: dict[str, list[int]] = {pkg: [] for pkg in active_packages}
	for p_idx, pool in enumerate(prepared_pools):
		seen_in_pool: set[str] = set()
		for it in pool["items"]:
			pkg = it["package"]
			if pkg and pkg not in seen_in_pool:
				seen_in_pool.add(pkg)
				pkg_to_pools[pkg].append(p_idx)

	def _build_design_rows() -> tuple[list[dict[str, float]], list[float]]:
		"""Return `(A_rows, y_targets)` for the current `alphas`."""
		a_rows: list[dict[str, float]] = []
		y_targets: list[float] = []
		for pool in prepared_pools:
			coeff_map: dict[str, float] = {}
			adhoc_const = 0.0
			for it in pool["items"]:
				grp = it["overlap_group"]
				c = alphas.get(grp, ALPHA_CLAMP_MAX) if (it["is_secondary"] and grp) else 1.0
				pkg = it["package"]
				if pkg:
					coeff_map[pkg] = coeff_map.get(pkg, 0.0) + c
				else:
					adhoc_const += c * it["frt_hours"]
			a_rows.append(coeff_map)
			y_targets.append(max(0.0, pool["total_hours"] - adhoc_const))
		return (a_rows, y_targets)

	# Alternating passes: Step A (Gauss-Seidel on mu) -> Step B (scalar ridge on alpha_g)
	n_outer = max(1, int(outer_iterations))
	n_inner = max(1, int(inner_sweeps))

	for outer_idx in range(n_outer):
		prev_mu = dict(mu)
		prev_alphas = dict(alphas)

		# Step A: Update mu_j holding alpha_g fixed
		a_rows, y_targets = _build_design_rows()

		# Precompute pool predictions for O(1) incremental residual updates
		pool_preds = [
			sum(coeff * mu[pkg] for pkg, coeff in a_rows[p_idx].items())
			for p_idx in range(len(prepared_pools))
		]

		for _sweep in range(n_inner):
			max_delta = 0.0
			for pkg in active_packages:
				p_indices = pkg_to_pools[pkg]
				if not p_indices:
					continue
				frt_j = valid_frts[pkg]
				cur_mu = mu[pkg]
				h_jj = lam
				rhs = lam * frt_j

				for p_idx in p_indices:
					a_rj = a_rows[p_idx].get(pkg, 0.0)
					if a_rj == 0.0:
						continue
					d_r = prepared_pools[p_idx]["decay"]
					pred_without_j = pool_preds[p_idx] - a_rj * cur_mu
					residual = y_targets[p_idx] - pred_without_j
					h_jj += d_r * a_rj * a_rj
					rhs += d_r * a_rj * residual

				lo = min_r * frt_j
				hi = max_r * frt_j
				new_mu = max(lo, min(hi, rhs / h_jj))
				delta = new_mu - cur_mu
				if abs(delta) > 0.0:
					mu[pkg] = new_mu
					if abs(delta) > max_delta:
						max_delta = abs(delta)
					for p_idx in p_indices:
						a_rj = a_rows[p_idx].get(pkg, 0.0)
						if a_rj != 0.0:
							pool_preds[p_idx] += a_rj * delta

			if max_delta < 1e-6:
				break

		# Step B: Update alpha_g holding mu_j fixed (skip groups with manual overrides)
		for grp in sorted(observed_groups):
			if grp in manual_map:
				alphas[grp] = manual_map[grp]
				continue

			num = m0 * ALPHA_CLAMP_MAX
			den = m0
			for pool in prepared_pools:
				s_rg = 0.0
				other_pred = 0.0
				for it in pool["items"]:
					pkg = it["package"]
					est_h = mu[pkg] if pkg else it["frt_hours"]
					if it["is_secondary"] and it["overlap_group"] == grp:
						s_rg += est_h
					else:
						it_grp = it["overlap_group"]
						c = alphas.get(it_grp, ALPHA_CLAMP_MAX) if (it["is_secondary"] and it_grp) else 1.0
						other_pred += c * est_h

				if s_rg <= 0.0:
					continue
				d_r = pool["decay"]
				e_rg = pool["total_hours"] - other_pred
				num += d_r * s_rg * e_rg
				den += d_r * s_rg * s_rg

			alpha_new = num / den if den > 0 else ALPHA_CLAMP_MAX
			alphas[grp] = max(ALPHA_CLAMP_MIN, min(ALPHA_CLAMP_MAX, alpha_new))

		if outer_idx >= 2:
			max_mu_chg = max((abs(mu[p] - prev_mu[p]) for p in active_packages), default=0.0)
			max_alpha_chg = max(
				(abs(alphas.get(g, ALPHA_CLAMP_MAX) - prev_alphas.get(g, ALPHA_CLAMP_MAX)) for g in observed_groups),
				default=0.0,
			)
			if max_mu_chg < 1e-4 and max_alpha_chg < 1e-4:
				break

	# Compute final residuals, posterior sigmas, effective weights, and drift alerts
	final_a_rows, _final_y = _build_design_rows()
	pool_final_preds: list[float] = []
	for p_idx, pool in enumerate(prepared_pools):
		pred = 0.0
		for it in pool["items"]:
			pkg = it["package"]
			est_h = mu[pkg] if pkg else it["frt_hours"]
			grp = it["overlap_group"]
			c = alphas.get(grp, ALPHA_CLAMP_MAX) if (it["is_secondary"] and grp) else 1.0
			pred += c * est_h
		pool_final_preds.append(pred)

	pkg_results: dict[str, dict[str, Any]] = {}
	for pkg in active_packages:
		frt_j = valid_frts[pkg]
		mu_j = mu[pkg]
		sigma_0 = prior_sigmas[pkg]

		eff_w = 0.0
		sq_log_resid = 0.0
		for p_idx in pkg_to_pools[pkg]:
			pool = prepared_pools[p_idx]
			k_r = max(1, len(pool["items"]))
			w_rj = pool["decay"] * (1.0 / float(k_r))
			eff_w += w_rj

			pred_r = pool_final_preds[p_idx]
			if pred_r > 0 and pool["total_hours"] > 0:
				log_resid = math.log(pool["total_hours"] / pred_r)
				sq_log_resid += w_rj * (log_resid**2)

		var_post = (lam * (sigma_0**2) + sq_log_resid) / (lam + eff_w)
		sigma_post = max(SIGMA_CLAMP_MIN, min(SIGMA_CLAMP_MAX, math.sqrt(max(0.0, var_post))))

		median_min = flt(mu_j * 60.0, 1)
		ratio = flt(mu_j / frt_j, 2)
		drift = 1 if (abs(ratio - 1.0) * 100.0 >= flt(drift_alert_pct) and eff_w >= DRIFT_ALERT_MIN_EFFECTIVE_SAMPLES) else 0

		pkg_results[pkg] = {
			"calibrated_hours": flt(mu_j, 4),
			"calibrated_median_minutes": median_min,
			"calibrated_sigma": flt(sigma_post, 3),
			"frt_realization_ratio": ratio,
			"effective_sample_count": flt(eff_w, 2),
			"drift_alert": drift,
		}

	group_results: dict[str, dict[str, Any]] = {}
	for grp in sorted(observed_groups):
		sample_count = 0
		eff_bundle_weight = 0.0
		for pool in prepared_pools:
			has_sec = any(it["is_secondary"] and it["overlap_group"] == grp for it in pool["items"])
			if has_sec:
				sample_count += 1
				eff_bundle_weight += pool["decay"]
		cal_alpha = flt(alphas.get(grp, ALPHA_CLAMP_MAX), 3)
		eff_alpha = flt(manual_map[grp], 3) if grp in manual_map else cal_alpha
		group_results[grp] = {
			"calibrated_overlap_factor": cal_alpha,
			"effective_overlap_factor": eff_alpha,
			"bundle_sample_count": sample_count,
			"effective_bundle_weight": flt(eff_bundle_weight, 2),
		}

	return {
		"packages": pkg_results,
		"overlap_groups": group_results,
		"pools_processed": len(prepared_pools),
	}


def run_nightly_calibration_job(now: datetime | str | None = None) -> dict[str, Any]:
	"""Nightly scheduled job running alternating ridge deconvolution across clean RO pools."""
	cfg = get_calibration_settings()
	if cfg.is_disabled:
		return {
			"skipped": True,
			"reason": "disabled",
			"pools_processed": 0,
			"packages_updated": 0,
			"groups_updated": 0,
		}

	ref_now = get_datetime(now) if now else now_datetime()
	window_days = max(1, 4 * cfg.half_life_days)
	cutoff_dt = add_to_date(ref_now, days=-window_days)

	obs_rows = frappe.get_all(
		"Service Time Observation",
		filters={
			"review_status": ["in", [REVIEW_CLEAN, REVIEW_CONFIRMED]],
			"exclude_from_calibration": 0,
			"is_outlier": 0,
			"allocated_hours": [">", 0],
			"observed_at": [">=", cutoff_dt],
		},
		fields=[
			"repair_order",
			"technician",
			"allocation_method",
			"service_package",
			"overlap_group",
			"overlap_bucket_key",
			"is_overlap_primary",
			"allocated_hours",
			"frt_hours",
			"observed_at",
		],
	)

	if not obs_rows:
		return {
			"skipped": False,
			"pools_processed": 0,
			"packages_updated": 0,
			"groups_updated": 0,
		}

	# Group observations into clean RO / tech pools
	pool_buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
	pkg_names: set[str] = set()
	group_codes: set[str] = set()

	for row in obs_rows:
		ro_name = str(row.get("repair_order") or "")
		if not ro_name:
			continue
		if row.get("allocation_method") == ALLOC_TECH_SCOPED and row.get("technician"):
			key = (ro_name, str(row["technician"]))
		else:
			key = (ro_name, "__RO__")
		pool_buckets.setdefault(key, []).append(row)

		if row.get("service_package"):
			pkg_names.add(row["service_package"])
		if row.get("overlap_group"):
			group_codes.add(str(row["overlap_group"]).strip().upper())

	package_frts: dict[str, float] = {}
	package_prior_sigmas: dict[str, float] = {}
	if pkg_names:
		pkgs = frappe.get_all(
			"Service Package",
			filters={"name": ["in", sorted(pkg_names)]},
			fields=[
				"name",
				"package_name",
				"correction_code",
				"standard_labor_hours",
				"exclude_from_time_calibration",
			],
		)
		for p in pkgs:
			if cint(p.get("exclude_from_time_calibration")) == 1:
				continue
			frt_h = flt(p.get("standard_labor_hours"))
			if frt_h > 0:
				package_frts[p["name"]] = frt_h
				package_prior_sigmas[p["name"]] = resolve_package_prior_sigma(p)

	initial_alphas: dict[str, float] = {}
	manual_alphas: dict[str, float] = {}
	if group_codes:
		groups = frappe.get_all(
			"Service Overlap Group",
			filters={"name": ["in", sorted(group_codes)]},
			fields=["name", "calibrated_overlap_factor", "manual_overlap_factor"],
		)
		for g in groups:
			code = str(g["name"]).strip().upper()
			cal = flt(g.get("calibrated_overlap_factor"))
			man = flt(g.get("manual_overlap_factor"))
			if cal > 0:
				initial_alphas[code] = cal
			if man > 0:
				manual_alphas[code] = man

	pools: list[dict[str, Any]] = []
	for _key, members in pool_buckets.items():
		total_h = sum(flt(m.get("allocated_hours")) for m in members)
		if total_h <= 0:
			continue
		obs_dts = [get_datetime(m.get("observed_at")) for m in members if m.get("observed_at")]
		latest_dt = max(obs_dts) if obs_dts else ref_now
		age_days = max(0.0, (ref_now - latest_dt).total_seconds() / 86400.0)

		items = [
			{
				"package": m.get("service_package") or None,
				"frt_hours": flt(m.get("frt_hours")),
				"overlap_group": m.get("overlap_group") or None,
				"bucket_key": m.get("overlap_bucket_key") or None,
				"is_overlap_primary": cint(m.get("is_overlap_primary", 1)),
			}
			for m in members
		]
		pools.append(
			{
				"total_hours": total_h,
				"age_days": age_days,
				"items": items,
			}
		)

	solution = solve_alternating_ridge_deconvolution(
		pools=pools,
		package_frts=package_frts,
		package_prior_sigmas=package_prior_sigmas,
		initial_alphas=initial_alphas,
		manual_alphas=manual_alphas,
		prior_strength=cfg.prior_strength,
		overlap_prior_strength=cfg.overlap_prior_strength,
		half_life_days=cfg.half_life_days,
		min_ratio=cfg.min_ratio,
		max_ratio=cfg.max_ratio,
		drift_alert_pct=cfg.drift_alert_pct,
	)

	# Persist Service Package updates (never touching standard_labor_hours!)
	packages_updated = 0
	for pkg_id, stats in solution["packages"].items():
		frappe.db.set_value(
			"Service Package",
			pkg_id,
			{
				"calibrated_median_minutes": stats["calibrated_median_minutes"],
				"calibrated_sigma": stats["calibrated_sigma"],
				"frt_realization_ratio": stats["frt_realization_ratio"],
				"effective_sample_count": stats["effective_sample_count"],
				"drift_alert": stats["drift_alert"],
				"last_calibrated_at": ref_now,
			},
			update_modified=False,
		)
		packages_updated += 1

	# Persist Service Overlap Group updates (preserving manual_overlap_factor)
	groups_updated = 0
	for grp_code, g_stats in solution["overlap_groups"].items():
		if not frappe.db.exists("Service Overlap Group", grp_code):
			continue
		update_vals: dict[str, Any] = {
			"effective_overlap_factor": g_stats["effective_overlap_factor"],
			"bundle_sample_count": g_stats["bundle_sample_count"],
			"effective_bundle_weight": g_stats["effective_bundle_weight"],
			"last_calibrated_at": ref_now,
		}
		if grp_code not in manual_alphas:
			update_vals["calibrated_overlap_factor"] = g_stats["calibrated_overlap_factor"]
		frappe.db.set_value("Service Overlap Group", grp_code, update_vals, update_modified=False)
		groups_updated += 1

	summary = {
		"skipped": False,
		"pools_processed": solution["pools_processed"],
		"packages_updated": packages_updated,
		"groups_updated": groups_updated,
	}
	frappe.logger("ism.time_calibration").info(
		"Nightly calibration deconvolution completed: pools=%s packages=%s groups=%s",
		summary["pools_processed"],
		summary["packages_updated"],
		summary["groups_updated"],
	)
	return summary

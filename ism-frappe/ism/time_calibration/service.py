# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Orchestration entrypoints and document event hooks for ISM Time Calibration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe

from ism.time_calibration.allocator import COMPLETED_RO_STATUSES, recompute_observations_for_ro
from ism.time_calibration.bayesian import recalibrate_overlap_group, recalibrate_service_package
from ism.time_calibration.contracts import get_calibration_settings
from ism.time_calibration.deconvolution import run_nightly_calibration_job

_TRIGGER_RO_STATUSES = (*COMPLETED_RO_STATUSES, "Cancelled")


def _collect_ro_observation_targets(ro_name: str) -> tuple[set[str], set[str]]:
	rows = frappe.get_all(
		"Service Time Observation",
		filters={"repair_order": ro_name},
		fields=["service_package", "overlap_group"],
	)
	packages = {str(r["service_package"]).strip() for r in rows if r.get("service_package")}
	groups = {str(r["overlap_group"]).strip().upper() for r in rows if r.get("overlap_group")}
	return (packages, groups)


def on_repair_order_update(doc: Any, method: str | None = None) -> dict[str, Any]:
	"""Repair Order `on_update` handler: recompute observations and incrementally recalibrate."""
	settings = get_calibration_settings()
	if settings.is_disabled:
		return {"skipped": True, "reason": "disabled"}

	if getattr(doc.flags, "ignore_time_calibration", False) or getattr(
		frappe.flags, "in_time_calibration_recompute", False
	):
		return {"skipped": True, "reason": "recursive_guard"}

	if getattr(doc, "status", None) not in _TRIGGER_RO_STATUSES:
		return {"skipped": True, "reason": "status_not_eligible"}

	ro_name = getattr(doc, "name", None)
	if not ro_name:
		return {"skipped": True, "reason": "missing_name"}

	frappe.flags.in_time_calibration_recompute = True
	try:
		prev_packages, prev_groups = _collect_ro_observation_targets(ro_name)
		obs_names = recompute_observations_for_ro(doc)
		curr_packages, curr_groups = _collect_ro_observation_targets(ro_name)

		affected_packages = sorted(prev_packages | curr_packages)
		affected_groups = sorted(prev_groups | curr_groups)

		for pkg in affected_packages:
			recalibrate_service_package(pkg, settings=settings)
		for grp in affected_groups:
			recalibrate_overlap_group(grp, settings=settings)

		return {
			"skipped": False,
			"observations": obs_names,
			"packages_recalibrated": affected_packages,
			"groups_recalibrated": affected_groups,
		}
	finally:
		frappe.flags.in_time_calibration_recompute = False


def on_observation_update(doc: Any, method: str | None = None) -> dict[str, Any]:
	"""Service Time Observation `on_update` handler: recalibrate affected package and group."""
	settings = get_calibration_settings()
	if settings.is_disabled:
		return {"skipped": True, "reason": "disabled"}

	if getattr(doc.flags, "ignore_calibration_hook", False) or getattr(
		frappe.flags, "in_time_calibration_recompute", False
	):
		return {"skipped": True, "reason": "recursive_guard"}

	frappe.flags.in_time_calibration_recompute = True
	try:
		pkg_result = {}
		grp_result = {}
		pkg = getattr(doc, "service_package", None)
		if pkg:
			pkg_result = recalibrate_service_package(pkg, settings=settings)

		grp = getattr(doc, "overlap_group", None)
		if grp:
			grp_result = recalibrate_overlap_group(grp, settings=settings)

		return {
			"skipped": False,
			"service_package": pkg,
			"overlap_group": grp,
			"package_result": pkg_result,
			"group_result": grp_result,
		}
	finally:
		frappe.flags.in_time_calibration_recompute = False


def recalibrate_all(
	run_deconvolution: bool = True,
	now: datetime | str | None = None,
) -> dict[str, Any]:
	"""Full synchronous recalibration across all observed/calibrated packages and overlap groups."""
	settings = get_calibration_settings()
	if settings.is_disabled:
		return {
			"skipped": True,
			"reason": "disabled",
			"packages_recalibrated": 0,
			"groups_recalibrated": 0,
			"deconvolution": None,
		}

	obs_pkgs = frappe.get_all(
		"Service Time Observation",
		filters={"service_package": ["is", "set"]},
		distinct=True,
		pluck="service_package",
	)
	cal_pkgs = frappe.get_all(
		"Service Package",
		filters={"calibrated_median_minutes": [">", 0]},
		pluck="name",
	)
	all_pkgs = sorted({str(p).strip() for p in (*obs_pkgs, *cal_pkgs) if p})

	obs_grps = frappe.get_all(
		"Service Time Observation",
		filters={"overlap_group": ["is", "set"]},
		distinct=True,
		pluck="overlap_group",
	)
	cal_grps = frappe.get_all(
		"Service Overlap Group",
		filters={"bundle_sample_count": [">", 0]},
		pluck="name",
	)
	all_grps = sorted({str(g).strip().upper() for g in (*obs_grps, *cal_grps) if g})

	for pkg in all_pkgs:
		recalibrate_service_package(pkg, now=now, settings=settings)
	for grp in all_grps:
		recalibrate_overlap_group(grp, now=now, settings=settings)

	deconv = run_nightly_calibration_job(now=now) if run_deconvolution else None

	return {
		"skipped": False,
		"packages_recalibrated": len(all_pkgs),
		"groups_recalibrated": len(all_grps),
		"deconvolution": deconv,
	}

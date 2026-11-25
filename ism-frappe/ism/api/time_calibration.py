# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Manager and Service Advisor APIs for operational time calibration review and reporting."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.permissions import is_manager_or_advisor
from ism.time_calibration.contracts import (
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_STATUSES,
	get_calibration_settings,
)

_MANAGER_ONLY_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager"})


def _require_manager_or_advisor() -> None:
	if not is_manager_or_advisor():
		frappe.throw(
			_("Manager or Service Advisor permission is required to access time calibration."),
			frappe.PermissionError,
		)


def _require_manager_only() -> None:
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(
			_("Manager permission is required to run time calibration."),
			frappe.PermissionError,
		)
	if user == "Administrator":
		return
	roles = set(frappe.get_roles(user) or [])
	if not (roles & _MANAGER_ONLY_ROLES):
		frappe.throw(
			_("Manager permission is required to run time calibration."),
			frappe.PermissionError,
		)


@frappe.whitelist(methods=["GET"])
def get_package_time_accuracy(
	service_package: str | None = None,
	only_drift_alerts: int = 0,
	limit: int = 100,
) -> dict[str, Any]:
	"""Return Service Package FRT vs. calibrated operational time metrics."""
	_require_manager_or_advisor()

	page_limit = max(1, min(500, cint(limit) or 100))
	filters: dict[str, Any] = {}
	if service_package and str(service_package).strip():
		filters["name"] = str(service_package).strip()
	if cint(only_drift_alerts) == 1:
		filters["drift_alert"] = 1

	rows = frappe.get_list(
		"Service Package",
		filters=filters,
		fields=[
			"name",
			"package_name",
			"correction_code",
			"overlap_group",
			"standard_labor_hours",
			"calibrated_median_minutes",
			"calibrated_sigma",
			"frt_realization_ratio",
			"effective_sample_count",
			"drift_alert",
			"exclude_from_time_calibration",
			"last_calibrated_at",
		],
		order_by="drift_alert desc, effective_sample_count desc, modified desc",
		limit=page_limit,
	)

	packages: list[dict[str, Any]] = []
	for row in rows:
		frt_hours = flt(row.get("standard_labor_hours"))
		packages.append(
			{
				"name": row.get("name"),
				"package_name": row.get("package_name"),
				"correction_code": row.get("correction_code"),
				"overlap_group": row.get("overlap_group"),
				"standard_labor_hours": frt_hours,
				"frt_minutes": round(frt_hours * 60.0, 1) if frt_hours > 0 else 0.0,
				"calibrated_median_minutes": flt(row.get("calibrated_median_minutes")),
				"calibrated_sigma": flt(row.get("calibrated_sigma")),
				"frt_realization_ratio": flt(row.get("frt_realization_ratio")),
				"effective_sample_count": flt(row.get("effective_sample_count")),
				"drift_alert": cint(row.get("drift_alert")),
				"exclude_from_time_calibration": cint(row.get("exclude_from_time_calibration")),
				"last_calibrated_at": row.get("last_calibrated_at"),
			}
		)

	settings = get_calibration_settings()
	return {
		"mode": settings.mode,
		"count": len(packages),
		"packages": packages,
	}


@frappe.whitelist(methods=["GET"])
def list_flagged_time_observations(
	repair_order: str | None = None,
	review_status: str = "Needs Review",
	limit: int = 50,
) -> dict[str, Any]:
	"""Return Service Time Observation rows awaiting manager review or filtered by RO/status."""
	_require_manager_or_advisor()

	page_limit = max(1, min(500, cint(limit) or 50))
	clean_status = str(review_status or "").strip()
	if clean_status and clean_status != "All" and clean_status not in REVIEW_STATUSES:
		frappe.throw(
			_("Invalid review_status '{0}'.").format(clean_status),
			frappe.ValidationError,
		)

	filters: dict[str, Any] = {}
	if repair_order and str(repair_order).strip():
		filters["repair_order"] = str(repair_order).strip()
	if clean_status and clean_status != "All":
		filters["review_status"] = clean_status

	rows = frappe.get_list(
		"Service Time Observation",
		filters=filters,
		fields=[
			"name",
			"repair_order",
			"repair_order_service",
			"service_package",
			"service_title",
			"technician",
			"vehicle_model",
			"vehicle_generation",
			"service_position",
			"is_paired_symmetrical",
			"is_supplement",
			"overlap_group",
			"overlap_bucket_key",
			"same_group_job_count",
			"is_overlap_primary",
			"applied_overlap_factor",
			"frt_hours",
			"allocation_weight",
			"allocated_hours",
			"allocated_minutes",
			"ro_net_active_hours",
			"ro_service_count",
			"allocation_method",
			"confidence_score",
			"frt_ratio",
			"review_status",
			"flag_reason",
			"is_outlier",
			"exclude_from_calibration",
			"observed_at",
		],
		order_by="observed_at desc, modified desc",
		limit=page_limit,
	)

	return {
		"count": len(rows),
		"review_status": clean_status or "All",
		"observations": rows,
	}


@frappe.whitelist(methods=["POST"])
def review_time_observation(
	observation: str,
	action: str,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Confirm or exclude a Service Time Observation and trigger incremental recalibration if available."""
	_require_manager_or_advisor()

	obs_name = str(observation or "").strip()
	if not obs_name:
		frappe.throw(_("Observation name is required."), frappe.ValidationError)

	clean_action = str(action or "").strip().lower()
	if clean_action not in ("confirm", "exclude"):
		frappe.throw(
			_("Action must be 'confirm' or 'exclude'."),
			frappe.ValidationError,
		)

	if not frappe.db.exists("Service Time Observation", obs_name):
		frappe.throw(
			_("Service Time Observation {0} does not exist.").format(obs_name),
			frappe.DoesNotExistError,
		)

	doc = frappe.get_doc("Service Time Observation", obs_name)
	doc.check_permission("write")

	if clean_action == "confirm":
		doc.review_status = REVIEW_CONFIRMED
		doc.exclude_from_calibration = 0
	else:
		doc.review_status = REVIEW_EXCLUDED
		doc.exclude_from_calibration = 1

	doc.flags._calibration_hook_ran = False
	doc.save()

	clean_notes = str(notes or "").strip()
	if clean_notes:
		doc.add_comment("Comment", text=clean_notes)

	recalibrated = bool(getattr(doc.flags, "_calibration_hook_ran", False))
	if not recalibrated:
		try:
			from ism.time_calibration.service import on_observation_update

			on_observation_update(doc)
			recalibrated = True
		except ImportError:
			pass

	return {
		"observation": doc.name,
		"action": clean_action,
		"review_status": doc.review_status,
		"exclude_from_calibration": cint(doc.exclude_from_calibration),
		"service_package": doc.service_package,
		"overlap_group": doc.overlap_group,
		"recalibrated": recalibrated,
	}


@frappe.whitelist(methods=["POST"])
def run_time_calibration(
	repair_order: str | None = None,
	run_deconvolution: int = 0,
) -> dict[str, Any]:
	"""Manager-only entrypoint to recompute a single RO's observations or run full calibration."""
	_require_manager_only()

	ro_name = str(repair_order or "").strip()
	if ro_name:
		if not frappe.db.exists("Repair Order", ro_name):
			frappe.throw(
				_("Repair Order {0} does not exist.").format(ro_name),
				frappe.DoesNotExistError,
			)
		try:
			from ism.time_calibration.service import on_repair_order_update
		except ImportError:
			return {
				"status": "unavailable",
				"repair_order": ro_name,
				"message": _("Time calibration service module is not installed."),
			}

		ro_doc = frappe.get_doc("Repair Order", ro_name)
		result = on_repair_order_update(ro_doc)
		return {
			"status": "ok",
			"repair_order": ro_name,
			"result": result,
		}

	use_deconvolution = bool(cint(run_deconvolution))
	try:
		from ism.time_calibration.service import recalibrate_all
	except ImportError:
		if use_deconvolution:
			try:
				from ism.time_calibration.deconvolution import run_nightly_calibration_job

				summary = run_nightly_calibration_job()
				return {
					"status": "ok",
					"run_deconvolution": True,
					"result": summary,
				}
			except ImportError:
				pass
		return {
			"status": "unavailable",
			"run_deconvolution": use_deconvolution,
			"message": _("Time calibration service module is not installed."),
		}

	summary = recalibrate_all(run_deconvolution=use_deconvolution)
	return {
		"status": "ok",
		"run_deconvolution": use_deconvolution,
		"result": summary,
	}

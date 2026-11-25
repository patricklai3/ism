# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Overlap group master helpers and same-corner bucket resolution for Time Calibration."""

from __future__ import annotations

from typing import Any, Sequence

import frappe
from frappe.utils import flt

from ism.time_calibration.contracts import (
	ALPHA_CLAMP_MAX,
	_get_attr,
	derive_overlap_group_info,
)


def ensure_overlap_group(
	group_code: str,
	group_label: str | None = None,
	is_auto_derived: bool = True,
) -> str:
	"""Idempotently get or create a Service Overlap Group record.

	Safe under concurrent creation via duplicate-key fallback.
	Returns the normalized uppercase group_code.
	"""
	code = str(group_code or "").strip().upper()
	if not code:
		raise ValueError("group_code is required")

	if frappe.db.exists("Service Overlap Group", code):
		return code

	label = str(group_label or "").strip()
	if not label:
		_derived_code, derived_label, _auto = derive_overlap_group_info(code, None)
		label = derived_label or code

	doc = frappe.get_doc(
		{
			"doctype": "Service Overlap Group",
			"group_code": code,
			"group_label": label,
			"is_auto_derived": 1 if is_auto_derived else 0,
			"calibrated_overlap_factor": ALPHA_CLAMP_MAX,
			"effective_overlap_factor": ALPHA_CLAMP_MAX,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		if frappe.db.exists("Service Overlap Group", code):
			return code
		raise
	except Exception as exc:
		# Handle MariaDB duplicate key integrity errors under concurrent inserts
		if "Duplicate entry" in str(exc) or "1062" in str(exc):
			if frappe.db.exists("Service Overlap Group", code):
				return code
		raise

	return doc.name


def resolve_service_overlap_bucket(
	service_row: Any,
	pkg_meta: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
	"""Resolve `(overlap_group, overlap_bucket_key)` for a Repair Order Service row.

	- Uses explicit `pkg_meta.overlap_group` override when present, otherwise derives
	  the 4-digit Tesla subcategory code from `correction_code` via `contracts.derive_overlap_group_info`.
	- Partitions strictly by same-corner `(overlap_group, service_position)` ->
	  `f"{group_code}:{pos}"`, so `LH+LH` or `None+None` overlap while `LH+RH` stay independent.
	"""
	explicit_group = _get_attr(pkg_meta, "overlap_group") or _get_attr(service_row, "overlap_group")
	correction_code = _get_attr(service_row, "correction_code") or _get_attr(pkg_meta, "correction_code")

	group_code, _group_label, _is_auto = derive_overlap_group_info(correction_code, explicit_group)
	if not group_code:
		return (None, None)

	pos = str(_get_attr(service_row, "service_position", "None") or "None").strip() or "None"
	return (group_code, f"{group_code}:{pos}")


def get_effective_overlap_factors(group_codes: Sequence[str]) -> dict[str, float]:
	"""Return effective overlap factor (`manual_overlap_factor or calibrated_overlap_factor or 1.0`) per group."""
	normalized: list[str] = []
	seen: set[str] = set()
	for raw in group_codes or ():
		code = str(raw or "").strip().upper()
		if code and code not in seen:
			seen.add(code)
			normalized.append(code)

	factors: dict[str, float] = {code: ALPHA_CLAMP_MAX for code in normalized}
	if not normalized:
		return factors

	rows = frappe.get_all(
		"Service Overlap Group",
		filters={"name": ["in", normalized]},
		fields=[
			"name",
			"manual_overlap_factor",
			"calibrated_overlap_factor",
			"effective_overlap_factor",
		],
	)
	for row in rows:
		code = str(row.get("name") or "").strip().upper()
		if not code:
			continue
		manual = flt(row.get("manual_overlap_factor"))
		calibrated = flt(row.get("calibrated_overlap_factor"))
		if manual > 0:
			eff = manual
		elif calibrated > 0:
			eff = calibrated
		else:
			eff = flt(row.get("effective_overlap_factor")) or ALPHA_CLAMP_MAX

		if eff <= 0 or eff > ALPHA_CLAMP_MAX:
			eff = ALPHA_CLAMP_MAX
		factors[code] = eff

	return factors

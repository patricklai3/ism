# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen cross-wave contracts, constants, and shared helpers for ISM Time Calibration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, TypedDict

import frappe
from frappe.utils import cint, flt

from ism.utils.tesla.catalog import _get_categories_dict, get_category_for_correction_code

# ---------------------------------------------------------------------------
# Frozen Constants
# ---------------------------------------------------------------------------

MODE_DISABLED = "Disabled"
MODE_SHADOW = "Shadow (Observe Only)"
MODE_ACTIVE = "Active"
TIME_CALIBRATION_MODES = (
	MODE_DISABLED,
	MODE_SHADOW,
	MODE_ACTIVE,
)

SIGMA_CLAMP_MIN = 0.10
SIGMA_CLAMP_MAX = 0.80

ALPHA_CLAMP_MIN = 0.35
ALPHA_CLAMP_MAX = 1.00

MANUAL_ALPHA_CLAMP_MIN = 0.25
MANUAL_ALPHA_CLAMP_MAX = 1.00

MIN_VALID_SESSION_HOURS = 5.0 / 60.0

DEFAULT_PRIOR_STRENGTH = 5.0
DEFAULT_OVERLAP_PRIOR_STRENGTH = 5.0
DEFAULT_HALF_LIFE_DAYS = 180
DEFAULT_MIN_RATIO = 0.20
DEFAULT_MAX_RATIO = 4.00
DEFAULT_DRIFT_ALERT_PCT = 25.0
DEFAULT_MAX_SINGLE_SESSION_HOURS = 8.5
DRIFT_ALERT_MIN_EFFECTIVE_SAMPLES = 3.0

PRIOR_STRENGTH_MIN = 0.5
PRIOR_STRENGTH_MAX = 50.0
OVERLAP_PRIOR_STRENGTH_MIN = 0.5
OVERLAP_PRIOR_STRENGTH_MAX = 50.0
HALF_LIFE_DAYS_MIN = 14
HALF_LIFE_DAYS_MAX = 1095
MIN_RATIO_LOWER = 0.05
MIN_RATIO_UPPER = 0.90
MAX_RATIO_LOWER = 1.20
MAX_RATIO_UPPER = 10.0
DRIFT_ALERT_PCT_MIN = 5.0
DRIFT_ALERT_PCT_MAX = 100.0
MAX_SINGLE_SESSION_HOURS_MIN = 1.0
MAX_SINGLE_SESSION_HOURS_MAX = 24.0

ALLOC_SINGLE_JOB = "Single-Job Direct"
ALLOC_TECH_SCOPED = "Tech-Scoped Prorated"
ALLOC_RO_WIDE = "RO-Wide Prorated"
ALLOC_SUPPLEMENT_POST_APPROVAL = "Supplement Post-Approval"
ALLOCATION_METHODS = (
	ALLOC_SINGLE_JOB,
	ALLOC_TECH_SCOPED,
	ALLOC_RO_WIDE,
	ALLOC_SUPPLEMENT_POST_APPROVAL,
)

REVIEW_CLEAN = "Clean"
REVIEW_NEEDS_REVIEW = "Needs Review"
REVIEW_CONFIRMED = "Confirmed"
REVIEW_EXCLUDED = "Excluded"
REVIEW_VOIDED = "Voided"
REVIEW_STATUSES = (
	REVIEW_CLEAN,
	REVIEW_NEEDS_REVIEW,
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_VOIDED,
)

SESSION_OPEN = "Open"
SESSION_CLOSED = "Closed"

DIAGNOSTIC_CORRECTION_PREFIX = "00"


# ---------------------------------------------------------------------------
# Settings Dataclass & Loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationSettings:
	mode: str = MODE_SHADOW
	prior_strength: float = DEFAULT_PRIOR_STRENGTH
	overlap_prior_strength: float = DEFAULT_OVERLAP_PRIOR_STRENGTH
	half_life_days: int = DEFAULT_HALF_LIFE_DAYS
	min_ratio: float = DEFAULT_MIN_RATIO
	max_ratio: float = DEFAULT_MAX_RATIO
	drift_alert_pct: float = DEFAULT_DRIFT_ALERT_PCT
	max_single_session_hours: float = DEFAULT_MAX_SINGLE_SESSION_HOURS

	@property
	def is_disabled(self) -> bool:
		return self.mode == MODE_DISABLED

	@property
	def is_shadow(self) -> bool:
		return self.mode == MODE_SHADOW

	@property
	def is_active(self) -> bool:
		return self.mode == MODE_ACTIVE

	@property
	def is_observing(self) -> bool:
		return self.mode in (MODE_SHADOW, MODE_ACTIVE)


def _get_attr(obj: Any, field: str, default: Any = None) -> Any:
	if obj is None:
		return default
	if isinstance(obj, dict):
		return obj.get(field, default)
	return getattr(obj, field, default)


def _bounded_float(raw: Any, default: float, lo: float, hi: float) -> float:
	if raw is None or raw == "":
		return default
	val = flt(raw)
	if val < lo or val > hi:
		return default
	return val


def _bounded_int(raw: Any, default: int, lo: int, hi: int) -> int:
	if raw is None or raw == "":
		return default
	val = cint(raw)
	if val < lo or val > hi:
		return default
	return val


def get_calibration_settings(settings_doc: Any = None) -> CalibrationSettings:
	"""Read Time Calibration settings from cached ISM Settings with safe defaults."""
	doc = settings_doc
	if doc is None:
		try:
			doc = frappe.get_cached_doc("ISM Settings")
		except Exception:
			try:
				doc = frappe.get_single("ISM Settings")
			except Exception:
				doc = None

	if doc is None:
		return CalibrationSettings()

	raw_mode = str(_get_attr(doc, "time_calibration_mode", "") or "").strip()
	mode = raw_mode if raw_mode in TIME_CALIBRATION_MODES else MODE_SHADOW

	return CalibrationSettings(
		mode=mode,
		prior_strength=_bounded_float(
			_get_attr(doc, "calibration_prior_strength"),
			DEFAULT_PRIOR_STRENGTH,
			PRIOR_STRENGTH_MIN,
			PRIOR_STRENGTH_MAX,
		),
		overlap_prior_strength=_bounded_float(
			_get_attr(doc, "calibration_overlap_prior_strength"),
			DEFAULT_OVERLAP_PRIOR_STRENGTH,
			OVERLAP_PRIOR_STRENGTH_MIN,
			OVERLAP_PRIOR_STRENGTH_MAX,
		),
		half_life_days=_bounded_int(
			_get_attr(doc, "calibration_half_life_days"),
			DEFAULT_HALF_LIFE_DAYS,
			HALF_LIFE_DAYS_MIN,
			HALF_LIFE_DAYS_MAX,
		),
		min_ratio=_bounded_float(
			_get_attr(doc, "calibration_min_ratio"),
			DEFAULT_MIN_RATIO,
			MIN_RATIO_LOWER,
			MIN_RATIO_UPPER,
		),
		max_ratio=_bounded_float(
			_get_attr(doc, "calibration_max_ratio"),
			DEFAULT_MAX_RATIO,
			MAX_RATIO_LOWER,
			MAX_RATIO_UPPER,
		),
		drift_alert_pct=_bounded_float(
			_get_attr(doc, "calibration_drift_alert_pct"),
			DEFAULT_DRIFT_ALERT_PCT,
			DRIFT_ALERT_PCT_MIN,
			DRIFT_ALERT_PCT_MAX,
		),
		max_single_session_hours=_bounded_float(
			_get_attr(doc, "calibration_max_single_session_hours"),
			DEFAULT_MAX_SINGLE_SESSION_HOURS,
			MAX_SINGLE_SESSION_HOURS_MIN,
			MAX_SINGLE_SESSION_HOURS_MAX,
		),
	)


# ---------------------------------------------------------------------------
# Pure Classification & Overlap Group Derivation Helpers
# ---------------------------------------------------------------------------


def is_diagnostic_service(service_row: Any, package_doc: Any = None) -> bool:
	"""Return True if a service row / package should be treated as diagnostic or excluded.

	Checks:
	1. Explicit `exclude_from_time_calibration` flag on package_doc or service_row.
	2. Tesla `00xx` diagnostic/inspection correction code prefix.
	3. `"diagnostic"` substring in service_title or package_name (case-insensitive).
	"""
	if cint(_get_attr(package_doc, "exclude_from_time_calibration", 0)) == 1:
		return True
	if cint(_get_attr(service_row, "exclude_from_time_calibration", 0)) == 1:
		return True

	code = str(
		_get_attr(service_row, "correction_code", "")
		or _get_attr(package_doc, "correction_code", "")
		or ""
	).strip()
	if code.startswith(DIAGNOSTIC_CORRECTION_PREFIX):
		return True

	for candidate in (
		_get_attr(service_row, "service_title", ""),
		_get_attr(service_row, "package_name", ""),
		_get_attr(package_doc, "package_name", ""),
	):
		if candidate and "diagnostic" in str(candidate).lower():
			return True

	return False


def derive_overlap_group_info(
	correction_code: str | None,
	explicit_overlap_group: str | None = None,
) -> tuple[str | None, str | None, bool]:
	"""Derive `(group_code, group_label, is_auto_derived)` for a service/package.

	- Explicit `explicit_overlap_group` takes precedence (`is_auto_derived=False`).
	- Otherwise derives the 4-digit Tesla subcategory prefix from `correction_code`
	  using `tesla_service_categories.json` (`is_auto_derived=True`).
	- Returns `(None, None, True)` when neither an explicit group nor a valid 4-char code exists.
	"""
	explicit = str(explicit_overlap_group or "").strip().upper()
	if explicit:
		subcats = (_get_categories_dict() or {}).get("subcategories", {})
		label = subcats.get(explicit) or explicit
		return (explicit, label, False)

	code = str(correction_code or "").strip()
	if len(code) < 4:
		return (None, None, True)

	prefix4 = code[:4].upper()
	category, subcategory = get_category_for_correction_code(code)
	if subcategory:
		return (prefix4, subcategory, True)

	if prefix4.isdigit():
		label = f"{category} ({prefix4})" if category else prefix4
		return (prefix4, label, True)

	return (None, None, True)


# ---------------------------------------------------------------------------
# Cross-Worker TypedDicts & Protocols (Frozen Wave 1 -> Wave 2 Boundaries)
# ---------------------------------------------------------------------------


class CalibratedPackageParams(TypedDict):
	median_minutes: float
	sigma: float
	is_calibrated: bool
	overlap_group: str | None


class OperationWithMeta(TypedDict, total=False):
	flat_rate_minutes: float
	sigma: float
	service_package: str | None
	correction_code: str | None
	overlap_group: str | None
	service_position: str | None
	applied_overlap_factor: float


class RecomputeROObservationsFn(Protocol):
	def __call__(self, ro_or_name: Any) -> list[str]: ...


class ResolvePackageCalibratedParamsFn(Protocol):
	def __call__(
		self,
		package_doc: Any,
		*,
		default_sigma: float,
		settings: CalibrationSettings | None = None,
	) -> CalibratedPackageParams: ...


class ApplyOverlapDiscountsFn(Protocol):
	def __call__(
		self,
		operations: Sequence[OperationWithMeta],
		*,
		overlap_factors: dict[str, float] | None = None,
	) -> list[dict[str, Any]]: ...


def recompute_ro_observations(ro_or_name: Any) -> list[str]:
	"""Late-binding entrypoint to Wave 2B `allocator.recompute_observations_for_ro`."""
	from ism.time_calibration.allocator import recompute_observations_for_ro

	return recompute_observations_for_ro(ro_or_name)


def resolve_package_calibrated_params(
	package_doc: Any,
	*,
	default_sigma: float,
	settings: CalibrationSettings | None = None,
) -> CalibratedPackageParams:
	"""Resolve operational `(median_minutes, sigma, is_calibrated, overlap_group)` for a Service Package.

	Returns calibrated metrics only when `settings.is_active`, `calibrated_median_minutes > 0`,
	and `not exclude_from_time_calibration`.
	"""
	cfg = settings or get_calibration_settings()
	explicit_group = _get_attr(package_doc, "overlap_group")
	correction_code = _get_attr(package_doc, "correction_code")
	group_code, _, _ = derive_overlap_group_info(correction_code, explicit_group)

	frt_hours = flt(_get_attr(package_doc, "standard_labor_hours", 0))
	frt_minutes = frt_hours * 60.0 if frt_hours > 0 else 0.0

	if (
		cfg.is_active
		and cint(_get_attr(package_doc, "exclude_from_time_calibration", 0)) != 1
		and flt(_get_attr(package_doc, "calibrated_median_minutes", 0)) > 0
	):
		cal_median = flt(_get_attr(package_doc, "calibrated_median_minutes", 0))
		cal_sigma = flt(_get_attr(package_doc, "calibrated_sigma", 0))
		sigma = cal_sigma if SIGMA_CLAMP_MIN <= cal_sigma <= SIGMA_CLAMP_MAX else default_sigma
		return CalibratedPackageParams(
			median_minutes=cal_median,
			sigma=sigma,
			is_calibrated=True,
			overlap_group=group_code,
		)

	return CalibratedPackageParams(
		median_minutes=frt_minutes,
		sigma=default_sigma,
		is_calibrated=False,
		overlap_group=group_code,
	)


def apply_overlap_discounts_to_operations(
	operations: Sequence[OperationWithMeta],
	*,
	overlap_factors: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
	"""Apply same-corner `(overlap_group, service_position)` overlap discounts to operations.

	Within each bucket of size >= 2 sharing the same `(overlap_group, service_position)`,
	the longest operation retains `1.0 * flat_rate_minutes` and every additional operation
	is multiplied by `effective_overlap_factor` (default `1.0`). Operations without an
	`overlap_group` are left untouched.
	"""
	if not operations:
		return []

	result = [dict(op) for op in operations]
	buckets: dict[tuple[str, str], list[int]] = {}
	group_codes: set[str] = set()

	for idx, op in enumerate(result):
		raw_group = op.get("overlap_group")
		if not raw_group and op.get("correction_code"):
			raw_group, _, _ = derive_overlap_group_info(op.get("correction_code"), None)
		group = str(raw_group or "").strip().upper()
		if not group:
			continue
		pos = str(op.get("service_position") or "None").strip() or "None"
		buckets.setdefault((group, pos), []).append(idx)
		group_codes.add(group)

	factors: dict[str, float] = {}
	if overlap_factors is not None:
		factors = {str(k).strip().upper(): flt(v) for k, v in overlap_factors.items()}
	elif group_codes:
		try:
			from ism.time_calibration.overlap import get_effective_overlap_factors

			factors = get_effective_overlap_factors(sorted(group_codes))
		except Exception:
			rows = frappe.get_all(
				"Service Overlap Group",
				filters={"name": ["in", sorted(group_codes)]},
				fields=["name", "effective_overlap_factor", "manual_overlap_factor", "calibrated_overlap_factor"],
			)
			for row in rows:
				eff = flt(row.get("effective_overlap_factor"))
				if eff <= 0:
					eff = flt(row.get("manual_overlap_factor")) or flt(row.get("calibrated_overlap_factor")) or 1.0
				factors[str(row.get("name") or "").strip().upper()] = eff

	for (group, _pos), indices in buckets.items():
		if len(indices) < 2:
			result[indices[0]]["applied_overlap_factor"] = 1.0
			continue

		alpha = flt(factors.get(group, 1.0))
		if alpha <= 0 or alpha > ALPHA_CLAMP_MAX:
			alpha = 1.0

		# Longest job in bucket is primary (1.0x); tie-break by original order.
		primary_idx = max(indices, key=lambda i: (flt(result[i].get("flat_rate_minutes", 0)), -i))
		for i in indices:
			if i == primary_idx:
				result[i]["applied_overlap_factor"] = 1.0
			else:
				result[i]["applied_overlap_factor"] = alpha
				result[i]["flat_rate_minutes"] = flt(result[i].get("flat_rate_minutes", 0)) * alpha

	return result

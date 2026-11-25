# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from ism.time_calibration.contracts import (
	ALPHA_CLAMP_MAX,
	MANUAL_ALPHA_CLAMP_MAX,
	MANUAL_ALPHA_CLAMP_MIN,
)


class ServiceOverlapGroup(Document):
	def before_naming(self):
		self._normalize_fields()

	def validate(self):
		self._normalize_fields()
		self._validate_and_compute_overlap_factor()

	def _normalize_fields(self):
		if self.group_code:
			self.group_code = str(self.group_code).strip().upper()
		if self.group_label:
			self.group_label = str(self.group_label).strip()
		elif self.group_code:
			self.group_label = self.group_code

	def _validate_and_compute_overlap_factor(self):
		calibrated = flt(self.calibrated_overlap_factor)
		if calibrated <= 0:
			calibrated = ALPHA_CLAMP_MAX
			self.calibrated_overlap_factor = calibrated

		manual = flt(self.manual_overlap_factor)
		if self.manual_overlap_factor is not None and manual != 0:
			if manual < MANUAL_ALPHA_CLAMP_MIN or manual > MANUAL_ALPHA_CLAMP_MAX:
				frappe.throw(
					_(
						"Manual Overlap Factor must be between {0:.2f} and {1:.2f} (or 0 to use calibrated factor)."
					).format(MANUAL_ALPHA_CLAMP_MIN, MANUAL_ALPHA_CLAMP_MAX),
					frappe.ValidationError,
				)
			self.effective_overlap_factor = flt(manual, 3)
		else:
			self.effective_overlap_factor = flt(calibrated, 3)

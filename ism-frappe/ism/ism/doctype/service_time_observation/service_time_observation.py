# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint

from ism.time_calibration.contracts import (
	REVIEW_CLEAN,
	REVIEW_CONFIRMED,
	REVIEW_EXCLUDED,
	REVIEW_NEEDS_REVIEW,
	REVIEW_VOIDED,
)


class ServiceTimeObservation(Document):
	def validate(self):
		self._sync_review_status_and_exclusion()

	def _sync_review_status_and_exclusion(self):
		status = (self.review_status or "").strip() or REVIEW_CLEAN
		self.review_status = status

		excluded = cint(self.exclude_from_calibration)
		is_new = self.is_new()
		status_changed = False if is_new else bool(self.has_value_changed("review_status"))
		exclude_changed = False if is_new else bool(self.has_value_changed("exclude_from_calibration"))

		if status == REVIEW_VOIDED:
			self.exclude_from_calibration = 1
			return

		if status == REVIEW_CONFIRMED:
			if exclude_changed and excluded == 1 and not status_changed:
				self.review_status = REVIEW_EXCLUDED
				self.exclude_from_calibration = 1
			else:
				self.exclude_from_calibration = 0
			return

		if status == REVIEW_EXCLUDED:
			if exclude_changed and excluded == 0 and not status_changed:
				self.review_status = REVIEW_CONFIRMED if self.flag_reason else REVIEW_CLEAN
				self.exclude_from_calibration = 0
			else:
				self.exclude_from_calibration = 1
			return

		if status == REVIEW_NEEDS_REVIEW:
			if exclude_changed and excluded == 1 and not status_changed:
				self.review_status = REVIEW_EXCLUDED
				self.exclude_from_calibration = 1
			return

		# Clean (or fallback)
		if excluded == 1:
			self.review_status = REVIEW_EXCLUDED
			self.exclude_from_calibration = 1
		else:
			self.exclude_from_calibration = 0

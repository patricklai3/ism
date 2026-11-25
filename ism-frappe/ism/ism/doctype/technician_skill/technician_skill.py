# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class TechnicianSkill(Document):
	def validate(self):
		if self.skill_name:
			self.skill_name = self.skill_name.strip()
		if not self.skill_name:
			frappe.throw(_("Skill Name cannot be empty."), title=_("Validation Error"))

		if self.scarcity_weight is None or self.scarcity_weight == "":
			self.scarcity_weight = 1.0
		elif flt(self.scarcity_weight) <= 0:
			frappe.throw(
				_("Scarcity Weight must be greater than 0."),
				title=_("Validation Error"),
			)

	def on_trash(self):
		refs: list[str] = []

		if frappe.db.exists("DocType", "Technician Skill Competency"):
			profiles = frappe.get_all(
				"Technician Skill Competency",
				filters={"skill": self.name, "parenttype": "Technician Profile"},
				pluck="parent",
			)
			if profiles:
				refs.append(
					_("Technician Profile(s): {0}").format(", ".join(sorted(set(profiles))))
				)

		if frappe.db.exists("DocType", "Service Package Skill"):
			packages = frappe.get_all(
				"Service Package Skill",
				filters={"skill": self.name, "parenttype": "Service Package"},
				pluck="parent",
			)
			if packages:
				refs.append(
					_("Service Package(s): {0}").format(", ".join(sorted(set(packages))))
				)

		if frappe.db.exists("DocType", "Schedule Entry Type") and frappe.db.has_column(
			"Schedule Entry Type", "default_skill"
		):
			entry_types = frappe.get_all(
				"Schedule Entry Type",
				filters={"default_skill": self.name},
				pluck="name",
			)
			if entry_types:
				refs.append(
					_("Schedule Entry Type(s): {0}").format(", ".join(sorted(set(entry_types))))
				)

		if frappe.db.exists("DocType", "Repair Order Service") and frappe.db.has_column(
			"Repair Order Service", "required_skill"
		):
			ros = frappe.get_all(
				"Repair Order Service",
				filters={"required_skill": self.name, "parenttype": "Repair Order"},
				pluck="parent",
				limit=10,
			)
			if ros:
				refs.append(
					_("Repair Order(s): {0}").format(", ".join(sorted(set(ros))))
				)

		if refs:
			frappe.throw(
				_(
					"Cannot delete Technician Skill '{0}' because it is referenced by {1}. "
					"Remove all references before deleting."
				).format(self.name, "; ".join(refs)),
				title=_("Skill in Use"),
			)

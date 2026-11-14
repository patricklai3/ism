"""High-level Python client for Frappe sites running ISM."""

from __future__ import annotations

from typing import Any, Optional

from .config import IsmConfig
from .transport import FrappeTransport, IsmApiError


class IsmAgentClient:
	"""High-level client wrapping ISM Agent Facade and Frappe REST APIs."""

	def __init__(self, config: Optional[IsmConfig] = None) -> None:
		self.config = config or IsmConfig.from_env()
		self._tx = FrappeTransport(self.config)

	# --- Curated ISM Agent Facade Methods ---

	def create_quote(
		self,
		customer_name: Optional[str] = None,
		vehicle_identifier: Optional[str] = None,
		services: Optional[list[Any] | str] = None,
		parts: Optional[list[Any] | str] = None,
		notes: Optional[str] = None,
		customer_phone: Optional[str] = None,
		customer_email: Optional[str] = None,
		company: Optional[str] = None,
		odometer_in: Optional[int] = None,
		include_summary: bool = False,
		customer: Optional[str] = None,
		vehicle: Optional[str] = None,
		concern: Optional[str] = None,
	) -> dict[str, Any]:
		"""Create a Draft Estimate Repair Order with deterministic totals."""
		args = _drop_none({
			"customer_name": customer_name,
			"customer": customer,
			"vehicle_identifier": vehicle_identifier,
			"vehicle": vehicle,
			"company": company or self.config.company or None,
			"services": services,
			"parts": parts,
			"notes": notes,
			"concern": concern,
			"customer_phone": customer_phone,
			"customer_email": customer_email,
			"odometer_in": odometer_in,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.create_quote", args)

	def amend_quote(
		self,
		repair_order: str,
		add_services: Optional[list[Any] | str] = None,
		remove_service_indices: Optional[list[int] | str] = None,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Amend services or notes on an unapproved Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"add_services": add_services,
			"remove_service_indices": remove_service_indices,
			"notes": notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.amend_quote", args)

	def find_available_slots(
		self,
		repair_order: Optional[str] = None,
		duration_minutes: Optional[int] = None,
		start_date: Optional[str] = None,
		days_ahead: int = 3,
		equipment_tags: Optional[list[str] | str] = None,
		include_resources: bool = False,
		max_slots: Optional[int] = None,
		include_past: bool = False,
		min_start_time: Optional[str] = None,
		include_summary: bool = False,
		target_date: Optional[str] = None,
	) -> dict[str, Any]:
		"""Query dual-resource capacity for available bay and tech slots."""
		args = _drop_none({
			"repair_order": repair_order,
			"duration_minutes": duration_minutes,
			"start_date": start_date,
			"target_date": target_date,
			"days_ahead": days_ahead,
			"equipment_tags": equipment_tags,
			"include_resources": True if include_resources else None,
			"max_slots": max_slots,
			"include_past": True if include_past else None,
			"min_start_time": min_start_time,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.find_available_slots", args)

	def reschedule_appointment(
		self,
		schedule_entry: str,
		scheduled_date: Optional[str] = None,
		start_time: Optional[str] = None,
		duration_minutes: Optional[int] = None,
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Reschedule an existing Schedule Entry with bay and technician checks."""
		args = _drop_none({
			"schedule_entry": schedule_entry,
			"scheduled_date": scheduled_date,
			"start_time": start_time,
			"duration_minutes": duration_minutes,
			"service_bay": service_bay,
			"technician": technician,
			"notes": notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.reschedule_appointment", args)

	def update_appointment_status(
		self,
		schedule_entry: str,
		status: str,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Transition a Schedule Entry status (Confirmed, No Show, Cancelled, etc.)."""
		args = _drop_none({
			"schedule_entry": schedule_entry,
			"status": status,
			"notes": notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.update_appointment_status", args)

	def create_ro_from_appointment(
		self,
		schedule_entry: str,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Create a Checked In Repair Order from a pre-intake Schedule Entry."""
		args = _drop_none({
			"schedule_entry": schedule_entry,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.create_ro_from_appointment", args)

	def estimate_duration(
		self,
		repair_order: Optional[str] = None,
		operations: Optional[list[Any] | str] = None,
		percentile: Optional[float] = None,
		sigma: Optional[float] = None,
		buffer_minutes: Optional[int] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Estimate probabilistic shop duration for a Repair Order or operations list."""
		args = _drop_none({
			"repair_order": repair_order,
			"operations": operations,
			"percentile": percentile,
			"sigma": sigma,
			"buffer_minutes": buffer_minutes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.estimate_duration", args)

	def book_appointment(
		self,
		repair_order: Optional[str] = None,
		scheduled_date: str = "",
		start_time: str = "",
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
		entry_type: Optional[str] = None,
		notes: Optional[str] = None,
		estimated_duration: Optional[int] = None,
		provisional_customer_name: Optional[str] = None,
		provisional_vehicle_info: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Allocate a linked or provisional Schedule Entry and push realtime calendar update."""
		args = _drop_none({
			"repair_order": repair_order,
			"scheduled_date": scheduled_date,
			"start_time": start_time,
			"service_bay": service_bay,
			"technician": technician,
			"entry_type": entry_type,
			"notes": notes,
			"estimated_duration": estimated_duration,
			"provisional_customer_name": provisional_customer_name,
			"provisional_vehicle_info": provisional_vehicle_info,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.book_appointment", args)

	def send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: Optional[str] = None,
		include_summary: bool = False,
		recipient: Optional[str] = None,
	) -> dict[str, Any]:
		"""Generate approval token and optionally send SMS/email link."""
		args = _drop_none({
			"repair_order": repair_order,
			"communication_medium": communication_medium,
			"phone_number": phone_number,
			"recipient": recipient,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.send_quote_for_approval", args)

	def import_supplier_catalog(
		self,
		items: Optional[list[dict[str, Any]] | dict[str, Any] | str] = None,
		raw_text: Optional[str] = None,
		supplier: Optional[str] = None,
		default_source: str = "OEM",
		default_condition: str = "New",
		service_package: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Bulk ingest structured items or delimited supplier catalog text into Item master."""
		args = _drop_none({
			"items": items,
			"raw_text": raw_text,
			"supplier": supplier,
			"default_source": default_source,
			"default_condition": default_condition,
			"service_package": service_package,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.import_supplier_catalog", args)

	def get_component_alternates(
		self,
		service_package: Optional[str] = None,
		component_slot: Optional[str] = None,
		slot_number: Optional[int] = None,
		oem_part_number: Optional[str] = None,
		item_code: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Find compatible OEM, aftermarket, or used alternate parts with stock info."""
		args = _drop_none({
			"service_package": service_package,
			"component_slot": component_slot,
			"slot_number": slot_number,
			"oem_part_number": oem_part_number,
			"item_code": item_code,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_component_alternates", args)

	def import_tesla_procedure(
		self,
		procedure_title: Optional[str] = None,
		correction_code: Optional[str] = None,
		labor_hours: float = 1.0,
		model: str = "Model 3",
		parts: Optional[list[Any] | str] = None,
		repair_order: Optional[str] = None,
		url: Optional[str] = None,
		data: Optional[dict[str, Any] | str] = None,
		on_conflict: str = "skip",
		force: bool = False,
		side: Optional[str] = None,
		is_supplement: int = 0,
		advisor_notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Save a Tesla service manual procedure as a Service Package or import onto a Repair Order."""
		args = _drop_none({
			"procedure_title": procedure_title,
			"correction_code": correction_code,
			"labor_hours": labor_hours,
			"model": model,
			"parts": parts,
			"repair_order": repair_order,
			"url": url,
			"data": data,
			"on_conflict": on_conflict,
			"force": True if force else None,
			"side": side,
			"is_supplement": 1 if is_supplement else None,
			"advisor_notes": advisor_notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.import_tesla_procedure", args)

	def get_shop_floor_status(self, date: Optional[str] = None, include_summary: bool = False) -> dict[str, Any]:
		"""Retrieve active bays, on-duty technicians, appointments, and ROs."""
		return self.call_method(
			"ism.api.agent.get_shop_floor_status",
			_drop_none({"date": date, "include_summary": True if include_summary else None}),
		)

	def update_job_status(
		self,
		repair_order: str,
		action: str,
		odometer_out: Optional[int] = None,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Transition Repair Order status (start, hold, complete) with guardrails."""
		args = _drop_none({
			"repair_order": repair_order,
			"action": action,
			"odometer_out": odometer_out,
			"notes": notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.update_job_status", args)

	# --- Extended ISM Agent Facade Methods ---

	def get_parts_stock(
		self,
		ro_name: Optional[str] = None,
		company: Optional[str] = None,
		include_summary: bool = False,
		repair_order: Optional[str] = None,
		items: Optional[list[Any] | str] = None,
	) -> dict[str, Any]:
		args = _drop_none({
			"repair_order": repair_order or ro_name,
			"items": items,
			"company": company or self.config.company or None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.check_parts_stock", args)

	def check_parts_stock(
		self,
		repair_order: Optional[str] = None,
		items: Optional[list[Any] | str] = None,
		company: Optional[str] = None,
		include_summary: bool = False,
		ro_name: Optional[str] = None,
	) -> dict[str, Any]:
		"""Canonical alias for get_parts_stock matching ism.api.agent.check_parts_stock."""
		return self.get_parts_stock(
			ro_name=ro_name,
			company=company,
			include_summary=include_summary,
			repair_order=repair_order,
			items=items,
		)

	def submit_procurement_orders(
		self,
		repair_order: str,
		parts_data: list[dict[str, Any]],
		submit: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		args = _drop_none({
			"repair_order": repair_order,
			"parts_data": parts_data,
			"submit": True if submit else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.submit_procurement_orders", args)

	def fetch_tesla_procedure(
		self,
		url: str,
		repair_order: Optional[str] = None,
		vehicle: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch and parse a Tesla service manual URL with optional vehicle compatibility check."""
		args = _drop_none({
			"url": url,
			"repair_order": repair_order,
			"vehicle": vehicle,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.fetch_tesla_manual", args)

	def fetch_tesla_manual(
		self,
		url: str,
		repair_order: Optional[str] = None,
		vehicle: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Canonical alias for fetch_tesla_procedure matching ism.api.agent.fetch_tesla_manual."""
		return self.fetch_tesla_procedure(
			url=url,
			repair_order=repair_order,
			vehicle=vehicle,
			include_summary=include_summary,
		)

	def decode_vehicle_vin(self, vin: str, include_summary: bool = False) -> dict[str, Any]:
		"""Decode a 17-character Tesla VIN and resolve recommended service manual URL."""
		args = _drop_none({"vin": vin, "include_summary": True if include_summary else None})
		return self.call_method("ism.api.agent.decode_vehicle_vin", args)

	def search_vehicles(
		self,
		query: Optional[str] = None,
		customer: Optional[str] = None,
		model: Optional[str] = None,
		limit: int = 20,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Search Repair Vehicles by VIN, license plate, customer, or model."""
		args = _drop_none({
			"query": query,
			"customer": customer,
			"model": model,
			"limit": limit,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.search_vehicles", args)

	def get_vehicle_intelligence(
		self,
		vehicle: str,
		history_limit: int = 20,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Retrieve VIN decode, owners, service history, and open deferred services for a vehicle."""
		args = _drop_none({
			"vehicle": vehicle,
			"history_limit": history_limit,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_vehicle_intelligence", args)

	def link_vehicle_customer(
		self,
		vehicle: str,
		customer: str,
		is_primary: bool = False,
		relationship: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Link a Customer as primary owner or additional contact on a Repair Vehicle."""
		args = _drop_none({
			"vehicle": vehicle,
			"customer": customer,
			"is_primary": True if is_primary else None,
			"relationship": relationship,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.link_vehicle_customer", args)

	def list_inspection_templates(
		self,
		include_inactive: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""List available check-in Inspection Templates."""
		args = _drop_none({
			"include_inactive": True if include_inactive else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.list_inspection_templates", args)

	def get_inspection_template(
		self,
		template_name: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch a check-in Inspection Template with its checklist items."""
		args = _drop_none({
			"template_name": template_name,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_inspection_template", args)

	def apply_inspection_template(
		self,
		repair_order: str,
		template_name: Optional[str] = None,
		replace_existing: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Populate check-in inspection items on a Repair Order from a template."""
		args = _drop_none({
			"repair_order": repair_order,
			"template_name": template_name,
			"replace_existing": True if replace_existing else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.apply_inspection_template", args)

	def get_check_in_inspection(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Retrieve the check-in inspection items and summary counts for a Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_check_in_inspection", args)

	def record_check_in_inspection(
		self,
		repair_order: str,
		items: Optional[list[Any] | str] = None,
		odometer_in: Optional[int | float] = None,
		odometer: Optional[int | float] = None,
		customer_concern: Optional[str] = None,
		template_name: Optional[str] = None,
		auto_apply_template: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Record check-in walkaround item results, odometer reading, and customer concern."""
		args = _drop_none({
			"repair_order": repair_order,
			"items": items,
			"odometer_in": odometer_in,
			"odometer": odometer,
			"customer_concern": customer_concern,
			"template_name": template_name,
			"auto_apply_template": True if auto_apply_template else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.record_check_in_inspection", args)

	def get_daily_tech_overview(
		self,
		date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		args = _drop_none({"date": date, "include_summary": True if include_summary else None})
		return self.call_method("ism.api.agent.get_tech_overview", args)

	def get_tech_overview(
		self,
		date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Canonical alias for get_daily_tech_overview matching ism.api.agent.get_tech_overview."""
		return self.get_daily_tech_overview(date=date, include_summary=include_summary)

	def get_unscheduled_services(self, repair_order: str, include_summary: bool = False) -> dict[str, Any]:
		args = _drop_none({
			"repair_order": repair_order,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_unscheduled_services", args)

	def receive_purchase_order(
		self,
		purchase_order: str,
		submit_po: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Submit (when confirmed) and receive a Purchase Order via Purchase Receipt."""
		args = _drop_none({
			"purchase_order": purchase_order,
			"submit_po": True if submit_po else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.receive_purchase_order", args)

	def checkout_repair_order(
		self,
		repair_order: str,
		payment_method: str = "Debit / Interac",
		paid_amount: Optional[float] = None,
		odometer_out: Optional[int] = None,
		confirm: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Invoice, collect payment, complete schedule entries, and release a Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"payment_method": payment_method,
			"paid_amount": paid_amount,
			"odometer_out": odometer_out,
			"confirm": True if confirm else None,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.checkout_repair_order", args)

	def toggle_service_completion(
		self,
		repair_order: str,
		service_row_id: Optional[str | list[str]] = None,
		is_completed: bool = True,
		include_summary: bool = False,
		service_row_ids: Optional[str | list[str]] = None,
	) -> dict[str, Any]:
		"""Toggle completion checkoff for one or more service rows on a Repair Order."""
		target = service_row_ids if service_row_ids is not None else service_row_id
		if target is None:
			rows: list[str] = []
		elif isinstance(target, (list, tuple)):
			rows = [str(r).strip() for r in target if str(r).strip()]
		else:
			rows = [p.strip() for p in str(target).split(",") if p.strip()]
		args = _drop_none({
			"repair_order": repair_order,
			"service_row_id": rows[0] if len(rows) == 1 else None,
			"service_row_ids": rows if len(rows) > 1 else None,
			"is_completed": bool(is_completed),
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.toggle_service_completion", args)

	def add_supplement(
		self,
		repair_order: str,
		service_title: str,
		category: str = "Maintenance",
		labor_hours: float = 1.0,
		labor_rate: Optional[float] = None,
		parts: Optional[list[dict[str, Any]]] = None,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Add a mid-job supplemental operation in Estimating status for customer approval."""
		args = _drop_none({
			"repair_order": repair_order,
			"service_title": service_title,
			"category": category,
			"labor_hours": labor_hours,
			"labor_rate": labor_rate,
			"parts": parts,
			"notes": notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.add_supplement", args)

	def duplicate_service_for_opposite_side(
		self,
		repair_order: str,
		service_row_id: str,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Duplicate a symmetrical LH or RH service row for the opposite side."""
		args = _drop_none({
			"repair_order": repair_order,
			"service_row_id": service_row_id,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.duplicate_service_for_opposite_side", args)

	def get_deferred_services(
		self,
		repair_order: Optional[str] = None,
		vehicle: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch open previously declined/deferred services for a vehicle or Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"vehicle": vehicle,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.get_deferred_services", args)

	def add_deferred_services(
		self,
		repair_order: str,
		deferred_items: list[Any],
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Add previously deferred service items onto a Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"deferred_items": deferred_items,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.add_deferred_services", args)

	def list_shop_action_items(
		self,
		status: str = "Open",
		reference_doctype: Optional[str] = None,
		reference_name: Optional[str] = None,
		category: Optional[str] = None,
		limit: int = 20,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""List Shop Action Items matching filters."""
		args = _drop_none({
			"status": status,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"category": category,
			"limit": limit,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.list_shop_action_items", args)

	def create_shop_action_item(
		self,
		title: str,
		category: str = "Shop Floor Discrepancy",
		priority: str = "High",
		reference_doctype: Optional[str] = None,
		reference_name: Optional[str] = None,
		item_code: Optional[str] = None,
		details: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Create a Shop Action Item escalation."""
		args = _drop_none({
			"title": title,
			"category": category,
			"priority": priority,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"item_code": item_code,
			"details": details,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.create_shop_action_item", args)

	def resolve_shop_action_item(
		self,
		action_item: str,
		resolution_notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Mark a Shop Action Item as Resolved."""
		args = _drop_none({
			"action_item": action_item,
			"resolution_notes": resolution_notes,
			"include_summary": True if include_summary else None,
		})
		return self.call_method("ism.api.agent.resolve_shop_action_item", args)

	# --- Generic Frappe Operations ---

	def call_method(self, method: str, args: Optional[dict[str, Any]] = None) -> Any:
		"""Execute a whitelisted Frappe method over direct REST."""
		return self._tx.call_rest_method(method, args or {})

	def list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]] = None,
		fields: Optional[list[str]] = None,
		limit: int = 20,
		order_by: Optional[str] = None,
	) -> Any:
		"""List documents for a Frappe DocType over direct REST."""
		return self._tx.rest_list_documents(doctype, filters, fields, limit, order_by)

	def get_document(self, doctype: str, name: str) -> Any:
		"""Fetch a single Frappe document over direct REST."""
		return self._tx.rest_get_document(doctype, name)

	def create_document(self, doctype: str, data: dict[str, Any]) -> Any:
		"""Create a Frappe document over direct REST."""
		return self._tx.rest_create_document(doctype, data)

	def update_document(self, doctype: str, name: str, data: dict[str, Any]) -> Any:
		"""Update an existing Frappe document over direct REST."""
		return self._tx.rest_update_document(doctype, name, data)


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
	return {k: v for k, v in data.items() if v is not None}

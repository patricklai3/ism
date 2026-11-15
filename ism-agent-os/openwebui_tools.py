"""Open WebUI Tools adapter exposing all ISM Agent OS tools."""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

try:
	from ism_agent_os.client import IsmAgentClient
	from ism_agent_os.config import IsmConfig
except ImportError:
	from tools.client import IsmAgentClient
	from tools.config import IsmConfig


class Tools:
	"""Open WebUI native toolset backed by IsmAgentClient."""

	class Valves(BaseModel):
		FRAPPE_URL: str = Field(default="http://localhost:8000", description="Base URL of your Frappe site")
		FRAPPE_API_KEY: str = Field(default="", description="Frappe Service Advisor API Key")
		FRAPPE_API_SECRET: str = Field(default="", description="Frappe Service Advisor API Secret")
		FRAPPE_SITE: str = Field(default="", description="Optional Frappe multi-tenant site header")
		ISM_COMPANY: str = Field(default="", description="Optional default Company name")

	def __init__(self) -> None:
		self.valves = self.Valves()

	def _client(self) -> IsmAgentClient:
		cfg = IsmConfig(
			url=self.valves.FRAPPE_URL,
			api_key=self.valves.FRAPPE_API_KEY,
			api_secret=self.valves.FRAPPE_API_SECRET,
			site=self.valves.FRAPPE_SITE,
			company=self.valves.ISM_COMPANY,
		)
		return IsmAgentClient(cfg)

	def ism_create_quote(
		self,
		customer_name: str = "",
		vehicle_identifier: str = "",
		services: Optional[Union[list[Any], str]] = None,
		parts: Optional[Union[list[dict[str, Any]], str]] = None,
		notes: str = "",
		customer_phone: str = "",
		customer_email: str = "",
		company: str = "",
		odometer_in: Optional[int] = None,
		include_summary: bool = False,
		customer: str = "",
		vehicle: str = "",
		concern: str = "",
	) -> str:
		"""Create a Draft Estimate Repair Order with server-computed totals and 13% HST."""
		res = self._client().create_quote(
			customer_name=customer_name or None,
			customer=customer or None,
			vehicle_identifier=vehicle_identifier or None,
			vehicle=vehicle or None,
			services=_parse_json_param(services),
			parts=_parse_json_param(parts),
			notes=notes or None,
			concern=concern or None,
			customer_phone=customer_phone or None,
			customer_email=customer_email or None,
			company=company or None,
			odometer_in=odometer_in,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_amend_quote(
		self,
		repair_order: str,
		add_services: Optional[Union[list[Any], str]] = None,
		remove_service_indices: Optional[Union[list[int], str]] = None,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Add or remove service operations on an unapproved Draft Estimate Repair Order."""
		res = self._client().amend_quote(
			repair_order=repair_order,
			add_services=_parse_json_param(add_services),
			remove_service_indices=_parse_json_param(remove_service_indices),
			notes=notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: str = "",
		include_summary: bool = False,
		recipient: str = "",
	) -> str:
		"""Generate a 48-hour customer approval link and dispatch it via SMS, email, or link."""
		res = self._client().send_quote_for_approval(
			repair_order=repair_order,
			communication_medium=communication_medium,
			phone_number=phone_number or None,
			recipient=recipient or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_find_available_slots(
		self,
		repair_order: str = "",
		duration_minutes: Optional[int] = None,
		start_date: str = "",
		days_ahead: int = 5,
		equipment_tags: Optional[Union[list[str], str]] = None,
		include_resources: bool = False,
		max_slots: int = 15,
		include_past: bool = False,
		min_start_time: str = "",
		include_summary: bool = False,
		target_date: str = "",
	) -> str:
		"""Find conflict-free bay and technician appointment slots (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		res = self._client().find_available_slots(
			repair_order=repair_order or None,
			duration_minutes=duration_minutes,
			start_date=start_date or None,
			target_date=target_date or None,
			days_ahead=days_ahead,
			equipment_tags=_parse_json_param(equipment_tags),
			include_resources=include_resources,
			max_slots=max_slots,
			include_past=include_past,
			min_start_time=min_start_time or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_reschedule_appointment(
		self,
		schedule_entry: str,
		scheduled_date: str = "",
		start_time: str = "",
		duration_minutes: Optional[int] = None,
		service_bay: str = "",
		technician: str = "",
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Move an existing Schedule Entry to a new date, time, bay, or technician (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		res = self._client().reschedule_appointment(
			schedule_entry=schedule_entry,
			scheduled_date=scheduled_date or None,
			start_time=start_time or None,
			duration_minutes=duration_minutes,
			service_bay=service_bay or None,
			technician=technician or None,
			notes=notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_update_appointment_status(
		self,
		schedule_entry: str,
		status: str,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Transition a Schedule Entry status (Confirmed, In Progress, Completed, No Show, Cancelled)."""
		res = self._client().update_appointment_status(
			schedule_entry=schedule_entry,
			status=status,
			notes=notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_create_ro_from_appointment(
		self,
		schedule_entry: str,
		odometer_in: int,
		include_summary: bool = False,
	) -> str:
		"""Create a Checked In Repair Order from a pre-intake Schedule Entry."""
		res = self._client().create_ro_from_appointment(
			schedule_entry=schedule_entry,
			odometer_in=odometer_in,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_estimate_duration(
		self,
		repair_order: str = "",
		operations: Optional[Union[list[Any], str]] = None,
		percentile: Optional[float] = None,
		sigma: Optional[float] = None,
		buffer_minutes: Optional[int] = None,
		include_summary: bool = False,
	) -> str:
		"""Estimate probabilistic duration for a Repair Order or operations list."""
		res = self._client().estimate_duration(
			repair_order=repair_order or None,
			operations=_parse_json_param(operations),
			percentile=percentile,
			sigma=sigma,
			buffer_minutes=buffer_minutes,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_book_appointment(
		self,
		repair_order: str = "",
		scheduled_date: str = "",
		start_time: str = "",
		service_bay: str = "",
		technician: str = "",
		entry_type: str = "",
		notes: str = "",
		estimated_duration: Optional[int] = None,
		provisional_customer_name: str = "",
		provisional_vehicle_info: str = "",
		include_summary: bool = False,
	) -> str:
		"""Book a linked or provisional bay + technician Schedule Entry and update the ambient calendar (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		res = self._client().book_appointment(
			repair_order=repair_order or None,
			scheduled_date=scheduled_date,
			start_time=start_time,
			service_bay=service_bay or None,
			technician=technician or None,
			entry_type=entry_type or None,
			notes=notes or None,
			estimated_duration=estimated_duration,
			provisional_customer_name=provisional_customer_name or None,
			provisional_vehicle_info=provisional_vehicle_info or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_shop_floor_status(
		self,
		date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Return active service bays, on-duty technicians, appointments, and active Repair Orders (optional date: ISO YYYY-MM-DD)."""
		res = self._client().get_shop_floor_status(
			date=date or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_update_job_status(
		self,
		repair_order: str,
		action: str,
		odometer_out: Optional[int] = None,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Execute a floor status transition (start, hold, or complete) with authorization checks."""
		res = self._client().update_job_status(
			repair_order=repair_order,
			action=action,
			odometer_out=odometer_out,
			notes=notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_import_supplier_catalog(
		self,
		items: Optional[Union[list[dict[str, Any]], dict[str, Any], str]] = None,
		raw_text: str = "",
		supplier: str = "",
		default_source: str = "OEM",
		default_condition: str = "New",
		service_package: str = "",
		include_summary: bool = False,
	) -> str:
		"""Bulk ingest structured part items or delimited supplier text into the Frappe Item catalog."""
		res = self._client().import_supplier_catalog(
			items=_parse_json_param(items),
			raw_text=raw_text or None,
			supplier=supplier or None,
			default_source=default_source,
			default_condition=default_condition,
			service_package=service_package or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_component_alternates(
		self,
		service_package: str = "",
		component_slot: str = "",
		slot_number: Optional[int] = None,
		oem_part_number: str = "",
		item_code: str = "",
		include_summary: bool = False,
	) -> str:
		"""Find compatible OEM, aftermarket, or used alternate parts with stock info."""
		res = self._client().get_component_alternates(
			service_package=service_package or None,
			component_slot=component_slot or None,
			slot_number=slot_number,
			oem_part_number=oem_part_number or None,
			item_code=item_code or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_import_tesla_procedure(
		self,
		procedure_title: str = "",
		correction_code: str = "",
		labor_hours: float = 1.0,
		model: str = "Model 3",
		parts: Optional[Union[list[dict[str, Any]], str]] = None,
		repair_order: str = "",
		url: str = "",
		data: Optional[Union[dict[str, Any], str]] = None,
		on_conflict: str = "skip",
		force: bool = False,
		side: str = "",
		is_supplement: int = 0,
		advisor_notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Save a Tesla procedure as a Service Package or import it onto a Repair Order."""
		res = self._client().import_tesla_procedure(
			procedure_title=procedure_title or None,
			correction_code=correction_code or None,
			labor_hours=labor_hours,
			model=model,
			parts=_parse_json_param(parts),
			repair_order=repair_order or None,
			url=url or None,
			data=_parse_json_param(data),
			on_conflict=on_conflict,
			force=force,
			side=side or None,
			is_supplement=is_supplement,
			advisor_notes=advisor_notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_check_parts_stock(
		self,
		repair_order: str = "",
		items: Optional[Union[list[Any], str]] = None,
		company: str = "",
		ro_name: str = "",
		include_summary: bool = False,
	) -> str:
		"""Check live stock availability, committed quantities, and deficits for a Repair Order or items."""
		res = self._client().check_parts_stock(
			repair_order=(repair_order or ro_name) or None,
			items=_parse_json_param(items),
			company=company or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_submit_procurement_orders(
		self,
		repair_order: str,
		parts_data: Union[list[dict[str, Any]], str],
		submit: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Create Purchase Orders (Draft by default) or RFQs grouped by supplier."""
		parsed_parts = _parse_json_param(parts_data) or []
		res = self._client().submit_procurement_orders(
			repair_order=repair_order,
			parts_data=parsed_parts,
			submit=submit,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_fetch_tesla_manual(
		self,
		url: str,
		repair_order: str = "",
		vehicle: str = "",
		include_summary: bool = False,
	) -> str:
		"""Fetch and parse a service.tesla.com procedure URL with optional compatibility check."""
		res = self._client().fetch_tesla_procedure(
			url=url,
			repair_order=repair_order or None,
			vehicle=vehicle or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_decode_vehicle_vin(
		self,
		vin: str,
		include_summary: bool = False,
	) -> str:
		"""Decode a 17-character Tesla VIN and resolve recommended manual URL."""
		res = self._client().decode_vehicle_vin(vin=vin, include_summary=include_summary)
		return _format(res)

	def ism_search_vehicles(
		self,
		query: str = "",
		customer: str = "",
		model: str = "",
		limit: int = 20,
		include_summary: bool = False,
	) -> str:
		"""Search Repair Vehicles by VIN, license plate, customer, or model."""
		res = self._client().search_vehicles(
			query=query or None,
			customer=customer or None,
			model=model or None,
			limit=limit,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_vehicle_intelligence(
		self,
		vehicle: str,
		history_limit: int = 20,
		include_summary: bool = False,
	) -> str:
		"""Retrieve VIN decode, owners, service history, and deferred work for a vehicle."""
		res = self._client().get_vehicle_intelligence(
			vehicle=vehicle,
			history_limit=history_limit,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_link_vehicle_customer(
		self,
		vehicle: str,
		customer: str,
		is_primary: bool = False,
		relationship: str = "",
		include_summary: bool = False,
	) -> str:
		"""Link a Customer as primary owner or additional contact on a Repair Vehicle."""
		res = self._client().link_vehicle_customer(
			vehicle=vehicle,
			customer=customer,
			is_primary=is_primary,
			relationship=relationship or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_list_inspection_templates(
		self,
		include_inactive: bool = False,
		include_summary: bool = False,
	) -> str:
		"""List available check-in Inspection Templates."""
		res = self._client().list_inspection_templates(
			include_inactive=include_inactive,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_inspection_template(
		self,
		template_name: str = "",
		include_summary: bool = False,
	) -> str:
		"""Retrieve a check-in Inspection Template and its checklist items."""
		res = self._client().get_inspection_template(
			template_name=template_name or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_apply_inspection_template(
		self,
		repair_order: str,
		template_name: str = "",
		replace_existing: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Populate check-in inspection items on a Repair Order from a template."""
		res = self._client().apply_inspection_template(
			repair_order=repair_order,
			template_name=template_name or None,
			replace_existing=replace_existing,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_check_in_inspection(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> str:
		"""Retrieve check-in inspection items and summary counts for a Repair Order."""
		res = self._client().get_check_in_inspection(
			repair_order=repair_order,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_record_check_in_inspection(
		self,
		repair_order: str,
		items: Optional[Union[list[Any], str]] = None,
		odometer_in: Optional[int] = None,
		odometer: Optional[int] = None,
		customer_concern: str = "",
		template_name: str = "",
		auto_apply_template: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Record check-in walkaround results, odometer reading, and customer concern."""
		res = self._client().record_check_in_inspection(
			repair_order=repair_order,
			items=_parse_json_param(items),
			odometer_in=odometer_in,
			odometer=odometer,
			customer_concern=customer_concern or None,
			template_name=template_name or None,
			auto_apply_template=auto_apply_template,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_tech_overview(
		self,
		date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Retrieve daily technician utilization, scheduled hours, and unassigned entries (optional date: ISO YYYY-MM-DD)."""
		res = self._client().get_daily_tech_overview(
			date=date or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_unscheduled_services(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> str:
		"""Return approved services on a Repair Order not yet allocated to a Schedule Entry."""
		res = self._client().get_unscheduled_services(
			repair_order=repair_order,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_receive_purchase_order(
		self,
		purchase_order: str,
		submit_po: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Submit (when confirmed) and post Purchase Receipt for a Purchase Order."""
		res = self._client().receive_purchase_order(
			purchase_order=purchase_order,
			submit_po=submit_po,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_checkout_repair_order(
		self,
		repair_order: str,
		payment_method: Optional[str] = None,
		paid_amount: Optional[float] = None,
		odometer_out: Optional[int] = None,
		confirm: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Invoice, collect payment, complete schedule entries, and release a Repair Order."""
		res = self._client().checkout_repair_order(
			repair_order=repair_order,
			payment_method=payment_method,
			paid_amount=paid_amount,
			odometer_out=odometer_out,
			confirm=confirm,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_toggle_service_completion(
		self,
		repair_order: str,
		service_row_id: str = "",
		is_completed: bool = True,
		include_summary: bool = False,
		service_row_ids: Optional[Union[list[str], str]] = None,
	) -> str:
		"""Toggle completion status on one or more approved service rows."""
		parsed_ids = (
			_parse_json_param(service_row_ids)
			if isinstance(service_row_ids, str) and service_row_ids.lstrip().startswith("[")
			else service_row_ids
		)
		res = self._client().toggle_service_completion(
			repair_order=repair_order,
			service_row_id=service_row_id or None,
			service_row_ids=parsed_ids,
			is_completed=is_completed,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_add_supplement(
		self,
		repair_order: str,
		service_title: str,
		category: str = "Maintenance",
		labor_hours: float = 1.0,
		labor_rate: Optional[float] = None,
		parts: Optional[Union[list[dict[str, Any]], str]] = None,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Add a mid-job supplemental operation in Estimating state for customer approval."""
		res = self._client().add_supplement(
			repair_order=repair_order,
			service_title=service_title,
			category=category,
			labor_hours=labor_hours,
			labor_rate=labor_rate,
			parts=_parse_json_param(parts),
			notes=notes or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_duplicate_service_for_opposite_side(
		self,
		repair_order: str,
		service_row_id: str,
		include_summary: bool = False,
	) -> str:
		"""Duplicate a symmetrical LH or RH service row for the opposite side."""
		res = self._client().duplicate_service_for_opposite_side(
			repair_order=repair_order,
			service_row_id=service_row_id,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_get_deferred_services(
		self,
		repair_order: str = "",
		vehicle: str = "",
		include_summary: bool = False,
	) -> str:
		"""Check open previously declined/deferred services for a vehicle or Repair Order."""
		res = self._client().get_deferred_services(
			repair_order=repair_order or None,
			vehicle=vehicle or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_add_deferred_services(
		self,
		repair_order: str,
		deferred_items: Union[list[Any], str],
		include_summary: bool = False,
	) -> str:
		"""Add previously declined/deferred services onto a Repair Order."""
		res = self._client().add_deferred_services(
			repair_order=repair_order,
			deferred_items=_parse_json_param(deferred_items) or [],
			include_summary=include_summary,
		)
		return _format(res)

	def ism_list_shop_action_items(
		self,
		status: str = "Open",
		reference_doctype: str = "",
		reference_name: str = "",
		category: str = "",
		limit: int = 20,
		include_summary: bool = False,
	) -> str:
		"""List Shop Action Items matching optional filters."""
		res = self._client().list_shop_action_items(
			status=status,
			reference_doctype=reference_doctype or None,
			reference_name=reference_name or None,
			category=category or None,
			limit=limit,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_create_shop_action_item(
		self,
		title: str,
		category: str = "Shop Floor Discrepancy",
		priority: str = "High",
		reference_doctype: str = "",
		reference_name: str = "",
		item_code: str = "",
		details: str = "",
		include_summary: bool = False,
	) -> str:
		"""Create a Shop Action Item escalation."""
		res = self._client().create_shop_action_item(
			title=title,
			category=category,
			priority=priority,
			reference_doctype=reference_doctype or None,
			reference_name=reference_name or None,
			item_code=item_code or None,
			details=details or None,
			include_summary=include_summary,
		)
		return _format(res)

	def ism_resolve_shop_action_item(
		self,
		action_item: str,
		resolution_notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Resolve an open Shop Action Item with notes."""
		res = self._client().resolve_shop_action_item(
			action_item=action_item,
			resolution_notes=resolution_notes or None,
			include_summary=include_summary,
		)
		return _format(res)


def _parse_json_param(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return None
		return json.loads(stripped)
	return value


def _format(res: Any) -> str:
	if isinstance(res, dict) and res.get("summary_card"):
		return str(res["summary_card"])
	return json.dumps(res, separators=(",", ":"), default=str)

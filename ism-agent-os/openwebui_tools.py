"""Open WebUI Tools adapter exposing all ISM Agent OS tools."""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

try:
	from ism_agent_os.client import IsmAgentClient, IsmApiError
	from ism_agent_os.config import IsmConfig
except ImportError:
	from tools.client import IsmAgentClient, IsmApiError
	from tools.config import IsmConfig


class Tools:
	"""Open WebUI native toolset backed by IsmAgentClient."""

	class Valves(BaseModel):
		FRAPPE_URL: str = Field(default="http://localhost:8000", description="Base URL of your Frappe site")
		FRAPPE_API_KEY: str = Field(default="", description="Frappe Service Advisor API Key")
		FRAPPE_API_SECRET: str = Field(default="", repr=False, description="Frappe Service Advisor API Secret")
		FRAPPE_SITE: str = Field(default="", description="Optional Frappe multi-tenant site header")
		ISM_COMPANY: str = Field(default="", description="Optional default Company name")

	def __init__(self) -> None:
		self.valves = self.Valves()
		self._cached_cfg: Optional[tuple[str, str, str, str, str]] = None
		self._cached_client: Optional[IsmAgentClient] = None

	def _valves_key(self) -> tuple[str, str, str, str, str]:
		return (
			self.valves.FRAPPE_URL,
			self.valves.FRAPPE_API_KEY,
			self.valves.FRAPPE_API_SECRET,
			self.valves.FRAPPE_SITE,
			self.valves.ISM_COMPANY,
		)

	def _client(self) -> IsmAgentClient:
		key = self._valves_key()
		if self._cached_client is not None and self._cached_cfg == key:
			return self._cached_client
		if self._cached_client is not None:
			self._cached_client.close()
		cfg = IsmConfig(
			url=self.valves.FRAPPE_URL,
			api_key=self.valves.FRAPPE_API_KEY,
			api_secret=self.valves.FRAPPE_API_SECRET,
			site=self.valves.FRAPPE_SITE,
			company=self.valves.ISM_COMPANY,
		)
		self._cached_client = IsmAgentClient(cfg)
		self._cached_cfg = key
		return self._cached_client

	def close(self) -> None:
		"""Close any cached IsmAgentClient connection pool."""
		if self._cached_client is not None:
			self._cached_client.close()
			self._cached_client = None
			self._cached_cfg = None

	def __enter__(self) -> Tools:
		return self

	def __exit__(self, *_: Any) -> None:
		self.close()

	def _invoke(
		self,
		_method_name: str,
		/,
		*,
		json_fields: tuple[str, ...] = (),
		empty_list_fields: tuple[str, ...] = (),
		csv_or_json_fields: tuple[str, ...] = (),
		**kwargs: Any,
	) -> str:
		try:
			call_kwargs = dict(kwargs)
			for field_name in json_fields:
				call_kwargs[field_name] = _parse_json_param(call_kwargs.get(field_name))
			for field_name in empty_list_fields:
				call_kwargs[field_name] = _parse_json_param(call_kwargs.get(field_name)) or []
			for field_name in csv_or_json_fields:
				call_kwargs[field_name] = _parse_list_or_csv_param(call_kwargs.get(field_name))
			res = getattr(self._client(), _method_name)(**call_kwargs)
			return _format(res)
		except (IsmApiError, json.JSONDecodeError, ValueError, TypeError) as exc:
			return _format({"success": False, "error": str(exc)})

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
		return self._invoke(
			"create_quote",
			json_fields=("services", "parts"),
			customer_name=customer_name or None,
			customer=customer or None,
			vehicle_identifier=vehicle_identifier or None,
			vehicle=vehicle or None,
			services=services,
			parts=parts,
			notes=notes or None,
			concern=concern or None,
			customer_phone=customer_phone or None,
			customer_email=customer_email or None,
			company=company or None,
			odometer_in=odometer_in,
			include_summary=include_summary,
		)

	def ism_amend_quote(
		self,
		repair_order: str,
		add_services: Optional[Union[list[Any], str]] = None,
		remove_service_indices: Optional[Union[list[int], str]] = None,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Add or remove service operations on an unapproved Draft Estimate Repair Order."""
		return self._invoke(
			"amend_quote",
			json_fields=("add_services", "remove_service_indices"),
			repair_order=repair_order,
			add_services=add_services,
			remove_service_indices=remove_service_indices,
			notes=notes or None,
			include_summary=include_summary,
		)

	def ism_send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: str = "",
		include_summary: bool = False,
		recipient: str = "",
	) -> str:
		"""Generate a 48-hour customer approval link and dispatch it via SMS, email, or link."""
		return self._invoke(
			"send_quote_for_approval",
			repair_order=repair_order,
			communication_medium=communication_medium,
			phone_number=phone_number or None,
			recipient=recipient or None,
			include_summary=include_summary,
		)

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
		service_bay: str = "",
		technician: str = "",
	) -> str:
		"""Find conflict-free bay and technician appointment slots (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		return self._invoke(
			"find_available_slots",
			csv_or_json_fields=("equipment_tags",),
			repair_order=repair_order or None,
			duration_minutes=duration_minutes,
			start_date=start_date or None,
			target_date=target_date or None,
			days_ahead=days_ahead,
			equipment_tags=equipment_tags,
			service_bay=service_bay or None,
			technician=technician or None,
			include_resources=include_resources,
			max_slots=max_slots,
			include_past=include_past,
			min_start_time=min_start_time or None,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"reschedule_appointment",
			schedule_entry=schedule_entry,
			scheduled_date=scheduled_date or None,
			start_time=start_time or None,
			duration_minutes=duration_minutes,
			service_bay=service_bay or None,
			technician=technician or None,
			notes=notes or None,
			include_summary=include_summary,
		)

	def ism_update_appointment_status(
		self,
		schedule_entry: str,
		status: str,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Transition a Schedule Entry status (Confirmed, In Progress, Completed, No Show, Cancelled)."""
		return self._invoke(
			"update_appointment_status",
			schedule_entry=schedule_entry,
			status=status,
			notes=notes or None,
			include_summary=include_summary,
		)

	def ism_create_ro_from_appointment(
		self,
		schedule_entry: str,
		odometer_in: int,
		include_summary: bool = False,
	) -> str:
		"""Create a Checked In Repair Order from a pre-intake Schedule Entry."""
		return self._invoke(
			"create_ro_from_appointment",
			schedule_entry=schedule_entry,
			odometer_in=odometer_in,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"estimate_duration",
			json_fields=("operations",),
			repair_order=repair_order or None,
			operations=operations,
			percentile=percentile,
			sigma=sigma,
			buffer_minutes=buffer_minutes,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"book_appointment",
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

	def ism_get_shop_floor_status(
		self,
		date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Return active service bays, on-duty technicians, appointments, and active Repair Orders (optional date: ISO YYYY-MM-DD)."""
		return self._invoke(
			"get_shop_floor_status",
			date=date or None,
			include_summary=include_summary,
		)

	def ism_update_job_status(
		self,
		repair_order: str,
		action: str,
		odometer_out: Optional[int] = None,
		notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Execute a floor status transition (start, hold, or complete) with authorization checks."""
		return self._invoke(
			"update_job_status",
			repair_order=repair_order,
			action=action,
			odometer_out=odometer_out,
			notes=notes or None,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"import_supplier_catalog",
			json_fields=("items",),
			items=items,
			raw_text=raw_text or None,
			supplier=supplier or None,
			default_source=default_source,
			default_condition=default_condition,
			service_package=service_package or None,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"get_component_alternates",
			service_package=service_package or None,
			component_slot=component_slot or None,
			slot_number=slot_number,
			oem_part_number=oem_part_number or None,
			item_code=item_code or None,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"import_tesla_procedure",
			json_fields=("parts", "data"),
			procedure_title=procedure_title or None,
			correction_code=correction_code or None,
			labor_hours=labor_hours,
			model=model,
			parts=parts,
			repair_order=repair_order or None,
			url=url or None,
			data=data,
			on_conflict=on_conflict,
			force=force,
			side=side or None,
			is_supplement=is_supplement,
			advisor_notes=advisor_notes or None,
			include_summary=include_summary,
		)

	def ism_check_parts_stock(
		self,
		repair_order: str = "",
		items: Optional[Union[list[Any], str]] = None,
		company: str = "",
		ro_name: str = "",
		include_summary: bool = False,
	) -> str:
		"""Check live stock availability, committed quantities, and deficits for a Repair Order or items."""
		return self._invoke(
			"check_parts_stock",
			json_fields=("items",),
			repair_order=(repair_order or ro_name) or None,
			items=items,
			company=company or None,
			include_summary=include_summary,
		)

	def ism_submit_procurement_orders(
		self,
		repair_order: str = "",
		parts_data: Union[list[dict[str, Any]], str, None] = None,
		submit: bool = True,
		company: str = "",
		payment_method: str = "",
		paid_amount: Optional[float] = None,
		reference_no: str = "",
		reference_date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Create and submit (by default) Purchase Orders or RFQs, optionally linked to an RO or paid upfront."""
		return self._invoke(
			"submit_procurement_orders",
			empty_list_fields=("parts_data",),
			repair_order=repair_order or None,
			parts_data=parts_data,
			submit=submit,
			company=company or None,
			payment_method=payment_method or None,
			paid_amount=paid_amount,
			reference_no=reference_no or None,
			reference_date=reference_date or None,
			include_summary=include_summary,
		)

	def ism_fetch_tesla_manual(
		self,
		url: str,
		repair_order: str = "",
		vehicle: str = "",
		include_summary: bool = False,
	) -> str:
		"""Fetch and parse a service.tesla.com procedure URL with optional compatibility check."""
		return self._invoke(
			"fetch_tesla_procedure",
			url=url,
			repair_order=repair_order or None,
			vehicle=vehicle or None,
			include_summary=include_summary,
		)

	def ism_decode_vehicle_vin(
		self,
		vin: str,
		include_summary: bool = False,
	) -> str:
		"""Decode a 17-character Tesla VIN and resolve recommended manual URL."""
		return self._invoke(
			"decode_vehicle_vin",
			vin=vin,
			include_summary=include_summary,
		)

	def ism_search_vehicles(
		self,
		query: str = "",
		customer: str = "",
		model: str = "",
		limit: int = 20,
		include_summary: bool = False,
	) -> str:
		"""Search Repair Vehicles by VIN, license plate, customer, or model."""
		return self._invoke(
			"search_vehicles",
			query=query or None,
			customer=customer or None,
			model=model or None,
			limit=limit,
			include_summary=include_summary,
		)

	def ism_get_vehicle_intelligence(
		self,
		vehicle: str,
		history_limit: int = 20,
		include_summary: bool = False,
	) -> str:
		"""Retrieve VIN decode, owners, service history, and deferred work for a vehicle."""
		return self._invoke(
			"get_vehicle_intelligence",
			vehicle=vehicle,
			history_limit=history_limit,
			include_summary=include_summary,
		)

	def ism_link_vehicle_customer(
		self,
		vehicle: str,
		customer: str,
		is_primary: bool = False,
		relationship: str = "",
		include_summary: bool = False,
	) -> str:
		"""Link a Customer as primary owner or additional contact on a Repair Vehicle."""
		return self._invoke(
			"link_vehicle_customer",
			vehicle=vehicle,
			customer=customer,
			is_primary=is_primary,
			relationship=relationship or None,
			include_summary=include_summary,
		)

	def ism_list_inspection_templates(
		self,
		include_inactive: bool = False,
		include_summary: bool = False,
	) -> str:
		"""List available check-in Inspection Templates."""
		return self._invoke(
			"list_inspection_templates",
			include_inactive=include_inactive,
			include_summary=include_summary,
		)

	def ism_get_inspection_template(
		self,
		template_name: str = "",
		include_summary: bool = False,
	) -> str:
		"""Retrieve a check-in Inspection Template and its checklist items."""
		return self._invoke(
			"get_inspection_template",
			template_name=template_name or None,
			include_summary=include_summary,
		)

	def ism_apply_inspection_template(
		self,
		repair_order: str,
		template_name: str = "",
		replace_existing: bool = False,
		include_summary: bool = False,
	) -> str:
		"""Populate check-in inspection items on a Repair Order from a template."""
		return self._invoke(
			"apply_inspection_template",
			repair_order=repair_order,
			template_name=template_name or None,
			replace_existing=replace_existing,
			include_summary=include_summary,
		)

	def ism_get_check_in_inspection(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> str:
		"""Retrieve check-in inspection items and summary counts for a Repair Order."""
		return self._invoke(
			"get_check_in_inspection",
			repair_order=repair_order,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"record_check_in_inspection",
			json_fields=("items",),
			repair_order=repair_order,
			items=items,
			odometer_in=odometer_in,
			odometer=odometer,
			customer_concern=customer_concern or None,
			template_name=template_name or None,
			auto_apply_template=auto_apply_template,
			include_summary=include_summary,
		)

	def ism_get_tech_overview(
		self,
		date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Retrieve daily technician utilization, scheduled hours, and unassigned entries (optional date: ISO YYYY-MM-DD)."""
		return self._invoke(
			"get_daily_tech_overview",
			date=date or None,
			include_summary=include_summary,
		)

	def ism_get_unscheduled_services(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> str:
		"""Return approved services on a Repair Order not yet allocated to a Schedule Entry."""
		return self._invoke(
			"get_unscheduled_services",
			repair_order=repair_order,
			include_summary=include_summary,
		)

	def ism_receive_purchase_order(
		self,
		purchase_order: str = "",
		submit_po: bool = True,
		purchase_invoice: str = "",
		include_summary: bool = False,
	) -> str:
		"""Submit PO (if still Draft) and post Purchase Receipt for a Purchase Order or Purchase Invoice."""
		return self._invoke(
			"receive_purchase_order",
			purchase_order=purchase_order or None,
			submit_po=submit_po,
			purchase_invoice=purchase_invoice or None,
			include_summary=include_summary,
		)

	def ism_create_purchase_payment(
		self,
		payment_method: str,
		purchase_order: str = "",
		purchase_invoice: str = "",
		paid_amount: Optional[float] = None,
		reference_no: str = "",
		reference_date: str = "",
		include_summary: bool = False,
	) -> str:
		"""Submit a supplier Payment Entry against a Purchase Order or Purchase Invoice."""
		return self._invoke(
			"create_purchase_payment",
			payment_method=payment_method,
			purchase_order=purchase_order or None,
			purchase_invoice=purchase_invoice or None,
			paid_amount=paid_amount,
			reference_no=reference_no or None,
			reference_date=reference_date or None,
			include_summary=include_summary,
		)

	def ism_create_purchase_invoice(
		self,
		purchase_order: str = "",
		supplier: str = "",
		parts_data: Optional[Union[list[dict[str, Any]], str]] = None,
		update_stock: bool = False,
		company: str = "",
		bill_no: str = "",
		bill_date: str = "",
		attach_advances: bool = True,
		include_summary: bool = False,
	) -> str:
		"""Submit a Purchase Invoice (PO-linked or standalone) with optional stock update and advance attachment."""
		return self._invoke(
			"create_purchase_invoice",
			json_fields=("parts_data",),
			purchase_order=purchase_order or None,
			supplier=supplier or None,
			parts_data=parts_data,
			update_stock=update_stock,
			company=company or None,
			bill_no=bill_no or None,
			bill_date=bill_date or None,
			attach_advances=attach_advances,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"checkout_repair_order",
			repair_order=repair_order,
			payment_method=payment_method,
			paid_amount=paid_amount,
			odometer_out=odometer_out,
			confirm=confirm,
			include_summary=include_summary,
		)

	def ism_toggle_service_completion(
		self,
		repair_order: str,
		service_row_id: str = "",
		is_completed: bool = True,
		include_summary: bool = False,
		service_row_ids: Optional[Union[list[str], str]] = None,
	) -> str:
		"""Toggle completion status on one or more approved service rows."""
		return self._invoke(
			"toggle_service_completion",
			csv_or_json_fields=("service_row_ids",),
			repair_order=repair_order,
			service_row_id=service_row_id or None,
			service_row_ids=service_row_ids,
			is_completed=is_completed,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"add_supplement",
			json_fields=("parts",),
			repair_order=repair_order,
			service_title=service_title,
			category=category,
			labor_hours=labor_hours,
			labor_rate=labor_rate,
			parts=parts,
			notes=notes or None,
			include_summary=include_summary,
		)

	def ism_duplicate_service_for_opposite_side(
		self,
		repair_order: str,
		service_row_id: str,
		include_summary: bool = False,
	) -> str:
		"""Duplicate a symmetrical LH or RH service row for the opposite side."""
		return self._invoke(
			"duplicate_service_for_opposite_side",
			repair_order=repair_order,
			service_row_id=service_row_id,
			include_summary=include_summary,
		)

	def ism_get_deferred_services(
		self,
		repair_order: str = "",
		vehicle: str = "",
		include_summary: bool = False,
	) -> str:
		"""Check open previously declined/deferred services for a vehicle or Repair Order."""
		return self._invoke(
			"get_deferred_services",
			repair_order=repair_order or None,
			vehicle=vehicle or None,
			include_summary=include_summary,
		)

	def ism_add_deferred_services(
		self,
		repair_order: str,
		deferred_items: Union[list[Any], str],
		include_summary: bool = False,
	) -> str:
		"""Add previously declined/deferred services onto a Repair Order."""
		return self._invoke(
			"add_deferred_services",
			empty_list_fields=("deferred_items",),
			repair_order=repair_order,
			deferred_items=deferred_items,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"list_shop_action_items",
			status=status,
			reference_doctype=reference_doctype or None,
			reference_name=reference_name or None,
			category=category or None,
			limit=limit,
			include_summary=include_summary,
		)

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
		return self._invoke(
			"create_shop_action_item",
			title=title,
			category=category,
			priority=priority,
			reference_doctype=reference_doctype or None,
			reference_name=reference_name or None,
			item_code=item_code or None,
			details=details or None,
			include_summary=include_summary,
		)

	def ism_resolve_shop_action_item(
		self,
		action_item: str,
		resolution_notes: str = "",
		include_summary: bool = False,
	) -> str:
		"""Resolve an open Shop Action Item with notes."""
		return self._invoke(
			"resolve_shop_action_item",
			action_item=action_item,
			resolution_notes=resolution_notes or None,
			include_summary=include_summary,
		)


def _parse_json_param(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return None
		return json.loads(stripped)
	return value


def _parse_list_or_csv_param(value: Any) -> Optional[list[str]]:
	if value is None:
		return None
	if isinstance(value, (list, tuple)):
		return [str(v).strip() for v in value if str(v).strip()]
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return None
		if stripped[0] in "[{\"":
			parsed = json.loads(stripped)
			if isinstance(parsed, str):
				return [p.strip() for p in parsed.split(",") if p.strip()]
			if not isinstance(parsed, list):
				raise ValueError("Expected a JSON array or comma-separated string.")
			return [str(v).strip() for v in parsed if str(v).strip()]
		return [p.strip() for p in stripped.split(",") if p.strip()]
	raise ValueError("Expected a list or comma-separated string.")


def _format(res: Any) -> str:
	if isinstance(res, dict) and res.get("summary_card"):
		return str(res["summary_card"])
	return json.dumps(res, separators=(",", ":"), default=str)

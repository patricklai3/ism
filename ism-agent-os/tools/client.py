"""High-level Python client for Frappe sites running ISM."""

from __future__ import annotations

import json
from typing import Any, Optional

from .config import IsmConfig, validate_tesla_url
from .transport import FrappeTransport, IsmApiError  # noqa: F401

__all__ = ["IsmAgentClient", "IsmApiError"]


class IsmAgentClient:
	"""High-level client wrapping ISM Agent Facade and Frappe REST APIs."""

	def __init__(self, config: Optional[IsmConfig] = None) -> None:
		self.config = config or IsmConfig.from_env()
		self._tx = FrappeTransport(self.config)

	def close(self) -> None:
		"""Close any pooled HTTP connections held by the underlying transport."""
		self._tx.close()

	def __enter__(self) -> IsmAgentClient:
		return self

	def __exit__(self, *_: Any) -> None:
		self.close()

	def _call_agent(
		self,
		endpoint: str,
		include_summary: bool = False,
		**kwargs: Any,
	) -> dict[str, Any]:
		"""Invoke an ism.api.agent endpoint, stripping None values."""
		args = _drop_none(kwargs)
		if include_summary:
			args["include_summary"] = True
		return self.call_method(f"ism.api.agent.{endpoint}", args)

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
		vehicle_color: Optional[str] = None,
		license_plate: Optional[str] = None,
		include_summary: bool = False,
		customer: Optional[str] = None,
		vehicle: Optional[str] = None,
		concern: Optional[str] = None,
	) -> dict[str, Any]:
		"""Create a Draft Estimate Repair Order with deterministic totals."""
		return self._call_agent(
			"create_quote",
			include_summary=include_summary,
			customer_name=customer_name,
			customer=customer,
			vehicle_identifier=vehicle_identifier,
			vehicle=vehicle,
			company=company or self.config.company or None,
			services=services,
			parts=parts,
			notes=notes,
			concern=concern,
			customer_phone=customer_phone,
			customer_email=customer_email,
			odometer_in=odometer_in,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
		)

	def amend_quote(
		self,
		repair_order: str,
		add_services: Optional[list[Any] | str] = None,
		remove_service_indices: Optional[list[int] | str] = None,
		update_services: Optional[list[Any] | str] = None,
		notes: Optional[str] = None,
		discount_amount: Optional[float] = None,
		shop_supplies_amount: Optional[float] = None,
		environmental_fee_amount: Optional[float] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Amend services, fees, discounts, or notes on an unapproved Repair Order."""
		return self._call_agent(
			"amend_quote",
			include_summary=include_summary,
			repair_order=repair_order,
			add_services=add_services,
			remove_service_indices=remove_service_indices,
			update_services=update_services,
			notes=notes,
			discount_amount=discount_amount,
			shop_supplies_amount=shop_supplies_amount,
			environmental_fee_amount=environmental_fee_amount,
		)

	def find_available_slots(
		self,
		repair_order: Optional[str] = None,
		duration_minutes: Optional[int] = None,
		start_date: Optional[str] = None,
		days_ahead: int = 3,
		equipment_tags: Optional[list[str] | str] = None,
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
		include_resources: bool = False,
		max_slots: Optional[int] = None,
		include_past: bool = False,
		min_start_time: Optional[str] = None,
		include_summary: bool = False,
		target_date: Optional[str] = None,
	) -> dict[str, Any]:
		"""Query dual-resource capacity for available bay and tech slots (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		return self._call_agent(
			"find_available_slots",
			include_summary=include_summary,
			repair_order=repair_order,
			duration_minutes=duration_minutes,
			start_date=start_date,
			target_date=target_date,
			days_ahead=days_ahead,
			equipment_tags=equipment_tags,
			service_bay=service_bay,
			technician=technician,
			include_resources=True if include_resources else None,
			max_slots=max_slots,
			include_past=True if include_past else None,
			min_start_time=min_start_time,
		)

	def intake_and_find_slots(
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
		vehicle_color: Optional[str] = None,
		license_plate: Optional[str] = None,
		start_date: Optional[str] = None,
		days_ahead: int = 3,
		duration_minutes: Optional[int] = None,
		equipment_tags: Optional[list[str] | str] = None,
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
		max_slots: Optional[int] = None,
		include_past: bool = False,
		min_start_time: Optional[str] = None,
		include_resources: bool = False,
		include_summary: bool = False,
		customer: Optional[str] = None,
		vehicle: Optional[str] = None,
		concern: Optional[str] = None,
		target_date: Optional[str] = None,
	) -> dict[str, Any]:
		"""Create a Draft Estimate Repair Order and return candidate booking slots in one call (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		return self._call_agent(
			"intake_and_find_slots",
			include_summary=include_summary,
			customer_name=customer_name,
			customer=customer,
			vehicle_identifier=vehicle_identifier,
			vehicle=vehicle,
			company=company or self.config.company or None,
			services=services,
			parts=parts,
			notes=notes,
			concern=concern,
			customer_phone=customer_phone,
			customer_email=customer_email,
			odometer_in=odometer_in,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
			start_date=start_date,
			target_date=target_date,
			days_ahead=days_ahead,
			duration_minutes=duration_minutes,
			equipment_tags=equipment_tags,
			service_bay=service_bay,
			technician=technician,
			max_slots=max_slots,
			include_past=True if include_past else None,
			min_start_time=min_start_time,
			include_resources=True if include_resources else None,
		)

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
		"""Reschedule an existing Schedule Entry with bay and technician checks (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		return self._call_agent(
			"reschedule_appointment",
			include_summary=include_summary,
			schedule_entry=schedule_entry,
			scheduled_date=scheduled_date,
			start_time=start_time,
			duration_minutes=duration_minutes,
			service_bay=service_bay,
			technician=technician,
			notes=notes,
		)

	def update_appointment_status(
		self,
		schedule_entry: str,
		status: str,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Transition a Schedule Entry status (Confirmed, No Show, Cancelled, etc.)."""
		return self._call_agent(
			"update_appointment_status",
			include_summary=include_summary,
			schedule_entry=schedule_entry,
			status=status,
			notes=notes,
		)

	def create_ro_from_appointment(
		self,
		schedule_entry: str,
		odometer_in: int,
		vehicle_color: Optional[str] = None,
		license_plate: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Create a Checked In Repair Order from a pre-intake Schedule Entry."""
		return self._call_agent(
			"create_ro_from_appointment",
			include_summary=include_summary,
			schedule_entry=schedule_entry,
			odometer_in=odometer_in,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
		)

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
		return self._call_agent(
			"estimate_duration",
			include_summary=include_summary,
			repair_order=repair_order,
			operations=operations,
			percentile=percentile,
			sigma=sigma,
			buffer_minutes=buffer_minutes,
		)

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
		"""Allocate a linked or provisional Schedule Entry and push realtime calendar update (dates: ISO YYYY-MM-DD, times: 24h HH:MM[:SS])."""
		return self._call_agent(
			"book_appointment",
			include_summary=include_summary,
			repair_order=repair_order,
			scheduled_date=scheduled_date,
			start_time=start_time,
			service_bay=service_bay,
			technician=technician,
			entry_type=entry_type,
			notes=notes,
			estimated_duration=estimated_duration,
			provisional_customer_name=provisional_customer_name,
			provisional_vehicle_info=provisional_vehicle_info,
		)

	def send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: Optional[str] = None,
		include_summary: bool = False,
		recipient: Optional[str] = None,
	) -> dict[str, Any]:
		"""Generate approval token and optionally send SMS/email link."""
		return self._call_agent(
			"send_quote_for_approval",
			include_summary=include_summary,
			repair_order=repair_order,
			communication_medium=communication_medium,
			phone_number=phone_number,
			recipient=recipient,
		)

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
		pkg = (service_package or "").strip()
		if not pkg:
			row_list = [items] if isinstance(items, dict) else (items if isinstance(items, list) else [])
			has_row_pkg = bool(row_list) and all(
				isinstance(r, dict) and bool(r.get("service_package") or r.get("service_packages"))
				for r in row_list
			)
			if not has_row_pkg:
				raise ValueError("service_package is required when importing supplier catalog parts.")
		return self._call_agent(
			"import_supplier_catalog",
			include_summary=include_summary,
			items=items,
			raw_text=raw_text,
			supplier=supplier,
			default_source=default_source,
			default_condition=default_condition,
			service_package=service_package,
		)

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
		return self._call_agent(
			"get_component_alternates",
			include_summary=include_summary,
			service_package=service_package,
			component_slot=component_slot,
			slot_number=slot_number,
			oem_part_number=oem_part_number,
			item_code=item_code,
		)

	def import_tesla_procedure(
		self,
		procedure_title: Optional[str] = None,
		correction_code: Optional[str] = None,
		labor_hours: float = 1.0,
		model: str = "Model 3",
		applicable_generation: Optional[str] = None,
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
		return self._call_agent(
			"import_tesla_procedure",
			include_summary=include_summary,
			procedure_title=procedure_title,
			correction_code=correction_code,
			labor_hours=labor_hours,
			model=model,
			applicable_generation=applicable_generation,
			parts=parts,
			repair_order=repair_order,
			url=url,
			data=data,
			on_conflict=on_conflict,
			force=True if force else None,
			side=side,
			is_supplement=1 if is_supplement else None,
			advisor_notes=advisor_notes,
		)

	def get_vehicle_generation_catalog(self, model: Optional[str] = None) -> Any:
		"""Return canonical Tesla generation metadata for a specific model or all models."""
		return self.call_method(
			"ism.api.tesla.get_vehicle_generation_catalog",
			_drop_none({"model": model}),
		)

	def get_shop_floor_status(self, date: Optional[str] = None, include_summary: bool = False) -> dict[str, Any]:
		"""Retrieve active bays, on-duty technicians, appointments, and ROs (optional date: ISO YYYY-MM-DD)."""
		return self._call_agent(
			"get_shop_floor_status",
			include_summary=include_summary,
			date=date,
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
		return self._call_agent(
			"update_job_status",
			include_summary=include_summary,
			repair_order=repair_order,
			action=action,
			odometer_out=odometer_out,
			notes=notes,
		)

	# --- Extended ISM Agent Facade Methods ---

	def get_parts_stock(
		self,
		ro_name: Optional[str] = None,
		company: Optional[str] = None,
		include_summary: bool = False,
		repair_order: Optional[str] = None,
		items: Optional[list[Any] | str] = None,
		bin_location: Optional[str] = None,
	) -> dict[str, Any]:
		return self._call_agent(
			"check_parts_stock",
			include_summary=include_summary,
			repair_order=repair_order or ro_name,
			items=items,
			company=company or self.config.company or None,
			bin_location=bin_location,
		)

	def check_parts_stock(
		self,
		repair_order: Optional[str] = None,
		items: Optional[list[Any] | str] = None,
		company: Optional[str] = None,
		bin_location: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Check live warehouse stock and deficits for a Repair Order or items list."""
		return self.get_parts_stock(
			company=company,
			include_summary=include_summary,
			repair_order=repair_order,
			items=items,
			bin_location=bin_location,
		)

	def list_bin_locations(
		self,
		warehouse: Optional[str] = None,
		zone: Optional[str] = None,
		query: Optional[str] = None,
		include_stock: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""List Storage Bin locations with optional warehouse, zone, query, and stock filters."""
		return self._call_agent(
			"list_bin_locations",
			include_summary=include_summary,
			warehouse=warehouse,
			zone=zone,
			query=query,
			include_stock=bool(include_stock),
		)

	def create_bin_location(
		self,
		bin_code: str,
		warehouse: Optional[str] = None,
		zone: Optional[str] = None,
		aisle: Optional[str] = None,
		rack: Optional[str] = None,
		shelf: Optional[str] = None,
		bin_type: str = "Standard",
		description: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Create a new Storage Bin master record."""
		return self._call_agent(
			"create_bin_location",
			include_summary=include_summary,
			bin_code=bin_code,
			warehouse=warehouse,
			zone=zone,
			aisle=aisle,
			rack=rack,
			shelf=shelf,
			bin_type=bin_type,
			description=description,
		)

	def set_item_default_bin(
		self,
		item_code: str,
		bin_location: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Set or clear Item.default_bin_location (Home Bin) for a part."""
		return self._call_agent(
			"set_item_default_bin",
			include_summary=include_summary,
			item_code=item_code,
			bin_location=bin_location if bin_location is not None else "",
		)

	def transfer_bin_stock(
		self,
		from_bin: str,
		to_bin: str,
		items: Optional[list[Any] | str] = None,
		item_code: Optional[str] = None,
		qty: Optional[float] = None,
		warehouse: Optional[str] = None,
		notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Transfer stock between two Storage Bins in the same warehouse."""
		return self._call_agent(
			"transfer_bin_stock",
			include_summary=include_summary,
			from_bin=from_bin,
			to_bin=to_bin,
			items=items,
			item_code=item_code,
			qty=qty,
			warehouse=warehouse,
			notes=notes,
		)

	def adjust_bin_stock(
		self,
		purpose: str,
		items: list[Any] | str,
		bin_location: Optional[str] = None,
		warehouse: Optional[str] = None,
		reason: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Post a bin-attributed Material Receipt or Material Issue Stock Entry."""
		purpose_norm = {
			"receipt": "Material Receipt",
			"material receipt": "Material Receipt",
			"issue": "Material Issue",
			"material issue": "Material Issue",
		}.get(str(purpose or "").strip().lower(), purpose)
		return self._call_agent(
			"adjust_bin_stock",
			include_summary=include_summary,
			purpose=purpose_norm,
			items=items,
			bin_location=bin_location,
			warehouse=warehouse,
			reason=reason,
		)

	def submit_procurement_orders(
		self,
		repair_order: Optional[str] = None,
		parts_data: Optional[list[dict[str, Any]]] = None,
		submit: bool = True,
		company: Optional[str] = None,
		payment_method: Optional[str] = None,
		paid_amount: Optional[float] = None,
		reference_no: Optional[str] = None,
		reference_date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		return self._call_agent(
			"submit_procurement_orders",
			include_summary=include_summary,
			repair_order=repair_order,
			parts_data=parts_data if parts_data is not None else [],
			submit=bool(submit),
			company=company or self.config.company or None,
			payment_method=payment_method,
			paid_amount=paid_amount,
			reference_no=reference_no,
			reference_date=reference_date,
		)

	def fetch_tesla_procedure(
		self,
		url: str,
		repair_order: Optional[str] = None,
		vehicle: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch and parse a Tesla service manual URL with optional vehicle compatibility check."""
		validate_tesla_url(url)
		return self._call_agent(
			"fetch_tesla_manual",
			include_summary=include_summary,
			url=url,
			repair_order=repair_order,
			vehicle=vehicle,
		)

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

	def decode_vehicle_vin(
		self,
		vin: str,
		manufactured_month: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Decode a 17-character Tesla VIN and resolve recommended service manual URL."""
		return self._call_agent(
			"decode_vehicle_vin",
			include_summary=include_summary,
			vin=vin,
			manufactured_month=manufactured_month,
		)

	def search_vehicles(
		self,
		query: Optional[str] = None,
		customer: Optional[str] = None,
		model: Optional[str] = None,
		generation: Optional[str] = None,
		limit: int = 20,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Search Repair Vehicles by VIN, license plate, customer, model, or generation."""
		return self._call_agent(
			"search_vehicles",
			include_summary=include_summary,
			query=query,
			customer=customer,
			model=model,
			generation=generation,
			limit=limit,
		)

	def get_vehicle_intelligence(
		self,
		vehicle: str,
		history_limit: int = 20,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Retrieve VIN decode, owners, service history, and open deferred services for a vehicle."""
		return self._call_agent(
			"get_vehicle_intelligence",
			include_summary=include_summary,
			vehicle=vehicle,
			history_limit=history_limit,
		)

	def link_vehicle_customer(
		self,
		vehicle: str,
		customer: str,
		is_primary: bool = False,
		relationship: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Link a Customer as primary owner or additional contact on a Repair Vehicle."""
		return self._call_agent(
			"link_vehicle_customer",
			include_summary=include_summary,
			vehicle=vehicle,
			customer=customer,
			is_primary=True if is_primary else None,
			relationship=relationship,
		)

	def list_inspection_templates(
		self,
		include_inactive: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""List available check-in Inspection Templates."""
		return self._call_agent(
			"list_inspection_templates",
			include_summary=include_summary,
			include_inactive=True if include_inactive else None,
		)

	def get_inspection_template(
		self,
		template_name: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch a check-in Inspection Template with its checklist items."""
		return self._call_agent(
			"get_inspection_template",
			include_summary=include_summary,
			template_name=template_name,
		)

	def apply_inspection_template(
		self,
		repair_order: str,
		template_name: Optional[str] = None,
		replace_existing: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Populate check-in inspection items on a Repair Order from a template."""
		return self._call_agent(
			"apply_inspection_template",
			include_summary=include_summary,
			repair_order=repair_order,
			template_name=template_name,
			replace_existing=True if replace_existing else None,
		)

	def get_check_in_inspection(
		self,
		repair_order: str,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Retrieve the check-in inspection items and summary counts for a Repair Order."""
		return self._call_agent(
			"get_check_in_inspection",
			include_summary=include_summary,
			repair_order=repair_order,
		)

	def record_check_in_inspection(
		self,
		repair_order: str,
		items: Optional[list[Any] | str] = None,
		odometer_in: Optional[int | float] = None,
		odometer: Optional[int | float] = None,
		customer_concern: Optional[str] = None,
		template_name: Optional[str] = None,
		auto_apply_template: bool = False,
		vehicle_color: Optional[str] = None,
		license_plate: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Record check-in walkaround item results, odometer reading, and customer concern."""
		return self._call_agent(
			"record_check_in_inspection",
			include_summary=include_summary,
			repair_order=repair_order,
			items=items,
			odometer_in=odometer_in,
			odometer=odometer,
			customer_concern=customer_concern,
			template_name=template_name,
			auto_apply_template=True if auto_apply_template else None,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
		)

	def get_daily_tech_overview(
		self,
		date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		return self._call_agent(
			"get_tech_overview",
			include_summary=include_summary,
			date=date,
		)

	def get_tech_overview(
		self,
		date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Canonical alias for get_daily_tech_overview matching ism.api.agent.get_tech_overview."""
		return self.get_daily_tech_overview(date=date, include_summary=include_summary)

	def get_unscheduled_services(self, repair_order: str, include_summary: bool = False) -> dict[str, Any]:
		return self._call_agent(
			"get_unscheduled_services",
			include_summary=include_summary,
			repair_order=repair_order,
		)

	def receive_purchase_order(
		self,
		purchase_order: Optional[str] = None,
		submit_po: bool = True,
		purchase_invoice: Optional[str] = None,
		bin_location: Optional[str] = None,
		bin_allocations: Optional[dict[str, str] | list[Any] | str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Submit PO (if still Draft) and receive items via Purchase Receipt."""
		return self._call_agent(
			"receive_purchase_order",
			include_summary=include_summary,
			purchase_order=purchase_order,
			purchase_invoice=purchase_invoice,
			submit_po=bool(submit_po),
			bin_location=bin_location,
			bin_allocations=bin_allocations,
		)

	def create_purchase_payment(
		self,
		payment_method: str,
		purchase_order: Optional[str] = None,
		purchase_invoice: Optional[str] = None,
		paid_amount: Optional[float] = None,
		reference_no: Optional[str] = None,
		reference_date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Submit a supplier Payment Entry against a Purchase Order (advance) or Purchase Invoice."""
		return self._call_agent(
			"create_purchase_payment",
			include_summary=include_summary,
			purchase_order=purchase_order,
			purchase_invoice=purchase_invoice,
			payment_method=payment_method,
			paid_amount=paid_amount,
			reference_no=reference_no,
			reference_date=reference_date,
		)

	def create_purchase_invoice(
		self,
		purchase_order: Optional[str] = None,
		supplier: Optional[str] = None,
		parts_data: Optional[list[dict[str, Any]]] = None,
		update_stock: bool = False,
		company: Optional[str] = None,
		bill_no: Optional[str] = None,
		bill_date: Optional[str] = None,
		attach_advances: bool = True,
		bin_location: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Submit a Purchase Invoice (PO-linked or standalone) with optional stock update and advance attachment."""
		return self._call_agent(
			"create_purchase_invoice",
			include_summary=include_summary,
			purchase_order=purchase_order,
			supplier=supplier,
			parts_data=parts_data,
			update_stock=bool(update_stock),
			company=company or self.config.company or None,
			bill_no=bill_no,
			bill_date=bill_date,
			attach_advances=bool(attach_advances),
			bin_location=bin_location,
		)

	def record_ro_deposit(
		self,
		repair_order: str,
		amount: float,
		payment_method: str = "Debit / Interac",
		reference_no: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Record a customer deposit payment against a Repair Order."""
		return self._call_agent(
			"record_ro_deposit",
			include_summary=include_summary,
			repair_order=repair_order,
			amount=amount,
			payment_method=payment_method,
			reference_no=reference_no,
		)

	def checkout_repair_order(
		self,
		repair_order: str,
		payment_method: Optional[str] = None,
		paid_amount: Optional[float] = None,
		odometer_out: Optional[int] = None,
		payments: Optional[list[dict[str, Any]] | str] = None,
		confirm: bool = False,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Invoice, collect payment, complete schedule entries, and release a Repair Order."""
		return self._call_agent(
			"checkout_repair_order",
			include_summary=include_summary,
			repair_order=repair_order,
			payment_method=payment_method,
			paid_amount=paid_amount,
			odometer_out=odometer_out,
			payments=payments,
			confirm=True if confirm else None,
		)

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
		return self._call_agent(
			"toggle_service_completion",
			include_summary=include_summary,
			repair_order=repair_order,
			service_row_id=rows[0] if len(rows) == 1 else None,
			service_row_ids=rows if len(rows) > 1 else None,
			is_completed=bool(is_completed),
		)

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
		return self._call_agent(
			"add_supplement",
			include_summary=include_summary,
			repair_order=repair_order,
			service_title=service_title,
			category=category,
			labor_hours=labor_hours,
			labor_rate=labor_rate,
			parts=parts,
			notes=notes,
		)

	def duplicate_service_for_opposite_side(
		self,
		repair_order: str,
		service_row_id: str,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Duplicate a symmetrical LH or RH service row for the opposite side."""
		return self._call_agent(
			"duplicate_service_for_opposite_side",
			include_summary=include_summary,
			repair_order=repair_order,
			service_row_id=service_row_id,
		)

	def get_deferred_services(
		self,
		repair_order: Optional[str] = None,
		vehicle: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch open previously declined/deferred services for a vehicle or Repair Order."""
		return self._call_agent(
			"get_deferred_services",
			include_summary=include_summary,
			repair_order=repair_order,
			vehicle=vehicle,
		)

	def add_deferred_services(
		self,
		repair_order: str,
		deferred_items: list[Any],
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Add previously deferred service items onto a Repair Order."""
		return self._call_agent(
			"add_deferred_services",
			include_summary=include_summary,
			repair_order=repair_order,
			deferred_items=deferred_items,
		)

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
		return self._call_agent(
			"list_shop_action_items",
			include_summary=include_summary,
			status=status,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			category=category,
			limit=limit,
		)

	def create_shop_action_item(
		self,
		title: str,
		category: str = "Shop Floor Discrepancy",
		priority: str = "High",
		reference_doctype: Optional[str] = None,
		reference_name: Optional[str] = None,
		item_code: Optional[str] = None,
		details: Optional[str] = None,
		dedup_key: Optional[str] = None,
		source: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Create (or upsert by dedup_key) a Shop Action Item escalation."""
		return self._call_agent(
			"create_shop_action_item",
			include_summary=include_summary,
			title=title,
			category=category,
			priority=priority,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			item_code=item_code,
			details=details,
			dedup_key=dedup_key,
			source=source,
		)

	def sync_action_item_state(
		self,
		dedup_key: str,
		action: str,
		snooze_minutes: Optional[int] = None,
		title: Optional[str] = None,
		notes: Optional[str] = None,
		reference_doctype: Optional[str] = None,
		reference_name: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Snooze, dismiss, or reopen a deduplicated Shop Action Item across managers."""
		return self._call_agent(
			"sync_action_item_state",
			include_summary=include_summary,
			dedup_key=dedup_key,
			action=action,
			snooze_minutes=snooze_minutes,
			title=title,
			notes=notes,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
		)

	def get_cockpit_snapshot(
		self,
		date: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch batched shop monitoring snapshot, capacities, overlays, and cached scores."""
		return self._call_agent(
			"get_cockpit_snapshot",
			include_summary=include_summary,
			date=date,
		)

	def get_pnl_digest_inputs(
		self,
		from_date: Optional[str] = None,
		to_date: Optional[str] = None,
		company: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch server-computed P&L, labor rate, parts margin, and unbilled WIP metrics."""
		return self._call_agent(
			"get_pnl_digest_inputs",
			include_summary=include_summary,
			from_date=from_date,
			to_date=to_date,
			company=company,
		)

	def publish_health_snapshot(
		self,
		metric_key: str,
		score: int,
		verdict: str,
		grade: Optional[str] = None,
		period_date: Optional[str] = None,
		report_url: Optional[str] = None,
		details: Optional[dict[str, Any] | list[Any] | str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Publish a synced 0-100 Shop Health Snapshot for manager cockpits."""
		return self._call_agent(
			"publish_health_snapshot",
			include_summary=include_summary,
			metric_key=metric_key,
			score=score,
			verdict=verdict,
			grade=grade,
			period_date=period_date,
			report_url=report_url,
			details=details,
		)

	def resolve_shop_action_item(
		self,
		action_item: str,
		resolution_notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Mark a Shop Action Item as Resolved."""
		return self._call_agent(
			"resolve_shop_action_item",
			include_summary=include_summary,
			action_item=action_item,
			resolution_notes=resolution_notes,
		)

	def _call_board(
		self,
		endpoint: str,
		**kwargs: Any,
	) -> dict[str, Any]:
		"""Invoke an ism.api.board endpoint, stripping None values."""
		return self.call_method(f"ism.api.board.{endpoint}", _drop_none(kwargs))

	# --- Shop Board OS Methods ---

	def get_action_cards_context(
		self,
		action_items: Optional[list[str] | str] = None,
		repair_order: Optional[str] = None,
		status: str = "Open",
		include_events_limit: int = 20,
		limit: Optional[int] = None,
	) -> dict[str, Any]:
		"""Fetch Shop Board action card context bundle for one or more action items or a Repair Order."""
		return self._call_board(
			"get_action_cards_context",
			action_items=action_items,
			repair_order=repair_order,
			status=status,
			include_events_limit=include_events_limit,
			limit=limit,
		)

	def list_board_threads(
		self,
		status: str = "OPEN",
		assigned_only: bool = False,
		query: Optional[str] = None,
		limit: int = 50,
	) -> dict[str, Any]:
		"""List active Repair Order Shop Board threads filtered by status, assignment, or search query."""
		return self._call_board(
			"list_board_threads",
			status=status,
			assigned_only=bool(assigned_only),
			query=query,
			limit=limit,
		)

	def get_board_thread(
		self,
		thread_key: Optional[str] = None,
		repair_order: Optional[str] = None,
		limit: int = 100,
		before_event: Optional[str] = None,
	) -> dict[str, Any]:
		"""Fetch an active Repair Order Shop Board thread header, events, and action cards."""
		return self._call_board(
			"get_board_thread",
			thread_key=thread_key,
			repair_order=repair_order,
			limit=limit,
			before_event=before_event,
		)

	def post_board_capture(
		self,
		thread_key: Optional[str] = None,
		repair_order: Optional[str] = None,
		text: Optional[str] = None,
		intent_tag: Optional[str] = None,
		metadata: Optional[dict[str, Any] | str] = None,
		idempotency_key: Optional[str] = None,
		skip_ai: bool = True,
	) -> dict[str, Any]:
		"""Post a text/artifact capture event onto an active Repair Order Shop Board thread."""
		effective_metadata: Optional[dict[str, Any] | str] = metadata
		if skip_ai:
			if effective_metadata is None:
				effective_metadata = {"skip_ai": True}
			elif isinstance(effective_metadata, dict):
				effective_metadata = {**effective_metadata, "skip_ai": True}
			elif isinstance(effective_metadata, str):
				try:
					parsed = json.loads(effective_metadata)
				except json.JSONDecodeError:
					parsed = None
				if isinstance(parsed, dict):
					effective_metadata = {**parsed, "skip_ai": True}
		return self._call_board(
			"post_board_capture",
			thread_key=thread_key,
			repair_order=repair_order,
			text=text,
			intent_tag=intent_tag,
			metadata=effective_metadata,
			idempotency_key=idempotency_key,
		)

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

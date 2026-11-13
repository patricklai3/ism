"""High-level Python client for Frappe sites running ISM."""

from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from .config import IsmConfig
from .transport import FrappeTransport, IsmApiError


class IsmAgentClient:
	"""High-level client wrapping ISM Agent Facade and Frappe MCP/REST APIs."""

	def __init__(self, config: Optional[IsmConfig] = None) -> None:
		self.config = config or IsmConfig.from_env()
		self._tx = FrappeTransport(self.config)

	# --- Curated ISM Agent Facade Methods ---

	def create_quote(
		self,
		customer_name: str,
		vehicle_identifier: str,
		services: Optional[list[Any] | str] = None,
		parts: Optional[list[Any] | str] = None,
		notes: Optional[str] = None,
		customer_phone: Optional[str] = None,
		customer_email: Optional[str] = None,
		company: Optional[str] = None,
		odometer_in: Optional[int] = None,
	) -> dict[str, Any]:
		"""Create a Draft Estimate Repair Order with deterministic totals."""
		args = _drop_none({
			"customer_name": customer_name,
			"vehicle_identifier": vehicle_identifier,
			"company": company or self.config.company or None,
			"services": services,
			"parts": parts,
			"notes": notes,
			"customer_phone": customer_phone,
			"customer_email": customer_email,
			"odometer_in": odometer_in,
		})
		return self.call_method("ism.api.agent.create_quote", args)

	def amend_quote(
		self,
		repair_order: str,
		add_services: Optional[list[Any] | str] = None,
		remove_service_indices: Optional[list[int] | str] = None,
		notes: Optional[str] = None,
	) -> dict[str, Any]:
		"""Amend services or notes on an unapproved Repair Order."""
		args = _drop_none({
			"repair_order": repair_order,
			"add_services": add_services,
			"remove_service_indices": remove_service_indices,
			"notes": notes,
		})
		return self.call_method("ism.api.agent.amend_quote", args)

	def find_available_slots(
		self,
		repair_order: Optional[str] = None,
		duration_minutes: Optional[int] = None,
		start_date: Optional[str] = None,
		days_ahead: int = 5,
		equipment_tags: Optional[list[str] | str] = None,
	) -> dict[str, Any]:
		"""Query dual-resource capacity for available bay and tech slots."""
		args = _drop_none({
			"repair_order": repair_order,
			"duration_minutes": duration_minutes,
			"start_date": start_date,
			"days_ahead": days_ahead,
			"equipment_tags": equipment_tags,
		})
		return self.call_method("ism.api.agent.find_available_slots", args)

	def book_appointment(
		self,
		repair_order: str,
		scheduled_date: str,
		start_time: str,
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
		notes: Optional[str] = None,
	) -> dict[str, Any]:
		"""Allocate a Schedule Entry and push realtime calendar update."""
		args = _drop_none({
			"repair_order": repair_order,
			"scheduled_date": scheduled_date,
			"start_time": start_time,
			"service_bay": service_bay,
			"technician": technician,
			"notes": notes,
		})
		return self.call_method("ism.api.agent.book_appointment", args)

	def send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: Optional[str] = None,
	) -> dict[str, Any]:
		"""Generate approval token and optionally send SMS link."""
		args = _drop_none({
			"repair_order": repair_order,
			"communication_medium": communication_medium,
			"phone_number": phone_number,
		})
		return self.call_method("ism.api.agent.send_quote_for_approval", args)

	def import_supplier_catalog(
		self,
		raw_text: str,
		supplier: Optional[str] = None,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
	) -> dict[str, Any]:
		"""Bulk ingest delimited supplier catalog text into Item master."""
		args = _drop_none({
			"raw_text": raw_text,
			"supplier": supplier,
			"default_source": default_source,
			"default_condition": default_condition,
		})
		return self.call_method("ism.api.agent.import_supplier_catalog", args)

	def import_tesla_procedure(
		self,
		procedure_title: str,
		correction_code: Optional[str] = None,
		labor_hours: float = 1.0,
		model: str = "Model 3",
		parts: Optional[list[Any] | str] = None,
	) -> dict[str, Any]:
		"""Save a Tesla service manual procedure as a reusable Service Package."""
		args = _drop_none({
			"procedure_title": procedure_title,
			"correction_code": correction_code,
			"labor_hours": labor_hours,
			"model": model,
			"parts": parts,
		})
		return self.call_method("ism.api.agent.import_tesla_procedure", args)

	def get_shop_floor_status(self, date: Optional[str] = None) -> dict[str, Any]:
		"""Retrieve active bays, on-duty technicians, appointments, and ROs."""
		return self.call_method("ism.api.agent.get_shop_floor_status", _drop_none({"date": date}))

	def update_job_status(
		self,
		repair_order: str,
		action: str,
		odometer_out: Optional[int] = None,
		notes: Optional[str] = None,
	) -> dict[str, Any]:
		"""Transition Repair Order status (start, hold, complete) with guardrails."""
		args = _drop_none({
			"repair_order": repair_order,
			"action": action,
			"odometer_out": odometer_out,
			"notes": notes,
		})
		return self.call_method("ism.api.agent.update_job_status", args)

	# --- Extended ISM Shop APIs ---

	def get_parts_stock(self, ro_name: str, company: Optional[str] = None) -> dict[str, Any]:
		args = _drop_none({"ro_name": ro_name, "company": company or self.config.company or None})
		return self.call_method("ism.api.parts.get_repair_order_parts_stock", args)

	def submit_procurement_orders(self, repair_order: str, parts_data: list[dict[str, Any]]) -> dict[str, Any]:
		return self.call_method(
			"ism.api.parts.submit_procurement_orders",
			{"repair_order": repair_order, "parts_data": parts_data},
		)

	def fetch_tesla_procedure(self, url: str) -> dict[str, Any]:
		return self.call_method("ism.api.tesla.fetch_and_parse_tesla_procedure", {"url": url})

	def get_daily_tech_overview(self, date: str) -> dict[str, Any]:
		return self.call_method("ism.api.technician_availability.get_daily_technician_overview", {"date": date})

	def get_unscheduled_services(self, repair_order: str) -> Any:
		return self.call_method("ism.api.scheduling.get_unscheduled_services", {"repair_order": repair_order})

	# --- Generic Frappe Operations ---

	def call_method(self, method: str, args: Optional[dict[str, Any]] = None) -> Any:
		"""Execute a whitelisted Frappe method via MCP with automatic REST fallback."""
		params = args or {}
		return self._with_mcp_fallback(
			lambda: self._tx.call_mcp_tool("call_method", {"method": method, "args": params}),
			lambda: self._tx.call_rest_method(method, params),
		)

	def list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]] = None,
		fields: Optional[list[str]] = None,
		limit: int = 20,
		order_by: Optional[str] = None,
	) -> Any:
		args = _drop_none({
			"doctype": doctype,
			"filters": filters,
			"fields": fields,
			"limit_page_length": limit,
			"order_by": order_by,
		})
		return self._with_mcp_fallback(
			lambda: self._tx.call_mcp_tool("list_documents", args),
			lambda: self._tx.rest_list_documents(doctype, filters, fields, limit, order_by),
		)

	def get_document(self, doctype: str, name: str) -> Any:
		encoded_dt = urllib.parse.quote(doctype, safe="")
		encoded_name = urllib.parse.quote(name, safe="")
		return self._with_mcp_fallback(
			lambda: self._tx.call_mcp_tool("get_document", {"doctype": doctype, "name": name}),
			lambda: self._tx.request_json(f"{self.config.base_url}/api/resource/{encoded_dt}/{encoded_name}", "GET").get("data"),
		)

	def create_document(self, doctype: str, data: dict[str, Any]) -> Any:
		encoded_dt = urllib.parse.quote(doctype, safe="")
		return self._with_mcp_fallback(
			lambda: self._tx.call_mcp_tool("create_document", {"doctype": doctype, "data": data}),
			lambda: self._tx.request_json(f"{self.config.base_url}/api/resource/{encoded_dt}", "POST", data).get("data"),
		)

	def update_document(self, doctype: str, name: str, data: dict[str, Any]) -> Any:
		encoded_dt = urllib.parse.quote(doctype, safe="")
		encoded_name = urllib.parse.quote(name, safe="")
		return self._with_mcp_fallback(
			lambda: self._tx.call_mcp_tool("update_document", {"doctype": doctype, "name": name, "data": data}),
			lambda: self._tx.request_json(f"{self.config.base_url}/api/resource/{encoded_dt}/{encoded_name}", "PUT", data).get("data"),
		)

	# --- Native Frappe MCP Protocol Helpers ---

	def ping_mcp(self) -> dict[str, Any]:
		return self._tx.post_mcp("ping")

	def initialize_mcp(self) -> dict[str, Any]:
		return self._tx.post_mcp(
			"initialize",
			{
				"protocolVersion": "2024-11-05",
				"capabilities": {},
				"clientInfo": {"name": "ism-agent-os", "version": "1.0.0"},
			},
		)

	def list_native_mcp_tools(self) -> list[dict[str, Any]]:
		res = self._tx.post_mcp("tools/list")
		return res.get("result", {}).get("tools", [])

	def call_native_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
		return self._tx.call_mcp_tool(tool_name, arguments)

	def _with_mcp_fallback(self, mcp_fn, rest_fn) -> Any:
		if self._tx.should_use_mcp():
			try:
				return mcp_fn()
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._tx.mcp_available = False
		return rest_fn()


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
	return {k: v for k, v in data.items() if v is not None}

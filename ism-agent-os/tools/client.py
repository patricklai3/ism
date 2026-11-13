"""Zero-dependency HTTP/MCP client for Frappe sites running ISM."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .config import IsmConfig


class IsmApiError(Exception):
	"""Raised when Frappe or MCP returns an API or transport error."""

	def __init__(self, message: str, status_code: int = 500, details: Any = None) -> None:
		super().__init__(message)
		self.status_code = status_code
		self.details = details


class IsmAgentClient:
	"""High-level client wrapping ISM Agent Facade and Frappe MCP/REST APIs."""

	def __init__(self, config: Optional[IsmConfig] = None) -> None:
		self.config = config or IsmConfig.from_env()
		self._mcp_available: Optional[bool] = None
		self._req_counter = 0

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
		args: dict[str, Any] = {
			"customer_name": customer_name,
			"vehicle_identifier": vehicle_identifier,
			"company": company or self.config.company or None,
		}
		if services is not None:
			args["services"] = services
		if parts is not None:
			args["parts"] = parts
		if notes is not None:
			args["notes"] = notes
		if customer_phone is not None:
			args["customer_phone"] = customer_phone
		if customer_email is not None:
			args["customer_email"] = customer_email
		if odometer_in is not None:
			args["odometer_in"] = odometer_in
		return self.call_method("ism.api.agent.create_quote", _drop_none(args))

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
		args = _drop_none({"date": date})
		return self.call_method("ism.api.agent.get_shop_floor_status", args)

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
		if self._should_use_mcp():
			try:
				return self.call_native_mcp_tool("call_method", {"method": method, "args": params})
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._mcp_available = False
		return self._call_rest_method(method, params)

	def list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]] = None,
		fields: Optional[list[str]] = None,
		limit: int = 20,
		order_by: Optional[str] = None,
	) -> Any:
		if self._should_use_mcp():
			try:
				args = _drop_none({
					"doctype": doctype,
					"filters": filters,
					"fields": fields,
					"limit_page_length": limit,
					"order_by": order_by,
				})
				return self.call_native_mcp_tool("list_documents", args)
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._mcp_available = False
		return self._rest_list_documents(doctype, filters, fields, limit, order_by)

	def get_document(self, doctype: str, name: str) -> Any:
		if self._should_use_mcp():
			try:
				return self.call_native_mcp_tool("get_document", {"doctype": doctype, "name": name})
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._mcp_available = False
		encoded_dt = urllib.parse.quote(doctype, safe="")
		encoded_name = urllib.parse.quote(name, safe="")
		res = self._request_json(f"{self.config.base_url}/api/resource/{encoded_dt}/{encoded_name}", "GET")
		return res.get("data", res)

	def create_document(self, doctype: str, data: dict[str, Any]) -> Any:
		if self._should_use_mcp():
			try:
				return self.call_native_mcp_tool("create_document", {"doctype": doctype, "data": data})
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._mcp_available = False
		encoded_dt = urllib.parse.quote(doctype, safe="")
		res = self._request_json(f"{self.config.base_url}/api/resource/{encoded_dt}", "POST", data)
		return res.get("data", res)

	def update_document(self, doctype: str, name: str, data: dict[str, Any]) -> Any:
		if self._should_use_mcp():
			try:
				return self.call_native_mcp_tool("update_document", {"doctype": doctype, "name": name, "data": data})
			except IsmApiError as exc:
				if exc.status_code not in (404, 405, 501) or self.config.transport == "mcp":
					raise
				self._mcp_available = False
		encoded_dt = urllib.parse.quote(doctype, safe="")
		encoded_name = urllib.parse.quote(name, safe="")
		res = self._request_json(f"{self.config.base_url}/api/resource/{encoded_dt}/{encoded_name}", "PUT", data)
		return res.get("data", res)

	# --- Native Frappe MCP Protocol Helpers ---

	def ping_mcp(self) -> dict[str, Any]:
		return self._post_mcp("ping")

	def initialize_mcp(self) -> dict[str, Any]:
		return self._post_mcp(
			"initialize",
			{
				"protocolVersion": "2024-11-05",
				"capabilities": {},
				"clientInfo": {"name": "ism-agent-os", "version": "1.0.0"},
			},
		)

	def list_native_mcp_tools(self) -> list[dict[str, Any]]:
		res = self._post_mcp("tools/list")
		return res.get("result", {}).get("tools", [])

	def call_native_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
		res = self._post_mcp("tools/call", {"name": tool_name, "arguments": arguments})
		result = res.get("result", {})
		if result.get("isError"):
			msg = _extract_mcp_error_text(result)
			raise IsmApiError(msg, status_code=400, details=result)
		if "structuredContent" in result and isinstance(result["structuredContent"], dict):
			sc = result["structuredContent"]
			return sc.get("result", sc)
		return _extract_mcp_text_or_json(result)

	# --- Internal Transport Utilities ---

	def _should_use_mcp(self) -> bool:
		if self.config.transport == "rest":
			return False
		if self.config.transport == "mcp":
			return True
		return self._mcp_available is not False

	def _post_mcp(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		self._req_counter += 1
		payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._req_counter, "method": method}
		if params is not None:
			payload["params"] = params
		body = self._request_json(self.config.mcp_endpoint, "POST", payload)
		if isinstance(body, dict) and "error" in body:
			err = body["error"]
			msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
			raise IsmApiError(msg, status_code=400, details=err)
		self._mcp_available = True
		return body

	def _call_rest_method(self, method: str, args: dict[str, Any]) -> Any:
		res = self._request_json(f"{self.config.base_url}/api/method/{method}", "POST", args)
		if isinstance(res, dict) and "message" in res:
			return res["message"]
		return res

	def _rest_list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]],
		fields: Optional[list[str]],
		limit: int,
		order_by: Optional[str],
	) -> Any:
		query: dict[str, str] = {"limit_page_length": str(limit)}
		if filters is not None:
			query["filters"] = json.dumps(filters)
		if fields is not None:
			query["fields"] = json.dumps(fields)
		if order_by:
			query["order_by"] = order_by
		encoded_dt = urllib.parse.quote(doctype, safe="")
		qs = urllib.parse.urlencode(query)
		res = self._request_json(f"{self.config.base_url}/api/resource/{encoded_dt}?{qs}", "GET")
		return res.get("data", res)

	def _request_json(self, url: str, http_method: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		headers = {"Accept": "application/json", "Content-Type": "application/json"}
		if self.config.auth_header:
			headers["Authorization"] = self.config.auth_header
		if self.config.site:
			headers["X-Frappe-Site-Name"] = self.config.site

		data = json.dumps(body).encode("utf-8") if body is not None else None
		req = urllib.request.Request(url, data=data, headers=headers, method=http_method)
		try:
			with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
				raw = resp.read().decode("utf-8")
				return json.loads(raw) if raw else {}
		except urllib.error.HTTPError as exc:
			raw_err = exc.read().decode("utf-8", errors="replace")
			raise IsmApiError(_format_http_error(exc.code, raw_err), status_code=exc.code, details=raw_err) from exc
		except urllib.error.URLError as exc:
			raise IsmApiError(f"Connection failed to {url}: {exc.reason}", status_code=503) from exc


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
	return {k: v for k, v in data.items() if v is not None}


def _extract_mcp_error_text(result: dict[str, Any]) -> str:
	texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("text")]
	return "\n".join(texts) or "MCP tool execution failed."


def _extract_mcp_text_or_json(result: dict[str, Any]) -> Any:
	texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("text")]
	joined = "\n".join(texts)
	if not joined:
		return result
	try:
		return json.loads(joined)
	except Exception:
		return joined


def _format_http_error(status_code: int, raw_body: str) -> str:
	try:
		parsed = json.loads(raw_body)
		if isinstance(parsed, dict):
			if parsed.get("_server_messages"):
				msgs = json.loads(parsed["_server_messages"])
				extracted = [json.loads(m).get("message", m) if isinstance(m, str) else str(m) for m in msgs]
				return f"HTTP {status_code}: " + "; ".join(extracted)
			if parsed.get("exception"):
				return f"HTTP {status_code}: {parsed['exception']}"
			if parsed.get("message"):
				return f"HTTP {status_code}: {parsed['message']}"
	except Exception:
		pass
	return f"HTTP {status_code}: {raw_body[:300]}"

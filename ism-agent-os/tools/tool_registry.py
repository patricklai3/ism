"""Declarative MCP tool schemas and dispatch mapping for ISM Agent OS."""

from __future__ import annotations

from typing import Any

from .client import IsmAgentClient, IsmApiError


def get_mcp_tools() -> list[dict[str, Any]]:
	"""Return MCP-compliant tool definitions (without internal dispatch keys)."""
	return [
		{
			"name": spec["name"],
			"description": spec["description"],
			"inputSchema": spec["inputSchema"],
		}
		for spec in _TOOL_SPECS
	]


def dispatch_tool(client: IsmAgentClient, name: str, arguments: dict[str, Any]) -> Any:
	"""Validate arguments and route an MCP tool call to IsmAgentClient."""
	spec = _TOOL_INDEX.get(name)
	if not spec:
		raise IsmApiError(f"Unknown tool: {name}", status_code=404)

	args = arguments or {}
	schema = spec["inputSchema"]
	for req_key in schema.get("required", []):
		if req_key not in args or args[req_key] in (None, ""):
			raise IsmApiError(f"Missing required argument '{req_key}' for {name}.", status_code=400)

	allowed = set(schema.get("properties", {}).keys())
	filtered = {k: v for k, v in args.items() if k in allowed}
	fn = getattr(client, spec["client_method"])
	return fn(**filtered)


_TOOL_SPECS: list[dict[str, Any]] = [
	{
		"name": "ism_create_quote",
		"client_method": "create_quote",
		"description": (
			"Create a Repair Order in Draft Estimate state. Resolves or creates provisional "
			"Customer and Repair Vehicle records, calculates Fenton-Wilkinson duration and 13% HST "
			"on the server, and returns a Markdown summary card with Desk and Print URLs."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"customer_name": {"type": "string", "description": "Customer full name or Customer ID."},
				"vehicle_identifier": {"type": "string", "description": "17-character VIN or license plate."},
				"services": {
					"type": "array",
					"description": "Service operations or Service Package names/objects.",
					"items": {
						"type": ["string", "object"],
						"properties": {
							"service_title": {"type": "string"},
							"service_package": {"type": "string"},
							"labor_hours": {"type": "number"},
							"labor_rate": {"type": "number"},
							"is_flat_rate": {"type": "integer"},
							"service_position": {"type": "string", "enum": ["None", "LH", "RH", "Front", "Rear"]},
							"parts": {"type": "array", "items": {"type": "object"}},
						},
					},
				},
				"parts": {
					"type": "array",
					"description": "Optional parts list attached to the first service operation.",
					"items": {"type": "object"},
				},
				"notes": {"type": "string", "description": "Customer concern or diagnostic notes."},
				"customer_phone": {"type": "string", "description": "Customer phone number for SMS approval."},
				"customer_email": {"type": "string", "description": "Customer email address."},
				"company": {"type": "string", "description": "Optional Frappe Company override."},
				"odometer_in": {"type": "integer", "description": "Current vehicle odometer reading."},
			},
			"required": ["customer_name", "vehicle_identifier"],
		},
	},
	{
		"name": "ism_amend_quote",
		"client_method": "amend_quote",
		"description": (
			"Modify lines or notes on an unapproved Repair Order in Draft Estimate state. "
			"Blocked automatically if the customer already approved or submitted the order."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order document ID (e.g. RO-2026-00042)."},
				"add_services": {
					"type": "array",
					"description": "List of service titles or operation objects to append.",
					"items": {"type": ["string", "object"]},
				},
				"remove_service_indices": {
					"type": "array",
					"description": "Zero-based service row indices to remove.",
					"items": {"type": "integer"},
				},
				"notes": {"type": "string", "description": "Updated customer concern or advisor notes."},
			},
			"required": ["repair_order"],
		},
	},
	{
		"name": "ism_send_quote_for_approval",
		"client_method": "send_quote_for_approval",
		"description": (
			"Generate a 48-hour secure customer approval link for a Repair Order and "
			"optionally dispatch it via SMS."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID."},
				"communication_medium": {
					"type": "string",
					"enum": ["sms", "email", "link"],
					"description": "Delivery channel (default: sms).",
				},
				"phone_number": {"type": "string", "description": "Override customer mobile number."},
			},
			"required": ["repair_order"],
		},
	},
	{
		"name": "ism_find_available_slots",
		"client_method": "find_available_slots",
		"description": (
			"Query dual-resource capacity (service bays, equipment tags, and active technicians) "
			"to find available booking slots across shop operating hours."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID to auto-estimate duration."},
				"duration_minutes": {"type": "integer", "description": "Explicit duration override in minutes."},
				"start_date": {"type": "string", "description": "Start date (YYYY-MM-DD). Defaults to today."},
				"days_ahead": {"type": "integer", "description": "Number of days to search (default: 5)."},
				"equipment_tags": {
					"type": "array",
					"items": {"type": "string"},
					"description": "Required bay equipment tags (e.g. Alignment Rack, EV Lift).",
				},
			},
		},
	},
	{
		"name": "ism_book_appointment",
		"client_method": "book_appointment",
		"description": (
			"Allocate a Schedule Entry for a Repair Order, prevent bay and technician conflicts, "
			"and broadcast a realtime update to the ambient shop calendar."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID."},
				"scheduled_date": {"type": "string", "description": "Appointment date (YYYY-MM-DD)."},
				"start_time": {"type": "string", "description": "Start time (HH:MM or HH:MM:SS)."},
				"service_bay": {"type": "string", "description": "Optional explicit Service Bay name."},
				"technician": {"type": "string", "description": "Optional Employee ID for assigned technician."},
				"notes": {"type": "string", "description": "Schedule entry notes."},
			},
			"required": ["repair_order", "scheduled_date", "start_time"],
		},
	},
	{
		"name": "ism_get_shop_floor_status",
		"client_method": "get_shop_floor_status",
		"description": (
			"Return a live operational summary of active bays, on-duty technicians, "
			"scheduled appointments, and active shop Repair Orders."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"date": {"type": "string", "description": "Target date (YYYY-MM-DD). Defaults to today."},
			},
		},
	},
	{
		"name": "ism_update_job_status",
		"client_method": "update_job_status",
		"description": (
			"Transition a Repair Order job state on the shop floor. 'start' enforces the "
			"customer authorization lock; 'hold' flags Waiting on Parts; 'complete' marks "
			"Work Completed and records odometer_out."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID."},
				"action": {
					"type": "string",
					"enum": ["start", "hold", "complete"],
					"description": "Floor action to perform.",
				},
				"odometer_out": {"type": "integer", "description": "Final odometer when completing work."},
				"notes": {"type": "string", "description": "Technician or override notes."},
			},
			"required": ["repair_order", "action"],
		},
	},
	{
		"name": "ism_import_supplier_catalog",
		"client_method": "import_supplier_catalog",
		"description": (
			"Bulk ingest CSV, TSV, or pipe-delimited supplier parts text into the Frappe Item catalog. "
			"Expected columns: Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"raw_text": {"type": "string", "description": "Delimited catalog rows."},
				"supplier": {"type": "string", "description": "Optional Supplier name."},
				"default_source": {"type": "string", "description": "Default source (OEM or Aftermarket)."},
				"default_condition": {"type": "string", "description": "Default condition (New, Used, Reman)."},
			},
			"required": ["raw_text"],
		},
	},
	{
		"name": "ism_import_tesla_procedure",
		"client_method": "import_tesla_procedure",
		"description": (
			"Create or update a reusable Service Package from a Tesla Service Manual procedure "
			"with correction code, flat-rate labor hours, model, and required parts."
		),
		"inputSchema": {
			"type": "object",
			"properties": {
				"procedure_title": {"type": "string", "description": "Procedure title."},
				"correction_code": {"type": "string", "description": "Tesla correction code (e.g. 31011402)."},
				"labor_hours": {"type": "number", "description": "Flat-rate labor time in hours."},
				"model": {"type": "string", "description": "Applicable vehicle model (e.g. Model 3, Model Y)."},
				"parts": {"type": "array", "items": {"type": "object"}, "description": "Required OEM parts."},
			},
			"required": ["procedure_title"],
		},
	},
	{
		"name": "ism_check_parts_stock",
		"client_method": "get_parts_stock",
		"description": "Check live stock availability, committed quantities, and parts deficits for a Repair Order.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"ro_name": {"type": "string", "description": "Repair Order ID."},
				"company": {"type": "string", "description": "Optional Company override."},
			},
			"required": ["ro_name"],
		},
	},
	{
		"name": "ism_submit_procurement_orders",
		"client_method": "submit_procurement_orders",
		"description": "Create and submit Purchase Orders or RFQs grouped by supplier for Repair Order parts.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID."},
				"parts_data": {
					"type": "array",
					"description": "List of parts dicts with item_code, qty, supplier, order_mode.",
					"items": {"type": "object"},
				},
			},
			"required": ["repair_order", "parts_data"],
		},
	},
	{
		"name": "ism_fetch_tesla_manual",
		"client_method": "fetch_tesla_procedure",
		"description": "Fetch and parse a service.tesla.com procedure URL server-side into structured metadata.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "Full https://service.tesla.com/... procedure URL."},
			},
			"required": ["url"],
		},
	},
	{
		"name": "ism_get_tech_overview",
		"client_method": "get_daily_tech_overview",
		"description": "Retrieve technician daily utilization, scheduled hours, and unassigned entries.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"date": {"type": "string", "description": "Target date (YYYY-MM-DD)."},
			},
			"required": ["date"],
		},
	},
	{
		"name": "ism_get_unscheduled_services",
		"client_method": "get_unscheduled_services",
		"description": "Return service rows on a Repair Order not yet allocated to an active Schedule Entry.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"repair_order": {"type": "string", "description": "Repair Order ID."},
			},
			"required": ["repair_order"],
		},
	},
	{
		"name": "frappe_list_documents",
		"client_method": "list_documents",
		"description": "Query any Frappe or ISM DocType list with filters, fields, and limits.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string", "description": "Frappe DocType name (e.g. Repair Order)."},
				"filters": {"type": ["object", "array"], "description": "Frappe filter dict or list."},
				"fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to return."},
				"limit": {"type": "integer", "description": "Max documents to return (default: 20)."},
				"order_by": {"type": "string", "description": "Sort clause (e.g. modified desc)."},
			},
			"required": ["doctype"],
		},
	},
	{
		"name": "frappe_get_document",
		"client_method": "get_document",
		"description": "Fetch a single Frappe document by DocType and name.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string"},
				"name": {"type": "string"},
			},
			"required": ["doctype", "name"],
		},
	},
	{
		"name": "frappe_create_document",
		"client_method": "create_document",
		"description": "Create a Frappe document directly when no high-level ism_* tool covers the DocType.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string"},
				"data": {"type": "object"},
			},
			"required": ["doctype", "data"],
		},
	},
	{
		"name": "frappe_update_document",
		"client_method": "update_document",
		"description": "Update specific fields on an existing Frappe document.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string"},
				"name": {"type": "string"},
				"data": {"type": "object"},
			},
			"required": ["doctype", "name", "data"],
		},
	},
	{
		"name": "frappe_call_method",
		"client_method": "call_method",
		"description": "Call any whitelisted Frappe Python method by dotted path.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"method": {"type": "string", "description": "Dotted method path (e.g. ism.api.parts.get_component_alternates)."},
				"args": {"type": "object", "description": "Keyword arguments passed to the method."},
			},
			"required": ["method"],
		},
	},
]

_TOOL_INDEX: dict[str, dict[str, Any]] = {spec["name"]: spec for spec in _TOOL_SPECS}

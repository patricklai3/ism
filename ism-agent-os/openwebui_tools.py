"""Open WebUI Tool adapter exposing ISM Agent OS shop operations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from tools.client import IsmAgentClient  # noqa: E402
from tools.config import IsmConfig  # noqa: E402

try:
	from pydantic import BaseModel, Field
except ImportError:
	class BaseModel:  # type: ignore[no-redef]
		def __init__(self, **kwargs: Any) -> None:
			for k, v in kwargs.items():
				setattr(self, k, v)

	def Field(default: Any = None, **_kwargs: Any) -> Any:  # type: ignore[no-redef]
		return default


class Tools:
	"""Open WebUI Tools class acting as the OS for a Frappe + ISM site."""

	class Valves(BaseModel):
		FRAPPE_URL: str = Field(default="http://localhost:8000", description="Frappe site base URL")
		FRAPPE_API_KEY: str = Field(default="", description="Service Advisor API Key")
		FRAPPE_API_SECRET: str = Field(default="", description="Service Advisor API Secret")
		FRAPPE_SITE: str = Field(default="", description="Optional multi-tenant site header")

	def __init__(self) -> None:
		self.valves = self.Valves()

	def _client(self) -> IsmAgentClient:
		cfg = IsmConfig.from_env(
			overrides={
				"FRAPPE_URL": self.valves.FRAPPE_URL,
				"FRAPPE_API_KEY": self.valves.FRAPPE_API_KEY,
				"FRAPPE_API_SECRET": self.valves.FRAPPE_API_SECRET,
				"FRAPPE_SITE": self.valves.FRAPPE_SITE,
			}
		)
		return IsmAgentClient(cfg)

	def ism_create_quote(
		self,
		customer_name: str,
		vehicle_identifier: str,
		services_json: str = "[]",
		notes: str = "",
		customer_phone: str = "",
	) -> str:
		"""Create a Draft Estimate Repair Order with server-computed math and 13% HST."""
		services = json.loads(services_json) if services_json else []
		res = self._client().create_quote(
			customer_name=customer_name,
			vehicle_identifier=vehicle_identifier,
			services=services or None,
			notes=notes or None,
			customer_phone=customer_phone or None,
		)
		return _format_response(res)

	def ism_amend_quote(
		self,
		repair_order: str,
		add_services_json: str = "[]",
		remove_indices_json: str = "[]",
		notes: Optional[str] = None,
	) -> str:
		"""Amend lines or notes on an unapproved Repair Order."""
		add_services = json.loads(add_services_json) if add_services_json else None
		remove_indices = json.loads(remove_indices_json) if remove_indices_json else None
		res = self._client().amend_quote(
			repair_order=repair_order,
			add_services=add_services,
			remove_service_indices=remove_indices,
			notes=notes,
		)
		return _format_response(res)

	def ism_send_quote_for_approval(
		self,
		repair_order: str,
		communication_medium: str = "sms",
		phone_number: Optional[str] = None,
	) -> str:
		"""Generate a 48-hour customer approval link and optionally dispatch SMS."""
		res = self._client().send_quote_for_approval(
			repair_order=repair_order,
			communication_medium=communication_medium,
			phone_number=phone_number,
		)
		return _format_response(res)

	def ism_find_available_slots(
		self,
		repair_order: Optional[str] = None,
		duration_minutes: Optional[int] = None,
		start_date: Optional[str] = None,
		days_ahead: int = 5,
	) -> str:
		"""Query dual-resource capacity for available service bay and technician slots."""
		res = self._client().find_available_slots(
			repair_order=repair_order,
			duration_minutes=duration_minutes,
			start_date=start_date,
			days_ahead=days_ahead,
		)
		return _format_response(res)

	def ism_book_appointment(
		self,
		repair_order: str,
		scheduled_date: str,
		start_time: str,
		service_bay: Optional[str] = None,
		technician: Optional[str] = None,
	) -> str:
		"""Book a Schedule Entry and push realtime ambient wallboard update."""
		res = self._client().book_appointment(
			repair_order=repair_order,
			scheduled_date=scheduled_date,
			start_time=start_time,
			service_bay=service_bay,
			technician=technician,
		)
		return _format_response(res)

	def ism_get_shop_floor_status(self, date: Optional[str] = None) -> str:
		"""Get live bay occupancy, active technicians, and active shop Repair Orders."""
		return _format_response(self._client().get_shop_floor_status(date=date))

	def ism_update_job_status(
		self,
		repair_order: str,
		action: str,
		odometer_out: Optional[int] = None,
		notes: Optional[str] = None,
	) -> str:
		"""Transition Repair Order job status ('start', 'hold', or 'complete')."""
		res = self._client().update_job_status(
			repair_order=repair_order,
			action=action,
			odometer_out=odometer_out,
			notes=notes,
		)
		return _format_response(res)

	def ism_import_supplier_catalog(
		self,
		raw_text: str,
		supplier: Optional[str] = None,
		default_source: str = "Aftermarket",
	) -> str:
		"""Bulk import delimited supplier catalog text into Frappe Item master."""
		res = self._client().import_supplier_catalog(
			raw_text=raw_text,
			supplier=supplier,
			default_source=default_source,
		)
		return _format_response(res)

	def ism_import_tesla_procedure(
		self,
		procedure_title: str,
		correction_code: Optional[str] = None,
		labor_hours: float = 1.0,
		model: str = "Model 3",
	) -> str:
		"""Save a Tesla procedure into a reusable ISM Service Package."""
		res = self._client().import_tesla_procedure(
			procedure_title=procedure_title,
			correction_code=correction_code,
			labor_hours=labor_hours,
			model=model,
		)
		return _format_response(res)


def _format_response(res: Any) -> str:
	if isinstance(res, dict) and res.get("summary_card"):
		return str(res["summary_card"])
	return json.dumps(res, indent=2, default=str)

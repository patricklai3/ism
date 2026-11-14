"""Parse Tesla service manual URLs, HTML, or Markdown into ISM Service Packages."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

if __package__ and __package__.startswith("ism_agent_os"):
	from ..client import IsmAgentClient
else:
	try:
		from tools.client import IsmAgentClient
	except ImportError:
		from ism_agent_os.client import IsmAgentClient

try:
	from ism.utils.tesla_manual import (
		detect_service_side as _shared_detect_service_side,
		extract_oem_parts_from_text as _shared_extract_oem_parts,
		normalize_tesla_procedure as _shared_normalize_procedure,
		parse_tesla_manual_url as _shared_parse_manual_url,
		parse_tesla_procedure_html as _shared_parse_html,
		parse_tesla_procedure_markdown as _shared_parse_markdown,
	)
except ImportError:  # Standalone ism-agent-os runtime without ISM backend on sys.path
	_shared_detect_service_side = None
	_shared_extract_oem_parts = None
	_shared_normalize_procedure = None
	_shared_parse_manual_url = None
	_shared_parse_html = None
	_shared_parse_markdown = None

_MODEL_MAP = {
	"model3": "Model 3",
	"modely": "Model Y",
	"models": "Model S",
	"modelx": "Model X",
	"cybertruck": "Cybertruck",
}

_OEM_PART_RE = re.compile(r"\b(\d{7}-\d{2}-[A-Z])\b")


class TeslaManualParser:
	"""Extracts unified Tesla procedure metadata and persists Service Packages via ISM."""

	def __init__(self, client: Optional[IsmAgentClient] = None) -> None:
		self.client = client

	def ingest_from_url(
		self,
		url: str,
		model_override: Optional[str] = None,
		parts: Optional[list[dict[str, Any]]] = None,
		repair_order: Optional[str] = None,
		on_conflict: str = "skip",
		force: bool = False,
		side: Optional[str] = None,
		is_supplement: int = 0,
		advisor_notes: Optional[str] = None,
		include_summary: bool = False,
	) -> dict[str, Any]:
		"""Fetch procedure on the Frappe backend and create or update a Service Package."""
		if not self.client:
			raise ValueError("IsmAgentClient is required for remote ingestion.")
		_validate_tesla_url(url)

		parsed = self.client.fetch_tesla_procedure(url, repair_order=repair_order)
		normalized = self.normalize_procedure(parsed, url=url)
		title = normalized.get("title") or "Tesla Service Procedure"
		code = normalized.get("correction_code") or None
		frt = float(normalized.get("frt") or 1.0)
		model = model_override or normalized.get("applicable_model") or "Model 3"
		resolved_parts = parts if parts is not None else (normalized.get("parts") or None)

		return self.client.import_tesla_procedure(
			procedure_title=title,
			correction_code=code,
			labor_hours=frt,
			model=model,
			parts=resolved_parts,
			repair_order=repair_order,
			url=url if repair_order else None,
			data=normalized if repair_order else None,
			on_conflict=on_conflict,
			force=force,
			side=side,
			is_supplement=is_supplement,
			advisor_notes=advisor_notes,
			include_summary=include_summary,
		)

	def parse_url_metadata(self, url: str) -> dict[str, str]:
		"""Extract model, generation hint, and GUID from a service.tesla.com URL."""
		parsed = urlparse(url)
		parts = [p for p in parsed.path.split("/") if p]
		year_hint = ""
		doc_guid = ""
		model = "Model 3"
		applicable_generation = ""

		if _shared_parse_manual_url is not None:
			shared = _shared_parse_manual_url(url)
			model = shared.get("applicable_model") or model
			applicable_generation = shared.get("matched_generation") or ""
			doc_guid = shared.get("doc_guid") or ""
			year_hint = shared.get("year_hint") or ""

		for seg in parts:
			low = seg.lower()
			if low in _MODEL_MAP and _shared_parse_manual_url is None:
				model = _MODEL_MAP[low]
			elif re.fullmatch(r"20\d{2}", seg) and not year_hint:
				year_hint = f"{seg}+"
			elif seg.upper().startswith("GUID-") and not doc_guid:
				doc_guid = re.sub(r"\.html?$", "", seg, flags=re.IGNORECASE).upper()

		return {
			"model": model,
			"applicable_model": model,
			"generation": year_hint or applicable_generation,
			"applicable_generation": applicable_generation or year_hint,
			"doc_guid": doc_guid,
		}

	def parse_html(self, html_content: str, url: str = "") -> dict[str, Any]:
		"""Extract canonical procedure metadata and OEM parts from Tesla manual HTML."""
		if _shared_parse_html is not None:
			parsed = _shared_parse_html(html_content, url=url)
			return self.normalize_procedure(parsed, url=url)

		meta = self.parse_url_metadata(url) if url else {"model": "Model 3", "applicable_generation": "", "doc_guid": ""}
		cc = _first_match(
			html_content,
			[
				r'class=["\'][^"\']*correction-code[^"\']*["\'][^>]*>\s*([0-9A-Z]{6,10})\s*<',
				r"Correction code\s*[:#]?\s*([0-9A-Z]{6,10})",
			],
		)
		frt_raw = _first_match(
			html_content,
			[
				r'class=["\'][^"\']*frt[^"\']*["\'][^>]*>\s*([0-9]+\.[0-9]{1,2})\s*<',
				r"FRT\s*[:#]?\s*([0-9]+\.[0-9]{1,2})",
			],
		)
		title_raw = _first_match(
			html_content,
			[
				r'class=["\'][^"\']*procedure-title[^"\']*["\'][^>]*>(.*?)</span>',
				r'<h1[^>]*class=["\'][^"\']*topictitle1[^"\']*["\'][^>]*>(.*?)</h1>',
				r"<title>(.*?)</title>",
			],
		)
		title = re.sub(r"<[^>]+>", "", title_raw).strip()
		title = re.sub(r"\s*\|\s*Tesla.*$", "", title).strip() or "Tesla Service Procedure"

		return self.normalize_procedure(
			{
				"title": title,
				"correction_code": cc,
				"frt": float(frt_raw) if frt_raw else 1.0,
				"applicable_model": meta.get("model", "Model 3"),
				"applicable_generation": meta.get("applicable_generation", ""),
				"doc_guid": meta.get("doc_guid", ""),
				"parts": _fallback_extract_oem_parts(html_content),
			},
			url=url,
		)

	def parse_markdown(self, markdown_text: str, url: str = "") -> dict[str, Any]:
		"""Extract canonical procedure metadata and OEM part numbers from Markdown or text."""
		if _shared_parse_markdown is not None:
			parsed = _shared_parse_markdown(markdown_text, url=url)
			return self.normalize_procedure(parsed, url=url)

		meta = self.parse_url_metadata(url) if url else {"model": "Model 3", "applicable_generation": "", "doc_guid": ""}
		title = (
			_first_match(markdown_text, [r"^#\s+([^\r\n]+)", r"Procedure\s*:\s*([^\r\n]+)"], multiline_dot=False)
			or "Tesla Service Procedure"
		)
		cc = _first_match(markdown_text, [r"Correction\s*Code\s*[:#]?\s*([0-9A-Z]{6,10})"], multiline_dot=False)
		frt_raw = _first_match(
			markdown_text,
			[r"(?:FRT|Labor\s*Hours?)\s*[:#]?\s*([0-9]+(?:\.[0-9]{1,2})?)"],
			multiline_dot=False,
		)
		return self.normalize_procedure(
			{
				"title": title.strip(),
				"correction_code": cc,
				"frt": float(frt_raw) if frt_raw else 1.0,
				"applicable_model": meta.get("model", "Model 3"),
				"applicable_generation": meta.get("applicable_generation", ""),
				"doc_guid": meta.get("doc_guid", ""),
				"parts": _fallback_extract_oem_parts(markdown_text),
			},
			url=url,
		)

	def normalize_procedure(self, raw_data: dict[str, Any], url: str = "") -> dict[str, Any]:
		"""Normalize parsed payload with both canonical ISM fields and Agent OS aliases."""
		url_meta = self.parse_url_metadata(url) if url else {"model": "Model 3", "applicable_generation": "", "doc_guid": ""}
		if _shared_normalize_procedure is not None:
			norm = _shared_normalize_procedure(raw_data, url=url, fetch_manifest=False)
		else:
			norm = dict(raw_data or {})

		code = str(norm.get("correction_code") or "").strip()
		title = str(
			norm.get("title")
			or norm.get("procedure_title")
			or norm.get("procedure_name")
			or "Tesla Service Procedure"
		).strip()
		if code:
			title = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", title).strip() or "Tesla Service Procedure"

		frt = float(norm.get("frt") or norm.get("labor_hours") or 1.0)
		model = str(norm.get("applicable_model") or norm.get("model") or url_meta.get("model") or "Model 3").strip()
		generation = str(norm.get("applicable_generation") or url_meta.get("applicable_generation") or "").strip()
		service_side = str(
			norm.get("service_side")
			or (_shared_detect_service_side(title, code, norm.get("rh_correction_code")) if _shared_detect_service_side else _fallback_detect_side(title))
		)
		is_symmetrical = int(bool(norm.get("is_symmetrical") or service_side in ("LH", "RH")))
		parts = norm.get("parts") or []

		return {
			**norm,
			"title": title,
			"procedure_title": title,
			"correction_code": code,
			"rh_correction_code": str(norm.get("rh_correction_code") or ""),
			"is_symmetrical": is_symmetrical,
			"service_side": service_side,
			"frt": frt,
			"labor_hours": frt,
			"model": model,
			"applicable_model": model,
			"generation": url_meta.get("generation") or generation,
			"applicable_generation": generation,
			"doc_guid": str(norm.get("doc_guid") or url_meta.get("doc_guid") or ""),
			"tesla_manual_url": str(norm.get("tesla_manual_url") or url or ""),
			"parts": parts,
		}


def _validate_tesla_url(url: str) -> None:
	parsed = urlparse(url)
	if parsed.scheme != "https" or parsed.netloc not in ("service.tesla.com", "service.teslamotors.com"):
		raise ValueError("URL must be an https:// link on service.tesla.com or service.teslamotors.com.")


def _first_match(text: str, patterns: list[str], multiline_dot: bool = True) -> str:
	flags = re.IGNORECASE | re.MULTILINE | (re.DOTALL if multiline_dot else 0)
	for pattern in patterns:
		match = re.search(pattern, text, flags)
		if match:
			return match.group(1).strip()
	return ""


def _fallback_detect_side(title: str) -> str:
	if re.search(r"\bRH\b|Right Hand|\bRight\b(?!\s+(?:Turn|Foot))", title or "", re.IGNORECASE):
		return "RH"
	if re.search(r"\bLH\b|Left Hand|\bLeft\b(?!\s+(?:Turn|Foot))", title or "", re.IGNORECASE):
		return "LH"
	return "None"


def _fallback_extract_oem_parts(text: str) -> list[dict[str, Any]]:
	if _shared_extract_oem_parts is not None:
		return _shared_extract_oem_parts(text)
	parts: list[dict[str, Any]] = []
	seen: set[str] = set()
	for pn in _OEM_PART_RE.findall(text or ""):
		if pn not in seen:
			seen.add(pn)
			parts.append({"part_number": pn, "oem_part_number": pn, "part_name": f"Tesla OEM Part {pn}", "qty": 1, "side": "Both"})
	return parts

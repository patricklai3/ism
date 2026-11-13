"""Parse Tesla service manual URLs, HTML, or Markdown into ISM Service Packages."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from tools.client import IsmAgentClient

_MODEL_MAP = {
	"model3": "Model 3",
	"modely": "Model Y",
	"models": "Model S",
	"modelx": "Model X",
	"cybertruck": "Cybertruck",
}


class TeslaManualParser:
	"""Extracts Tesla procedure metadata and persists Service Packages via ISM."""

	def __init__(self, client: Optional[IsmAgentClient] = None) -> None:
		self.client = client

	def ingest_from_url(self, url: str, model_override: Optional[str] = None) -> dict[str, Any]:
		"""Fetch procedure on the Frappe backend and create a Service Package."""
		if not self.client:
			raise ValueError("IsmAgentClient is required for remote ingestion.")
		_validate_tesla_url(url)

		parsed = self.client.fetch_tesla_procedure(url)
		url_meta = self.parse_url_metadata(url)
		title = parsed.get("title") or "Tesla Service Procedure"
		code = parsed.get("correction_code") or None
		frt = float(parsed.get("frt") or 1.0)
		model = model_override or parsed.get("applicable_model") or url_meta.get("model") or "Model 3"

		return self.client.import_tesla_procedure(
			procedure_title=title,
			correction_code=code,
			labor_hours=frt,
			model=model,
		)

	def parse_url_metadata(self, url: str) -> dict[str, str]:
		"""Extract model, generation hint, and GUID from a service.tesla.com URL."""
		parsed = urlparse(url)
		parts = [p for p in parsed.path.split("/") if p]
		model = "Model 3"
		generation = ""
		doc_guid = ""

		for seg in parts:
			low = seg.lower()
			if low in _MODEL_MAP:
				model = _MODEL_MAP[low]
			elif re.fullmatch(r"20\d{2}", seg):
				generation = f"{seg}+"
			elif seg.upper().startswith("GUID-"):
				doc_guid = seg.removesuffix(".html")

		return {"model": model, "generation": generation, "doc_guid": doc_guid}

	def parse_html(self, html_content: str, url: str = "") -> dict[str, Any]:
		"""Extract procedure title, correction code, and FRT from Tesla manual HTML."""
		meta = self.parse_url_metadata(url) if url else {"model": "Model 3"}
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

		return {
			"procedure_title": title,
			"correction_code": cc,
			"labor_hours": float(frt_raw) if frt_raw else 1.0,
			"model": meta.get("model", "Model 3"),
		}

	def parse_markdown(self, markdown_text: str, url: str = "") -> dict[str, Any]:
		"""Extract procedure metadata and OEM part numbers from Markdown or plain text."""
		meta = self.parse_url_metadata(url) if url else {"model": "Model 3"}
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
		oem_parts = sorted(set(re.findall(r"\b(\d{7}-\d{2}-[A-Z])\b", markdown_text)))

		return {
			"procedure_title": title.strip(),
			"correction_code": cc,
			"labor_hours": float(frt_raw) if frt_raw else 1.0,
			"model": meta.get("model", "Model 3"),
			"parts": [{"part_number": p, "qty": 1} for p in oem_parts],
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

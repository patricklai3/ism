"""Normalize supplier CSV/TSV/pipe catalogs into ISM bulk-import structures."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional, Sequence, Union

if __package__ and __package__.startswith("ism_agent_os"):
	from ..client import IsmAgentClient
else:
	try:
		from tools.client import IsmAgentClient
	except ImportError:
		from ism_agent_os.client import IsmAgentClient

_HEADER_ALIASES = {
	"part": "part_num",
	"part#": "part_num",
	"part_no": "part_num",
	"part_number": "part_num",
	"sku": "part_num",
	"item_code": "part_num",
	"description": "description",
	"desc": "description",
	"name": "description",
	"item_name": "description",
	"part_name": "description",
	"title": "description",
	"source": "source",
	"part_source": "source",
	"brand_type": "source",
	"condition": "condition",
	"cond": "condition",
	"part_condition": "condition",
	"rate": "rate",
	"unit_rate": "rate",
	"price": "rate",
	"unit_price": "rate",
	"retail": "rate",
	"list_price": "rate",
	"standard_rate": "rate",
	"cost": "cost",
	"unit_cost": "cost",
	"wholesale": "cost",
	"valuation_rate": "cost",
	"qty": "qty",
	"quantity": "qty",
	"revision": "revision",
	"rev": "revision",
	"oem": "oem_num",
	"oem#": "oem_num",
	"oem_num": "oem_num",
	"oem_part_number": "oem_num",
	"brand": "brand",
	"warranty_period": "warranty_period",
	"stock_uom": "stock_uom",
	"uom": "stock_uom",
	"supplier": "supplier",
	"supplier_part_no": "supplier_part_no",
}

_CANONICAL_PIPE_HEADER = "Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#"


class SupplierCatalogSanitizer:
	"""Convert varied supplier catalog text or dict rows into canonical ISM items."""

	def parse_rows(
		self,
		raw_text: str,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
	) -> list[dict[str, Any]]:
		"""Parse raw CSV/TSV/pipe/semicolon catalog text into canonical structured dicts."""
		records = _iter_parsed_records(raw_text, default_source, default_condition)
		return [_record_to_structured_item(rec) for rec in records]

	def normalize_items(
		self,
		raw_items: Union[str, Sequence[dict[str, Any]], dict[str, Any]],
		default_source: str = "Aftermarket",
		default_condition: str = "New",
	) -> list[dict[str, Any]]:
		"""Normalize raw catalog text or dict rows into canonical bulk_import_parts items."""
		if isinstance(raw_items, str):
			return self.parse_rows(
				raw_items,
				default_source=default_source,
				default_condition=default_condition,
			)

		rows = [raw_items] if isinstance(raw_items, dict) else list(raw_items or [])
		norm_source = _normalize_source_label(default_source)
		norm_condition = _normalize_condition_label(default_condition)

		normalized: list[dict[str, Any]] = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			canonical = _canonicalize_dict_row(row, norm_source, norm_condition)
			if canonical:
				normalized.append(canonical)
		return normalized

	def normalize_text(
		self,
		raw_text: str,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
	) -> str:
		"""Parse raw CSV/TSV/pipe text and emit normalized pipe-delimited rows."""
		records = _iter_parsed_records(raw_text, default_source, default_condition)
		if not records:
			return ""

		output = [_CANONICAL_PIPE_HEADER]
		for record in records:
			output.append(
				f"{record['part_num']} | {record['description']} | {record['source']} | "
				f"{record['condition']} | {record['rate']} | {record['cost']} | "
				f"{record['qty']} | {record['revision']} | {record['oem_num']}"
			)
		return "\n".join(output)

	def ingest(
		self,
		client: IsmAgentClient,
		raw_text: str,
		supplier: Optional[str] = None,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
		chunk_size: int = 100,
		service_package: Optional[str] = None,
	) -> list[dict[str, Any]]:
		"""Parse catalog text into structured items and upload in safe chunk sizes."""
		items = self.parse_rows(
			raw_text,
			default_source=default_source,
			default_condition=default_condition,
		)
		if not items:
			return []

		results: list[dict[str, Any]] = []
		step = max(1, chunk_size)
		for start in range(0, len(items), step):
			batch_items = items[start : start + step]
			res = client.import_supplier_catalog(
				items=batch_items,
				supplier=supplier,
				default_source=default_source,
				default_condition=default_condition,
				service_package=service_package,
			)
			results.append(res)
		return results


def _iter_parsed_records(
	raw_text: str,
	default_source: str,
	default_condition: str,
) -> list[dict[str, str]]:
	normalized_condition = _normalize_condition_label(default_condition)
	lines = [line for line in (raw_text or "").strip().splitlines() if line.strip()]
	if not lines:
		return []

	delimiter = _detect_delimiter(lines[0])
	rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
	if not rows:
		return []

	col_map = _detect_header_map(rows[0])
	start_idx = 1 if col_map else 0
	records: list[dict[str, str]] = []
	for row in rows[start_idx:]:
		cells = [c.strip() for c in row]
		if not cells or not cells[0]:
			continue
		record = _extract_row(cells, col_map, default_source, normalized_condition)
		record["condition"] = _normalize_condition_label(record["condition"])
		records.append(record)
	return records


def _record_to_structured_item(record: dict[str, str]) -> dict[str, Any]:
	part_num = record["part_num"].strip()
	source = _normalize_source_label(record.get("source"))
	oem_num = record.get("oem_num", "").strip()
	return {
		"part_number": part_num,
		"part_name": (record.get("description") or part_num).strip(),
		"part_source": source,
		"part_condition": _normalize_condition_label(record.get("condition")),
		"unit_rate": _parse_float(record.get("rate"), 0.0),
		"valuation_rate": _parse_float(record.get("cost"), 0.0),
		"qty": _parse_float(record.get("qty"), 1.0),
		"revision": record.get("revision", "").strip(),
		"oem_part_number": oem_num or (part_num if source == "OEM" else ""),
	}


def _canonicalize_dict_row(
	row: dict[str, Any],
	default_source: str,
	default_condition: str,
) -> Optional[dict[str, Any]]:
	mapped: dict[str, Any] = {}
	for key, value in row.items():
		if value is None:
			continue
		norm_key = re.sub(r"[\s\-]+", "_", str(key).strip().lower())
		canonical_key = _HEADER_ALIASES.get(norm_key, norm_key)
		if canonical_key not in mapped or mapped[canonical_key] == "":
			mapped[canonical_key] = value

	part_num = str(mapped.get("part_num") or "").strip()
	if not part_num:
		return None

	source = _normalize_source_label(mapped.get("source") or default_source)
	oem_num = str(mapped.get("oem_num") or "").strip()
	desc = str(mapped.get("description") or part_num).replace("|", "/").strip() or part_num

	item: dict[str, Any] = {
		"part_number": part_num,
		"part_name": desc,
		"part_source": source,
		"part_condition": _normalize_condition_label(str(mapped.get("condition") or default_condition)),
		"unit_rate": _parse_float(mapped.get("rate"), 0.0),
		"valuation_rate": _parse_float(mapped.get("cost"), 0.0),
		"qty": _parse_float(mapped.get("qty"), 1.0),
		"revision": str(mapped.get("revision") or "").strip(),
		"oem_part_number": oem_num or (part_num if source == "OEM" else ""),
	}
	for extra in ("brand", "warranty_period", "stock_uom", "supplier", "supplier_part_no"):
		val = mapped.get(extra)
		if val is not None and str(val).strip():
			item[extra] = str(val).strip()
	return item


def _normalize_source_label(value: Optional[str]) -> str:
	return "OEM" if str(value or "").strip().upper().startswith("OEM") else "Aftermarket"


def _normalize_condition_label(value: Optional[str]) -> str:
	cleaned = (value or "New").strip().lower()
	if cleaned == "used" or "use" in cleaned:
		return "Used"
	if cleaned in ("reman", "remanufactured", "reconditioned", "rebuilt", "rec", "rcn", "reb") or any(
		tok in cleaned for tok in ("rec", "rcn", "reb", "rem")
	):
		return "Reconditioned"
	return "New"


def _detect_delimiter(sample_line: str) -> str:
	for delim in ("\t", "|", ";"):
		if delim in sample_line:
			return delim
	return ","


def _detect_header_map(first_row: list[str]) -> dict[str, int]:
	mapping: dict[str, int] = {}
	for idx, cell in enumerate(first_row):
		normalized = re.sub(r"[\s\-]+", "_", cell.strip().lower())
		canonical = _HEADER_ALIASES.get(normalized)
		if canonical and canonical not in mapping:
			mapping[canonical] = idx
	return mapping if "part_num" in mapping else {}


def _extract_row(
	cells: list[str],
	col_map: dict[str, int],
	default_source: str,
	default_condition: str,
) -> dict[str, str]:
	if col_map:
		def get(field: str, fallback: str = "") -> str:
			idx = col_map.get(field)
			return cells[idx] if idx is not None and idx < len(cells) and cells[idx] else fallback

		return {
			"part_num": get("part_num"),
			"description": get("description", get("part_num")).replace("|", "/"),
			"source": get("source", default_source),
			"condition": get("condition", default_condition),
			"rate": _clean_number(get("rate", "0")),
			"cost": _clean_number(get("cost", "0")),
			"qty": _clean_number(get("qty", "1"), default="1"),
			"revision": get("revision", ""),
			"oem_num": get("oem_num", ""),
		}

	# Positional fallback matching ISM native column order
	def pos(idx: int, fallback: str = "") -> str:
		return cells[idx] if idx < len(cells) and cells[idx] else fallback

	return {
		"part_num": pos(0),
		"description": pos(1, pos(0)).replace("|", "/"),
		"source": pos(2, default_source),
		"condition": pos(3, default_condition),
		"rate": _clean_number(pos(4, "0")),
		"cost": _clean_number(pos(5, "0")),
		"qty": _clean_number(pos(6, "1"), default="1"),
		"revision": pos(7, ""),
		"oem_num": pos(8, ""),
	}


def _clean_number(raw: Any, default: str = "0") -> str:
	if isinstance(raw, (int, float)):
		return str(raw)
	cleaned = re.sub(r"[,$\s]", "", str(raw or ""))
	return cleaned or default


def _parse_float(raw: Any, default: float = 0.0) -> float:
	if raw is None or raw == "":
		return default
	try:
		return float(_clean_number(raw, default=str(default)))
	except (ValueError, TypeError):
		return default

"""Normalize supplier CSV/TSV/pipe catalogs into ISM bulk-import format."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

from tools.client import IsmAgentClient

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
	"title": "description",
	"source": "source",
	"brand_type": "source",
	"condition": "condition",
	"rate": "rate",
	"price": "rate",
	"unit_price": "rate",
	"retail": "rate",
	"list_price": "rate",
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
	"oem_part_number": "oem_num",
}


class SupplierCatalogSanitizer:
	"""Convert varied supplier catalog text into ISM canonical pipe format."""

	def normalize_text(
		self,
		raw_text: str,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
	) -> str:
		"""Parse raw CSV/TSV/pipe text and emit normalized pipe-delimited rows."""
		lines = [line for line in raw_text.strip().splitlines() if line.strip()]
		if not lines:
			return ""

		delimiter = _detect_delimiter(lines[0])
		rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
		if not rows:
			return ""

		col_map = _detect_header_map(rows[0])
		start_idx = 1 if col_map else 0
		output = ["Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#"]

		for row in rows[start_idx:]:
			cells = [c.strip() for c in row]
			if not cells or not cells[0]:
				continue
			record = _extract_row(cells, col_map, default_source, default_condition)
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
	) -> list[dict[str, Any]]:
		"""Normalize and upload catalog text in safe chunk sizes."""
		normalized = self.normalize_text(raw_text, default_source, default_condition)
		all_lines = normalized.splitlines()
		if len(all_lines) <= 1:
			return []

		header, data_lines = all_lines[0], all_lines[1:]
		results: list[dict[str, Any]] = []
		for start in range(0, len(data_lines), max(1, chunk_size)):
			batch_text = "\n".join([header] + data_lines[start : start + chunk_size])
			res = client.import_supplier_catalog(
				raw_text=batch_text,
				supplier=supplier,
				default_source=default_source,
				default_condition=default_condition,
			)
			results.append(res)
		return results


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


def _clean_number(raw: str, default: str = "0") -> str:
	cleaned = re.sub(r"[,$\s]", "", raw or "")
	return cleaned or default

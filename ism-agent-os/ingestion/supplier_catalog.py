"""Normalize supplier CSV/TSV/pipe catalogs into ISM bulk-import structures."""

from __future__ import annotations

import csv
import io
import math
import re
from typing import Any, Optional, Sequence, Union

if __package__ and __package__.startswith("ism_agent_os"):
	from ..client import IsmAgentClient
	from ..pricing import PricingConfig, calculate_part_price
else:
	try:
		from tools.client import IsmAgentClient
		from tools.pricing import PricingConfig, calculate_part_price
	except ImportError:
		from ism_agent_os.client import IsmAgentClient
		from ism_agent_os.pricing import PricingConfig, calculate_part_price

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
	"tesla_price": "tesla_oem_price",
	"tesla_oem_price": "tesla_oem_price",
	"oem_price": "tesla_oem_price",
	"tesla_msrp": "tesla_oem_price",
	"tesla_retail": "tesla_oem_price",
}

_CANONICAL_PIPE_HEADER = "Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#"


class SupplierCatalogSanitizer:
	"""Convert varied supplier catalog text or dict rows into canonical ISM items."""

	def parse_rows(
		self,
		raw_text: str,
		default_source: str = "Aftermarket",
		default_condition: str = "New",
		*,
		apply_pricing: bool = True,
		preserve_explicit_rate: bool = False,
		pricing_config: Optional[PricingConfig] = None,
	) -> list[dict[str, Any]]:
		"""Parse raw CSV/TSV/pipe/semicolon catalog text into canonical structured dicts."""
		records = _iter_parsed_records(raw_text, default_source, default_condition)
		return [
			_record_to_structured_item(
				rec,
				apply_pricing=apply_pricing,
				preserve_explicit_rate=preserve_explicit_rate,
				pricing_config=pricing_config,
			)
			for rec in records
		]

	def normalize_items(
		self,
		raw_items: Union[str, Sequence[dict[str, Any]], dict[str, Any]],
		default_source: str = "Aftermarket",
		default_condition: str = "New",
		*,
		apply_pricing: bool = True,
		preserve_explicit_rate: bool = False,
		pricing_config: Optional[PricingConfig] = None,
	) -> list[dict[str, Any]]:
		"""Normalize raw catalog text or dict rows into canonical bulk_import_parts items."""
		if isinstance(raw_items, str):
			return self.parse_rows(
				raw_items,
				default_source=default_source,
				default_condition=default_condition,
				apply_pricing=apply_pricing,
				preserve_explicit_rate=preserve_explicit_rate,
				pricing_config=pricing_config,
			)

		rows = [raw_items] if isinstance(raw_items, dict) else list(raw_items or [])
		norm_source = _normalize_source_label(default_source)
		norm_condition = _normalize_condition_label(default_condition)

		normalized: list[dict[str, Any]] = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			canonical = _canonicalize_dict_row(
				row,
				norm_source,
				norm_condition,
				apply_pricing=apply_pricing,
				preserve_explicit_rate=preserve_explicit_rate,
				pricing_config=pricing_config,
			)
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

		pkg = (service_package or "").strip()
		if not pkg and not all(
			isinstance(it, dict) and bool(it.get("service_package") or it.get("service_packages"))
			for it in items
		):
			raise ValueError("service_package is required when ingesting supplier catalog parts.")

		results: list[dict[str, Any]] = []
		step = max(1, chunk_size)
		for start in range(0, len(items), step):
			batch_items = [{k: v for k, v in it.items() if k != "_pricing"} for it in items[start : start + step]]
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

	delimiter = _detect_delimiter("\n".join(lines[:10]))
	rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
	if not rows:
		return []

	col_map = _detect_header_map(rows[0])
	start_idx = 1 if col_map else 0
	records: list[dict[str, str]] = []
	for row in rows[start_idx:]:
		cells = [c.strip() for c in row]
		if not any(cells):
			continue
		record = _extract_row(cells, col_map, default_source, normalized_condition)
		if not record["part_num"].strip():
			continue
		record["condition"] = _normalize_condition_label(record["condition"])
		records.append(record)
	return records


def _build_structured_item(
	mapped: dict[str, Any],
	default_source: str = "Aftermarket",
	default_condition: str = "New",
	*,
	apply_pricing: bool = True,
	preserve_explicit_rate: bool = False,
	pricing_config: Optional[PricingConfig] = None,
) -> Optional[dict[str, Any]]:
	part_num = str(mapped.get("part_num") or "").strip()
	if not part_num:
		return None

	source_raw = mapped.get("source")
	source = _normalize_source_label(source_raw if source_raw not in (None, "") else default_source)
	cond_raw = mapped.get("condition")
	condition = _normalize_condition_label(cond_raw if cond_raw not in (None, "") else default_condition)
	oem_num = str(mapped.get("oem_num") or "").strip()
	desc = str(mapped.get("description") or part_num).replace("|", "/").strip() or part_num
	unit_rate = _parse_float(mapped.get("rate"), 0.0)
	valuation_rate = _parse_float(mapped.get("cost"), 0.0)
	tesla_oem_price = _parse_float(mapped.get("tesla_oem_price"), 0.0)

	pricing_meta: Optional[dict[str, Any]] = None
	if apply_pricing and tesla_oem_price > 0.0:
		override_val = unit_rate if (preserve_explicit_rate and unit_rate > 0.0) else None
		pricing_meta = calculate_part_price(
			tesla_oem_price=tesla_oem_price,
			cost=valuation_rate if valuation_rate > 0.0 else None,
			part_source=source,
			part_condition=condition,
			override_rate=override_val,
			part_number=oem_num or part_num,
			part_name=desc,
			config=pricing_config,
		)
		unit_rate = pricing_meta["sell_price"]

	item: dict[str, Any] = {
		"part_number": part_num,
		"part_name": desc,
		"part_source": source,
		"part_condition": condition,
		"unit_rate": unit_rate,
		"valuation_rate": valuation_rate,
		"qty": _parse_float(mapped.get("qty"), 1.0),
		"revision": str(mapped.get("revision") or "").strip(),
		"oem_part_number": oem_num or (part_num if source == "OEM" else ""),
	}
	if pricing_meta is not None:
		item["_pricing"] = pricing_meta
	for extra in ("brand", "warranty_period", "stock_uom", "supplier", "supplier_part_no", "service_package"):
		val = mapped.get(extra)
		if val is not None and str(val).strip():
			item[extra] = str(val).strip()
	if mapped.get("service_packages"):
		item["service_packages"] = mapped["service_packages"]
	return item


def _record_to_structured_item(
	record: dict[str, str],
	*,
	apply_pricing: bool = True,
	preserve_explicit_rate: bool = False,
	pricing_config: Optional[PricingConfig] = None,
) -> dict[str, Any]:
	return (
		_build_structured_item(
			record,
			apply_pricing=apply_pricing,
			preserve_explicit_rate=preserve_explicit_rate,
			pricing_config=pricing_config,
		)
		or {}
	)


def _canonicalize_dict_row(
	row: dict[str, Any],
	default_source: str,
	default_condition: str,
	*,
	apply_pricing: bool = True,
	preserve_explicit_rate: bool = False,
	pricing_config: Optional[PricingConfig] = None,
) -> Optional[dict[str, Any]]:
	mapped: dict[str, Any] = {}
	for key, value in row.items():
		if value is None:
			continue
		norm_key = re.sub(r"[\s\-]+", "_", str(key).strip().lower())
		canonical_key = _HEADER_ALIASES.get(norm_key, norm_key)
		if canonical_key not in mapped or mapped[canonical_key] == "":
			mapped[canonical_key] = value
	return _build_structured_item(
		mapped,
		default_source=default_source,
		default_condition=default_condition,
		apply_pricing=apply_pricing,
		preserve_explicit_rate=preserve_explicit_rate,
		pricing_config=pricing_config,
	)


def _normalize_source_label(value: Optional[str]) -> str:
	return "OEM" if str(value or "").strip().upper().startswith("OEM") else "Aftermarket"


_RECONDITIONED_TOKENS = frozenset(
	{"reman", "remanufactured", "reconditioned", "rebuilt", "rec", "rcn", "reb"}
)


def _normalize_condition_label(value: Any) -> str:
	cleaned = str(value if value is not None else "New").strip().lower()
	if not cleaned:
		return "New"
	tokens = set(re.findall(r"[a-z0-9]+", cleaned))
	if "used" in tokens:
		return "Used"
	if tokens & _RECONDITIONED_TOKENS:
		return "Reconditioned"
	return "New"


def _detect_delimiter(sample_text: str) -> str:
	sample = (sample_text or "").strip()
	if not sample:
		return ","
	try:
		dialect = csv.Sniffer().sniff(sample, delimiters="\t|;,")
		if dialect.delimiter in ("\t", "|", ";", ","):
			return dialect.delimiter
	except csv.Error:
		pass
	unquoted = re.sub(r'"(?:""|[^"])*"', "", sample)
	first_line = unquoted.splitlines()[0] if unquoted.splitlines() else unquoted
	for delim in ("\t", "|", ";"):
		if delim in first_line:
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


_POSITIONAL_COL_MAP = {
	"part_num": 0,
	"description": 1,
	"source": 2,
	"condition": 3,
	"rate": 4,
	"cost": 5,
	"qty": 6,
	"revision": 7,
	"oem_num": 8,
}


def _extract_row(
	cells: list[str],
	col_map: dict[str, int],
	default_source: str,
	default_condition: str,
) -> dict[str, str]:
	active_map = col_map or _POSITIONAL_COL_MAP

	def get(field: str, fallback: str = "") -> str:
		idx = active_map.get(field)
		return cells[idx] if idx is not None and idx < len(cells) and cells[idx] else fallback

	part_num = get("part_num")
	return {
		"part_num": part_num,
		"description": get("description", part_num).replace("|", "/"),
		"source": get("source", default_source),
		"condition": get("condition", default_condition),
		"rate": _clean_number(get("rate", "0")),
		"cost": _clean_number(get("cost", "0")),
		"qty": _clean_number(get("qty", "1"), default="1"),
		"revision": get("revision", ""),
		"oem_num": get("oem_num", ""),
		"tesla_oem_price": _clean_number(get("tesla_oem_price", ""), default=""),
	}


def _clean_number(raw: Any, default: str = "0") -> str:
	if isinstance(raw, bool):
		return default
	if isinstance(raw, (int, float)):
		return str(raw) if math.isfinite(raw) else default
	cleaned = re.sub(r"[,$\s]", "", str(raw or ""))
	return cleaned or default


def _parse_float(raw: Any, default: float = 0.0) -> float:
	if raw is None or raw == "" or isinstance(raw, bool):
		return default
	try:
		val = float(_clean_number(raw, default=str(default)))
	except (ValueError, TypeError):
		return default
	return val if math.isfinite(val) else default

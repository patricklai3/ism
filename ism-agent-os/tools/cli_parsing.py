"""Argument and JSON/filter parsing helpers for the ISM Agent OS CLI."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

_JSON_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
_JSON_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _read_utf8_file(file_arg: str, label: str = "JSON") -> str:
	"""Read a UTF-8 file (stripping optional BOM) after expanding `~`."""
	cleaned = str(file_arg or "").strip()
	if not cleaned:
		raise ValueError(f"{label} file path missing.")
	path = Path(cleaned).expanduser()
	if not path.is_file():
		raise FileNotFoundError(f"{label} file not found: {path}")
	return path.read_text(encoding="utf-8-sig")


def _parse_unquoted_json_scalar(token: str) -> Any:
	"""Convert an unquoted scalar token from PowerShell-stripped JSON into a Python value."""
	if token == "true":
		return True
	if token == "false":
		return False
	if token == "null":
		return None
	if _JSON_INT_RE.fullmatch(token):
		return int(token)
	if _JSON_NUM_RE.fullmatch(token):
		return float(token)
	return token


def _parse_single_quoted(text: str, idx: int) -> tuple[str, int]:
	"""Parse a single-quoted string token starting at idx."""
	n = len(text)
	idx += 1
	chars: list[str] = []
	while idx < n:
		ch = text[idx]
		if ch == "\\" and idx + 1 < n:
			chars.append(text[idx + 1])
			idx += 2
			continue
		if ch == "'":
			return "".join(chars), idx + 1
		chars.append(ch)
		idx += 1
	raise ValueError("Unterminated single-quoted string")


def _skip_ws(text: str, idx: int) -> int:
	n = len(text)
	while idx < n and text[idx].isspace():
		idx += 1
	return idx


def _parse_unquoted_json_value(text: str, idx: int, stop_chars: tuple[str, ...] = ()) -> tuple[Any, int]:
	"""Recursively parse PowerShell-stripped JSON containers and unquoted scalars."""
	idx = _skip_ws(text, idx)
	n = len(text)
	if idx >= n:
		raise ValueError("Unexpected end of input")

	ch = text[idx]
	if ch == "{":
		idx = _skip_ws(text, idx + 1)
		obj: dict[str, Any] = {}
		if idx < n and text[idx] == "}":
			return obj, idx + 1
		while idx < n:
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == "'":
				key, idx = _parse_single_quoted(text, idx)
				idx = _skip_ws(text, idx)
				if idx >= n or text[idx] != ":":
					raise ValueError("Expected ':' after object key")
			else:
				colon = text.find(":", idx)
				if colon == -1:
					raise ValueError("Expected ':' in object entry")
				key = text[idx:colon].strip()
				if not key:
					raise ValueError("Empty object key")
				idx = colon
			idx += 1
			val, idx = _parse_unquoted_json_value(text, idx, stop_chars=(",", "}"))
			obj[key] = val
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == ",":
				idx += 1
				continue
			if idx < n and text[idx] == "}":
				return obj, idx + 1
			raise ValueError("Expected ',' or '}' in object")
		raise ValueError("Unterminated object")

	if ch == "[":
		idx = _skip_ws(text, idx + 1)
		arr: list[Any] = []
		if idx < n and text[idx] == "]":
			return arr, idx + 1
		while idx < n:
			val, idx = _parse_unquoted_json_value(text, idx, stop_chars=(",", "]"))
			arr.append(val)
			idx = _skip_ws(text, idx)
			if idx < n and text[idx] == ",":
				idx += 1
				continue
			if idx < n and text[idx] == "]":
				return arr, idx + 1
			raise ValueError("Expected ',' or ']' in array")
		raise ValueError("Unterminated array")

	if ch == "'":
		return _parse_single_quoted(text, idx)

	start = idx
	while idx < n and text[idx] not in stop_chars:
		idx += 1
	token = text[start:idx].strip()
	if not token:
		raise ValueError("Empty scalar token")
	return _parse_unquoted_json_scalar(token), idx


def _repair_unquoted_json(text: str) -> Any:
	"""Parse JSON whose double quotes were stripped by Windows PowerShell argv passing."""
	value, idx = _parse_unquoted_json_value(text, 0)
	idx = _skip_ws(text, idx)
	if idx != len(text):
		raise ValueError("Unexpected trailing tokens")
	return value


def _loads_json_with_ps_fallback(text: str) -> Any:
	"""Parse JSON, falling back to unquoted PowerShell-stripped container repair when safe."""
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		stripped = text.lstrip()
		if '"' not in text and stripped.startswith(("[", "{")):
			try:
				return _repair_unquoted_json(stripped)
			except ValueError:
				pass
		raise


def _parse_json_opt(
	raw: Optional[str],
	default: Any = None,
	expected_type: Optional[type[Any] | tuple[type[Any], ...]] = None,
) -> Any:
	"""Parse a CLI JSON or @file option, optionally validating its container type."""
	if raw is None or not str(raw).strip():
		return default
	text = str(raw).strip()
	if text.startswith("@"):
		file_arg = text[1:].strip()
		if not file_arg:
			raise ValueError("JSON file path missing after '@'.")
		text = _read_utf8_file(file_arg, label="JSON")
	try:
		parsed = _loads_json_with_ps_fallback(text)
	except json.JSONDecodeError as exc:
		raise json.JSONDecodeError(
			f"{exc.msg} (hint: on Windows PowerShell, pass @path/to/payload.json to avoid shell quoting issues)",
			exc.doc,
			exc.pos,
		) from exc
	if parsed is None:
		return default
	if expected_type is not None and not isinstance(parsed, expected_type):
		if isinstance(expected_type, tuple):
			expected_desc = " or ".join(t.__name__ for t in expected_type)
		else:
			expected_desc = expected_type.__name__
		raise ValueError(f"Expected JSON {expected_desc}, got {type(parsed).__name__}.")
	return parsed


def _parse_service_specs(raw_items: Optional[list[str]]) -> list[Any]:
	"""Parse repeatable --service / --add-service values, supporting 'Title::hours' syntax."""
	parsed: list[Any] = []
	for raw in raw_items or []:
		if "::" in raw:
			title, hours_str = raw.rsplit("::", 1)
			title = title.strip()
			hours_str = hours_str.strip()
			if title and hours_str:
				try:
					hours = float(hours_str)
				except ValueError:
					hours = -1.0
				if math.isfinite(hours) and hours > 0:
					parsed.append({"service_title": title, "labor_hours": hours})
					continue
		parsed.append(raw)
	return parsed


def _flatten_csv_items(raw_items: Optional[list[str]]) -> Optional[list[str]]:
	"""Flatten repeatable comma-separated CLI strings into a trimmed list or None."""
	if not raw_items:
		return None
	items: list[str] = []
	for item in raw_items:
		items.extend(part.strip() for part in str(item).split(",") if part.strip())
	return items or None


def _flatten_service_rows(raw_rows: list[str]) -> list[str]:
	"""Flatten repeatable or comma-separated service row IDs into a list."""
	return _flatten_csv_items(raw_rows) or []


def _parse_fields_opt(raw: Optional[str]) -> Optional[list[str]]:
	if raw is None or not str(raw).strip():
		return None
	text = str(raw).strip()
	if text.startswith(("[", "{", "@")):
		parsed = _parse_json_opt(text, expected_type=list)
		return [str(col).strip() for col in parsed if str(col).strip()] or None if parsed is not None else None
	return [col.strip() for col in text.split(",") if col.strip()] or None


def _split_filter_expr(expr: str) -> list[str]:
	"""Split a --filter expression on the earliest comparison operator occurrence."""
	best_idx: Optional[int] = None
	best_op: Optional[str] = None
	for op in ("!=", ">=", "<=", "=", ">", "<"):
		idx = expr.find(op)
		if idx != -1 and (
			best_idx is None
			or idx < best_idx
			or (idx == best_idx and len(op) > len(best_op or ""))
		):
			best_idx = idx
			best_op = op
	if best_idx is None or best_op is None:
		raise ValueError(f"Invalid --filter syntax '{expr}'. Expected key=val, key!=val, or key=op:val.")
	key = expr[:best_idx].strip()
	rhs = expr[best_idx + len(best_op) :]
	if not key:
		raise ValueError(f"Invalid --filter syntax '{expr}'. Missing field name.")
	if best_op == "=" and ":" in rhs:
		explicit_op, val = rhs.split(":", 1)
		if explicit_op.strip():
			return [key, explicit_op.strip(), val.strip()]
	return [key, best_op, rhs.strip()]


def _parse_filters_opt(raw_json: Optional[str], exprs: Optional[list[str]]) -> Any:
	"""Parse --filters-json and merge any repeatable --filter expressions."""
	base = _parse_json_opt(raw_json, expected_type=(dict, list))
	if not exprs:
		return base
	clauses: list[Any] = []
	if isinstance(base, list):
		clauses.extend(base)
	elif isinstance(base, dict):
		for key, value in base.items():
			if isinstance(value, (list, tuple)) and len(value) == 2:
				clauses.append([str(key), str(value[0]), value[1]])
			else:
				clauses.append([str(key), "=", value])
	for expr in exprs:
		clauses.append(_split_filter_expr(expr))
	return clauses

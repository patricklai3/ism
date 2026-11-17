"""Atomic stage persistence and receipt recall for local staged actions."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_STAGE_TTL_SECONDS = 3600
_STAGE_ID_RE = re.compile(r"^stg_[0-9a-f]{12,32}$")
_AGENT_OS_ROOT = Path(__file__).resolve().parent.parent


def resolve_staged_dir(staged_dir: Optional[str | Path] = None) -> Path:
	"""Resolve the .ism_staged storage directory from arg, env, or repo root."""
	if staged_dir is not None and str(staged_dir).strip():
		return Path(staged_dir).expanduser().resolve()
	env_dir = os.environ.get("ISM_STAGED_DIR", "").strip()
	if env_dir:
		return Path(env_dir).expanduser().resolve()
	repo_root = _AGENT_OS_ROOT.parent
	if (repo_root / ".git").exists():
		return (repo_root / ".ism_staged").resolve()
	return (_AGENT_OS_ROOT / ".ism_staged").resolve()


def validate_stage_id(stage_id: str) -> str:
	"""Validate stage_id format to prevent path traversal."""
	if not isinstance(stage_id, str) or not _STAGE_ID_RE.fullmatch(stage_id):
		raise ValueError(f"Invalid stage_id: {stage_id!r}")
	return stage_id


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
	tmp_path = path.parent / tmp_name
	serialized = json.dumps(payload, indent=2, default=str)
	try:
		with open(tmp_path, "w", encoding="utf-8") as fh:
			fh.write(serialized)
			fh.flush()
			os.fsync(fh.fileno())
		os.replace(tmp_path, path)
	except BaseException:
		try:
			if tmp_path.exists():
				tmp_path.unlink()
		except OSError:
			pass
		raise


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
	if not path.is_file():
		return None
	raw = path.read_text(encoding="utf-8")
	if not raw.strip():
		return None
	data = json.loads(raw)
	if not isinstance(data, dict):
		raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}.")
	return data


def _stage_record_path(stage_id: str, staged_dir: Optional[str | Path] = None) -> Path:
	valid_id = validate_stage_id(stage_id)
	root = resolve_staged_dir(staged_dir)
	return root / "actions" / f"{valid_id}.json"


def create_stage_record(
	form_spec: dict[str, Any],
	*,
	mode: str = "single",
	env_file: Optional[str] = None,
	cwd: Optional[str] = None,
	ttl_seconds: int = DEFAULT_STAGE_TTL_SECONDS,
	staged_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
	"""Create and persist a new pending staged action record with a 256-bit nonce."""
	stage_id = f"stg_{secrets.token_hex(8)}"
	nonce = secrets.token_hex(32)
	now = time.time()
	ttl = max(1, int(ttl_seconds))
	steps = copy.deepcopy(form_spec.get("steps") or [])
	record: dict[str, Any] = {
		"stage_id": stage_id,
		"nonce": nonce,
		"status": "pending",
		"created_at": now,
		"expires_at": now + ttl,
		"updated_at": now,
		"mode": mode,
		"env_file": env_file,
		"cwd": cwd,
		"ready": bool(form_spec.get("ready", False)),
		"missing_required": list(form_spec.get("missing_required") or []),
		"form_spec": copy.deepcopy(form_spec),
		"commands": steps,
		"last_error": None,
		"result": None,
		"receipt": None,
	}
	_atomic_write_json(_stage_record_path(stage_id, staged_dir), record)
	return record


def load_stage_record(stage_id: str, *, staged_dir: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
	"""Load a staged action record by ID, or None if not found."""
	return _read_json_file(_stage_record_path(stage_id, staged_dir))


def update_stage_record(
	stage_id: str,
	updates: dict[str, Any],
	*,
	staged_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
	"""Merge updates into an existing staged action record and persist atomically."""
	path = _stage_record_path(stage_id, staged_dir)
	record = _read_json_file(path)
	if record is None:
		raise ValueError(f"Staged action '{stage_id}' not found.")
	record.update(updates)
	record["updated_at"] = time.time()
	_atomic_write_json(path, record)
	return record


def _collect_ids_from_data(data: Any, ids: dict[str, str], command: Optional[str] = None) -> None:
	if not isinstance(data, dict):
		return
	ro_val = data.get("repair_order")
	if isinstance(ro_val, str) and ro_val.strip():
		ids["repair_order"] = ro_val.strip()
	elif isinstance(ro_val, dict) and isinstance(ro_val.get("name"), str) and ro_val["name"].strip():
		ids["repair_order"] = ro_val["name"].strip()

	se_val = data.get("schedule_entry")
	if isinstance(se_val, str) and se_val.strip():
		ids["schedule_entry"] = se_val.strip()

	for list_key, single_key in (
		("purchase_orders", "purchase_order"),
		("rfqs", "rfq"),
		("payment_entries", "payment_entry"),
	):
		seq = data.get(list_key)
		if isinstance(seq, list) and seq and isinstance(seq[0], str) and seq[0].strip():
			ids[single_key] = seq[0].strip()

	for direct_key in (
		"purchase_order",
		"payment_entry",
		"purchase_invoice",
		"purchase_receipt",
		"sales_invoice",
		"action_item",
	):
		val = data.get(direct_key)
		if isinstance(val, str) and val.strip():
			ids[direct_key] = val.strip()

	name_val = data.get("name")
	if isinstance(name_val, str) and name_val.strip():
		clean_name = name_val.strip()
		ids["name"] = clean_name
		if clean_name.startswith("RO-"):
			ids.setdefault("repair_order", clean_name)
		elif clean_name.startswith("SE-"):
			ids.setdefault("schedule_entry", clean_name)
		elif clean_name.startswith("PUR-ORD-"):
			ids.setdefault("purchase_order", clean_name)
		elif clean_name.startswith("ACC-PAY-"):
			ids.setdefault("payment_entry", clean_name)
		elif clean_name.startswith(("ACC-PINV-", "PINV-")):
			ids.setdefault("purchase_invoice", clean_name)
		elif clean_name.startswith(("MAT-PRE-", "PREC-")):
			ids.setdefault("purchase_receipt", clean_name)
		elif clean_name.startswith(("ACC-SINV-", "SINV-")):
			ids.setdefault("sales_invoice", clean_name)
		elif command == "create-action":
			ids.setdefault("action_item", clean_name)


def extract_receipt_metadata(
	execution_result: dict[str, Any],
	*,
	mode: str,
	commands: list[str],
) -> dict[str, Any]:
	"""Extract normalized IDs, URLs, data, and per-step summaries from an execution result."""
	ids: dict[str, str] = {}
	urls: dict[str, str] = {}
	steps_summary: list[dict[str, Any]] = []
	merged_data: Any = None

	if mode == "batch" and isinstance(execution_result.get("results"), list):
		merged_dict: dict[str, Any] = {}
		for entry in execution_result["results"]:
			if not isinstance(entry, dict):
				continue
			step_cmd = entry.get("command")
			step_res = entry.get("result")
			step_data = step_res.get("data") if isinstance(step_res, dict) else None
			step_urls = step_res.get("urls") if isinstance(step_res, dict) else None
			_collect_ids_from_data(step_data, ids, command=step_cmd)
			if isinstance(step_urls, dict):
				for k, v in step_urls.items():
					if v:
						urls[str(k)] = str(v)
			if isinstance(step_data, dict):
				for k, v in step_data.items():
					if v is not None:
						merged_dict[k] = v
			steps_summary.append({
				"index": entry.get("index", len(steps_summary)),
				"command": step_cmd,
				"ok": bool(entry.get("ok")),
				"data": step_data,
				"urls": step_urls if isinstance(step_urls, dict) else {},
			})
		merged_data = merged_dict if merged_dict else None
	else:
		single_cmd = commands[0] if commands else None
		single_data = execution_result.get("data") if isinstance(execution_result, dict) else None
		single_urls = execution_result.get("urls") if isinstance(execution_result, dict) else None
		_collect_ids_from_data(single_data, ids, command=single_cmd)
		if isinstance(single_urls, dict):
			for k, v in single_urls.items():
				if v:
					urls[str(k)] = str(v)
		merged_data = single_data
		steps_summary.append({
			"index": 0,
			"command": single_cmd,
			"ok": not (isinstance(execution_result, dict) and execution_result.get("success") is False),
			"data": single_data,
			"urls": dict(urls),
		})

	primary_id = (
		ids.get("repair_order")
		or ids.get("schedule_entry")
		or ids.get("purchase_order")
		or ids.get("purchase_invoice")
		or ids.get("payment_entry")
		or ids.get("sales_invoice")
		or ids.get("purchase_receipt")
		or ids.get("rfq")
		or ids.get("action_item")
		or ids.get("name")
	)
	if isinstance(merged_data, dict) and primary_id and not merged_data.get("name"):
		merged_data = {**merged_data, "name": primary_id}

	return {
		"ids": ids,
		"primary_id": primary_id,
		"data": merged_data,
		"urls": urls,
		"steps": steps_summary,
	}


def record_receipt(
	stage_id: str,
	execution_result: dict[str, Any],
	*,
	commands: list[str],
	mode: str = "single",
	staged_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
	"""Persist a submitted stage receipt to actions/<stage_id>.json and latest.json."""
	meta = extract_receipt_metadata(execution_result, mode=mode, commands=commands)
	receipt: dict[str, Any] = {
		"stage_id": stage_id,
		"submitted_at": time.time(),
		"mode": mode,
		"commands": list(commands),
		**meta,
		"result": execution_result,
	}
	update_stage_record(
		stage_id,
		{
			"status": "submitted",
			"last_error": None,
			"result": execution_result,
			"receipt": receipt,
		},
		staged_dir=staged_dir,
	)
	root = resolve_staged_dir(staged_dir)
	_atomic_write_json(root / "latest.json", receipt)
	return receipt


def read_latest_receipt(
	*,
	stage_id: Optional[str] = None,
	staged_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
	"""Read the latest submitted receipt or inspect a specific stage_id record."""
	root = resolve_staged_dir(staged_dir)
	if stage_id is not None and str(stage_id).strip():
		rec = load_stage_record(str(stage_id).strip(), staged_dir=root)
		if rec is None:
			raise ValueError(f"Staged action '{stage_id}' not found.")
		return {
			"success": True,
			"stage_id": rec["stage_id"],
			"status": rec["status"],
			"ready": rec.get("ready"),
			"missing_required": rec.get("missing_required", []),
			"data": rec.get("receipt"),
		}

	latest_path = root / "latest.json"
	receipt = _read_json_file(latest_path)
	if receipt is None:
		raise ValueError(f"No staged action receipt found in {latest_path}.")
	return {
		"success": True,
		"stage_id": receipt.get("stage_id"),
		"status": "submitted",
		"data": receipt,
	}


def read_server_info(staged_dir: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
	"""Read daemon connection metadata from server.json if present."""
	root = resolve_staged_dir(staged_dir)
	try:
		return _read_json_file(root / "server.json")
	except (json.JSONDecodeError, OSError, ValueError):
		return None


def write_server_info(staged_dir: Optional[str | Path], info: dict[str, Any]) -> Path:
	"""Atomically write server.json daemon connection metadata."""
	root = resolve_staged_dir(staged_dir)
	path = root / "server.json"
	_atomic_write_json(path, info)
	return path


def remove_server_info(staged_dir: Optional[str | Path], expected_pid: Optional[int] = None) -> None:
	"""Remove server.json if owned by expected_pid (or unconditionally when omitted)."""
	root = resolve_staged_dir(staged_dir)
	path = root / "server.json"
	if not path.is_file():
		return
	if expected_pid is not None:
		info = read_server_info(root)
		if info and info.get("pid") != expected_pid:
			return
	try:
		path.unlink()
	except OSError:
		pass

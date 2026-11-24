#!/usr/bin/env python3
"""Zero-dependency Code Health discovery scanner, batch planner, and ledger CLI.

Designed for recurring / nightly Antigravity Swarm sessions in ``d:\\ism\\code-health``.
All mutations to ``zones.json``, ``backlog.json``, ``metrics_history.jsonl``, and
``runs/`` are intended to be executed by the single-writer Swarm coordinator.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SKIP_DIRS = {
	"__pycache__",
	"node_modules",
	".git",
	".codegraph",
	"eval_results",
	"logs",
	".venv",
	"venv",
	".pytest_cache",
	"dist",
	"build",
}
SCAN_EXTS = {".py", ".js", ".css", ".html", ".md"}
VALID_CATEGORIES = {
	"security",
	"performance",
	"correctness",
	"file-size",
	"test-coverage",
	"hygiene",
}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_EFFORTS = {"small", "medium", "large"}
VALID_STATUSES = {"open", "in_progress", "awaiting_review", "fixed", "wontfix", "deferred"}

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
EFFORT_RANK = {"small": 3, "medium": 2, "large": 1}
CRITICALITY_WEIGHT = {"critical": 40, "high": 25, "medium": 15, "low": 5}

PRESETS: dict[str, dict[str, int]] = {
	"eco": {
		"audit_zones": 1,
		"fix_tracks": 1,
		"max_loc_per_fixer": 250,
		"max_items_per_fixer": 2,
	},
	"standard": {
		"audit_zones": 2,
		"fix_tracks": 2,
		"max_loc_per_fixer": 400,
		"max_items_per_fixer": 2,
	},
	"audit-only": {
		"audit_zones": 3,
		"fix_tracks": 0,
		"max_loc_per_fixer": 0,
		"max_items_per_fixer": 0,
	},
}

DEFAULT_THRESHOLDS = {
	"file_size_warning_loc": 800,
	"file_size_critical_loc": 1500,
	"test_file_size_critical_loc": 2500,
	"long_function_loc": 120,
	"agents_md_max_bytes": 24000,
}

N_PLUS_ONE_CALLS = {
	"frappe.get_doc",
	"frappe.db.get_value",
	"frappe.db.get_single_value",
	"frappe.db.exists",
	"frappe.db.count",
	"frappe.get_all",
	"frappe.db.get_all",
}
GET_ALL_CALLS = {
	"frappe.get_all",
	"frappe.db.get_all",
	"frappe.get_list",
	"frappe.db.get_list",
}
LIMIT_KWARGS = {"limit", "limit_page_length", "page_length"}
TODO_RE = re.compile(r"(?:#|//)\s*\b(FIXME|HACK|XXX)\b[:\s]*(.*)")


def default_repo_root() -> Path:
	return Path(__file__).resolve().parents[2]


def default_state_dir(repo_root: Path) -> Path:
	return repo_root / "code-health"


def utc_now_iso() -> str:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_rel(path: str) -> str:
	return path.replace("\\", "/").lstrip("./").rstrip("/")


def is_test_path(rel_path: str) -> bool:
	norm = normalize_rel(rel_path).lower()
	parts = norm.split("/")
	name = parts[-1] if parts else ""
	if "tests" in parts or "testing" in parts:
		return True
	return name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.js")


def compute_fingerprint(rule: str, rel_path: str, symbol: str = "") -> str:
	raw = f"{rule.strip().lower()}|{normalize_rel(rel_path)}|{symbol.strip()}"
	return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def load_json(path: Path) -> dict[str, Any]:
	return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix(path.suffix + ".tmp")
	tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	os.replace(tmp, path)


def run_git(repo_root: Path, args: list[str]) -> tuple[int, str]:
	try:
		proc = subprocess.run(
			["git", *args],
			cwd=str(repo_root),
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=15,
			check=False,
		)
		return proc.returncode, proc.stdout.rstrip("\r\n")
	except (OSError, subprocess.SubprocessError):
		return 1, ""


def git_info(repo_root: Path) -> dict[str, Any]:
	rc_branch, branch = run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
	rc_sha, sha = run_git(repo_root, ["rev-parse", "HEAD"])
	rc_short, short_sha = run_git(repo_root, ["rev-parse", "--short", "HEAD"])
	rc_status, status_out = run_git(repo_root, ["status", "--porcelain"])
	dirty_files = [line[3:].strip() for line in status_out.splitlines() if line.strip()] if rc_status == 0 else []
	return {
		"available": rc_branch == 0 and rc_sha == 0,
		"branch": branch if rc_branch == 0 else None,
		"sha": sha if rc_sha == 0 else None,
		"short_sha": short_sha if rc_short == 0 else None,
		"dirty_files": dirty_files,
	}


def git_churn_for_paths(repo_root: Path, paths: list[str], since_commit: str | None) -> int:
	if not paths:
		return 0
	if since_commit:
		rc, out = run_git(repo_root, ["rev-list", "--count", f"{since_commit}..HEAD", "--", *paths])
		if rc == 0 and out.isdigit():
			return int(out)
	rc, out = run_git(repo_root, ["rev-list", "--count", "-n", "50", "HEAD", "--", *paths])
	if rc == 0 and out.isdigit():
		return int(out)
	return 0


def iter_zone_files(repo_root: Path, zone: dict[str, Any]) -> list[Path]:
	collected: list[Path] = []
	seen: set[Path] = set()
	for raw_entry in zone.get("paths", []):
		target = repo_root / raw_entry
		if not target.exists():
			continue
		if target.is_file():
			if target.suffix.lower() in SCAN_EXTS and not target.name.endswith((".min.js", ".min.css")):
				resolved = target.resolve()
				if resolved not in seen:
					seen.add(resolved)
					collected.append(target)
			continue
		for root, dirs, files in os.walk(target):
			dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
			for fname in sorted(files):
				if fname.endswith((".min.js", ".min.css", ".map")):
					continue
				fpath = Path(root) / fname
				if fpath.suffix.lower() not in SCAN_EXTS:
					continue
				resolved = fpath.resolve()
				if resolved not in seen:
					seen.add(resolved)
					collected.append(fpath)
	return collected


def validate_zones_disjoint(repo_root: Path, zones_data: dict[str, Any]) -> dict[str, Any]:
	file_owners: dict[str, list[str]] = defaultdict(list)
	missing_paths: list[dict[str, str]] = []
	zone_file_counts: dict[str, int] = {}

	for zone in zones_data.get("zones", []):
		zid = zone["id"]
		for raw_entry in zone.get("paths", []):
			if not (repo_root / raw_entry).exists():
				missing_paths.append({"zone": zid, "path": raw_entry})
		files = iter_zone_files(repo_root, zone)
		zone_file_counts[zid] = len(files)
		for fpath in files:
			rel = normalize_rel(str(fpath.relative_to(repo_root)))
			file_owners[rel].append(zid)

	overlaps = {rel: owners for rel, owners in file_owners.items() if len(owners) > 1}
	return {
		"ok": not overlaps and not missing_paths,
		"overlaps": overlaps,
		"missing_paths": missing_paths,
		"zone_file_counts": zone_file_counts,
		"total_mapped_files": len(file_owners),
	}


def dotted_name(node: ast.AST) -> str | None:
	parts: list[str] = []
	curr: ast.AST | None = node
	while isinstance(curr, ast.Attribute):
		parts.append(curr.attr)
		curr = curr.value
	if isinstance(curr, ast.Name):
		parts.append(curr.id)
		return ".".join(reversed(parts))
	return None


def is_dynamic_sql_expr(node: ast.AST) -> bool:
	if isinstance(node, ast.JoinedStr):
		return True
	if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
		return True
	if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
		return True
	return False


def is_true_constant(node: ast.AST) -> bool:
	return isinstance(node, ast.Constant) and node.value is True


class PythonFileAnalyzer(ast.NodeVisitor):
	def __init__(self, rel_path: str, zone_id: str, is_test: bool) -> None:
		self.rel_path = rel_path
		self.zone_id = zone_id
		self.is_test = is_test
		self.scope_stack: list[str] = []
		self.loop_depth_stack: list[int] = [0]
		self.findings: list[dict[str, Any]] = []
		self._seen_keys: set[tuple[str, str]] = set()

	def _current_scope(self) -> str:
		return ".".join(self.scope_stack) if self.scope_stack else "<module>"

	def _emit(
		self,
		rule: str,
		title: str,
		category: str,
		severity: str,
		line_hint: int,
		symbol: str,
		safe_for_auto_fix: bool,
		effort: str,
		evidence: str,
		proposed_fix: str,
	) -> None:
		key = (rule, symbol)
		if key in self._seen_keys:
			return
		self._seen_keys.add(key)
		self.findings.append(
			{
				"rule": rule,
				"fingerprint": compute_fingerprint(rule, self.rel_path, symbol),
				"title": title,
				"category": category,
				"severity": severity,
				"zone": self.zone_id,
				"files": [self.rel_path],
				"line_hint": line_hint,
				"symbol": symbol,
				"source": "static-scanner",
				"safe_for_auto_fix": safe_for_auto_fix,
				"estimated_effort": effort,
				"evidence": evidence,
				"proposed_fix": proposed_fix,
			}
		)

	def visit_ClassDef(self, node: ast.ClassDef) -> None:
		self.scope_stack.append(node.name)
		self.loop_depth_stack.append(0)
		self.generic_visit(node)
		self.loop_depth_stack.pop()
		self.scope_stack.pop()

	def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
		self.scope_stack.append(node.name)
		scope = self._current_scope()

		if not self.is_test:
			# Check @frappe.whitelist(allow_guest=True)
			for dec in node.decorator_list:
				if isinstance(dec, ast.Call):
					dname = dotted_name(dec.func)
					if dname == "frappe.whitelist":
						if any(kw.arg == "allow_guest" and is_true_constant(kw.value) for kw in dec.keywords):
							self._emit(
								rule="allow-guest-inventory",
								title=f"Guest-accessible endpoint `{scope}` in `{Path(self.rel_path).name}`",
								category="security",
								severity="medium",
								line_hint=node.lineno,
								symbol=scope,
								safe_for_auto_fix=False,
								effort="small",
								evidence=f"{self.rel_path}:{node.lineno} declares @frappe.whitelist(allow_guest=True) on {scope}.",
								proposed_fix="Audit rate limiting, input validation, and data-exposure boundaries; mark wontfix once verified safe.",
							)

			# Check long function
			end_line = getattr(node, "end_lineno", None)
			if end_line is not None:
				fn_len = end_line - node.lineno + 1
				if fn_len > DEFAULT_THRESHOLDS["long_function_loc"]:
					self._emit(
						rule="long-function",
						title=f"Long function `{scope}` ({fn_len} LOC) in `{Path(self.rel_path).name}`",
						category="file-size",
						severity="medium" if fn_len < 220 else "high",
						line_hint=node.lineno,
						symbol=scope,
						safe_for_auto_fix=False,
						effort="medium",
						evidence=f"{self.rel_path}:{node.lineno}-{end_line} defines `{scope}` spanning {fn_len} lines.",
						proposed_fix="Extract cohesive pure helpers while preserving public behavior and adding unit tests first.",
					)

		self.loop_depth_stack.append(0)
		self.generic_visit(node)
		self.loop_depth_stack.pop()
		self.scope_stack.pop()

	def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
		self._visit_func(node)

	def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
		self._visit_func(node)

	def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
		self.loop_depth_stack[-1] += 1
		self.generic_visit(node)
		self.loop_depth_stack[-1] -= 1

	def visit_For(self, node: ast.For) -> None:
		self._visit_loop(node)

	def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
		self._visit_loop(node)

	def visit_While(self, node: ast.While) -> None:
		self._visit_loop(node)

	def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
		if not self.is_test:
			is_broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"})
			is_noop_body = len(node.body) == 1 and (
				isinstance(node.body[0], ast.Pass)
				or (
					isinstance(node.body[0], ast.Expr)
					and isinstance(node.body[0].value, ast.Constant)
					and node.body[0].value.value is Ellipsis
				)
			)
			if is_broad and is_noop_body:
				scope = self._current_scope()
				self._emit(
					rule="swallowed-exception",
					title=f"Swallowed exception in `{scope}` ({Path(self.rel_path).name}:{node.lineno})",
					category="correctness",
					severity="medium",
					line_hint=node.lineno,
					symbol=f"{scope}:L{node.lineno}",
					safe_for_auto_fix=True,
					effort="small",
					evidence=f"{self.rel_path}:{node.lineno} catches broad exception and silently ignores it.",
					proposed_fix="Catch a specific exception type or log the failure via `frappe.log_error` / structured logger.",
				)
		self.generic_visit(node)

	def visit_Call(self, node: ast.Call) -> None:
		if not self.is_test:
			cname = dotted_name(node.func)
			scope = self._current_scope()

			# 1. Potential SQL injection via dynamic string formatting in *.db.sql(...)
			if cname and (cname == "frappe.db.sql" or cname.endswith(".db.sql")) and node.args:
				if is_dynamic_sql_expr(node.args[0]):
					self._emit(
						rule="sqli-candidate",
						title=f"Dynamic SQL query in `{scope}` ({Path(self.rel_path).name}:{node.lineno})",
						category="security",
						severity="high",
						line_hint=node.lineno,
						symbol=f"{scope}:sql",
						safe_for_auto_fix=False,
						effort="small",
						evidence=f"{self.rel_path}:{node.lineno} passes formatted/concatenated SQL expression to `{cname}`.",
						proposed_fix="Parameterize all dynamic values with `%s` / named placeholders (or Frappe QueryBuilder) and validate any dynamic identifiers against an allowlist.",
					)

			# 2. N+1 query candidate inside loop
			if cname in N_PLUS_ONE_CALLS and self.loop_depth_stack[-1] > 0:
				self._emit(
					rule="n-plus-one-candidate",
					title=f"Potential N+1 `{cname}` inside loop in `{scope}`",
					category="performance",
					severity="medium",
					line_hint=node.lineno,
					symbol=f"{scope}:{cname}",
					safe_for_auto_fix=True,
					effort="medium",
					evidence=f"{self.rel_path}:{node.lineno} calls `{cname}` inside a loop within `{scope}`.",
					proposed_fix="Batch-fetch records before the loop with `frappe.get_all(..., filters={'name': ['in', ids]})` or `frappe.get_cached_doc` where appropriate.",
				)

			# 3. Unbounded get_all / get_list
			if cname in GET_ALL_CALLS:
				has_limit = any(kw.arg in LIMIT_KWARGS or kw.arg is None for kw in node.keywords)
				if not has_limit:
					self._emit(
						rule="unbounded-get-all",
						title=f"Unbounded `{cname}` without limit in `{scope}`",
						category="performance",
						severity="low",
						line_hint=node.lineno,
						symbol=f"{scope}:{cname}",
						safe_for_auto_fix=True,
						effort="small",
						evidence=f"{self.rel_path}:{node.lineno} calls `{cname}` without `limit` / `limit_page_length`.",
						proposed_fix="Add an explicit `limit_page_length` / `limit` or document why full-table retrieval is bounded by filters.",
					)

			# 4. ignore_permissions=True audit inventory
			if any(kw.arg == "ignore_permissions" and is_true_constant(kw.value) for kw in node.keywords):
				self._emit(
					rule="ignore-permissions-audit",
					title=f"`ignore_permissions=True` usage in `{scope}` ({Path(self.rel_path).name})",
					category="security",
					severity="medium",
					line_hint=node.lineno,
					symbol=f"{scope}:ignore_permissions",
					safe_for_auto_fix=False,
					effort="small",
					evidence=f"{self.rel_path}:{node.lineno} passes `ignore_permissions=True` inside `{scope}`.",
					proposed_fix="Verify explicit authorization/ownership check precedes the privileged call; mark wontfix once audited safe.",
				)

		self.generic_visit(node)


def scan_file(repo_root: Path, fpath: Path, zone_id: str) -> tuple[int, list[dict[str, Any]], list[str]]:
	rel = normalize_rel(str(fpath.relative_to(repo_root)))
	is_test = is_test_path(rel)
	findings: list[dict[str, Any]] = []
	warnings: list[str] = []

	try:
		text = fpath.read_text(encoding="utf-8", errors="replace")
	except OSError as exc:
		return 0, [], [f"Could not read {rel}: {exc}"]

	lines = text.splitlines()
	loc = len(lines)

	# File-size checks
	if is_test:
		if loc > DEFAULT_THRESHOLDS["test_file_size_critical_loc"]:
			findings.append(
				{
					"rule": "file-size-critical",
					"fingerprint": compute_fingerprint("file-size-critical", rel, "<file>"),
					"title": f"Oversized test module `{Path(rel).name}` ({loc} LOC)",
					"category": "file-size",
					"severity": "low",
					"zone": zone_id,
					"files": [rel],
					"line_hint": 1,
					"symbol": "<file>",
					"source": "static-scanner",
					"safe_for_auto_fix": False,
					"estimated_effort": "medium",
					"evidence": f"{rel} has {loc} lines (threshold > {DEFAULT_THRESHOLDS['test_file_size_critical_loc']}).",
					"proposed_fix": "Split test classes by subdomain into sibling test modules.",
				}
			)
	else:
		if loc > DEFAULT_THRESHOLDS["file_size_critical_loc"]:
			findings.append(
				{
					"rule": "file-size-critical",
					"fingerprint": compute_fingerprint("file-size-critical", rel, "<file>"),
					"title": f"Critical file size `{Path(rel).name}` ({loc} LOC)",
					"category": "file-size",
					"severity": "high",
					"zone": zone_id,
					"files": [rel],
					"line_hint": 1,
					"symbol": "<file>",
					"source": "static-scanner",
					"safe_for_auto_fix": False,
					"estimated_effort": "large",
					"evidence": f"{rel} has {loc} lines (critical threshold > {DEFAULT_THRESHOLDS['file_size_critical_loc']}).",
					"proposed_fix": "Plan a scoped 1-file modularization extracting pure helpers while preserving all public re-exports.",
				}
			)
		elif loc > DEFAULT_THRESHOLDS["file_size_warning_loc"]:
			findings.append(
				{
					"rule": "file-size-warning",
					"fingerprint": compute_fingerprint("file-size-warning", rel, "<file>"),
					"title": f"Large file `{Path(rel).name}` ({loc} LOC)",
					"category": "file-size",
					"severity": "medium",
					"zone": zone_id,
					"files": [rel],
					"line_hint": 1,
					"symbol": "<file>",
					"source": "static-scanner",
					"safe_for_auto_fix": False,
					"estimated_effort": "medium",
					"evidence": f"{rel} has {loc} lines (warning threshold > {DEFAULT_THRESHOLDS['file_size_warning_loc']}).",
					"proposed_fix": "Consider extracting cohesive helper submodules once test coverage is verified.",
				}
			)

	# AST checks for Python
	if fpath.suffix.lower() == ".py":
		try:
			tree = ast.parse(text, filename=rel)
			analyzer = PythonFileAnalyzer(rel, zone_id, is_test)
			analyzer.visit(tree)
			findings.extend(analyzer.findings)
		except SyntaxError as exc:
			warnings.append(f"SyntaxError parsing {rel}: {exc}")

	# FIXME/HACK/XXX markers in non-test code
	if not is_test and fpath.suffix.lower() in {".py", ".js"}:
		for idx, line in enumerate(lines, start=1):
			m = TODO_RE.search(line)
			if m:
				tag, snippet = m.group(1), m.group(2).strip()[:120]
				findings.append(
					{
						"rule": "todo-marker",
						"fingerprint": compute_fingerprint("todo-marker", rel, f"{tag}:L{idx}"),
						"title": f"{tag} marker in `{Path(rel).name}:{idx}`",
						"category": "hygiene",
						"severity": "low",
						"zone": zone_id,
						"files": [rel],
						"line_hint": idx,
						"symbol": f"{tag}:L{idx}",
						"source": "static-scanner",
						"safe_for_auto_fix": False,
						"estimated_effort": "small",
						"evidence": f"{rel}:{idx}: {tag} {snippet}".strip(),
						"proposed_fix": "Resolve the underlying issue or remove obsolete comment.",
					}
				)
				break  # cap at 1 marker finding per file to keep noise low

	return loc, findings, warnings


def run_scan(
	repo_root: Path,
	zones_data: dict[str, Any],
	only_zone: str | None = None,
) -> dict[str, Any]:
	all_findings: list[dict[str, Any]] = []
	warnings: list[str] = []
	zone_summaries: dict[str, dict[str, Any]] = {}
	total_files = 0
	total_loc = 0
	files_over_800 = 0
	files_over_1500 = 0
	largest_files: list[tuple[int, str, str]] = []

	for zone in zones_data.get("zones", []):
		zid = zone["id"]
		if only_zone and zid != only_zone:
			continue
		files = iter_zone_files(repo_root, zone)
		z_loc = 0
		z_findings: list[dict[str, Any]] = []
		for fpath in files:
			loc, f_findings, f_warnings = scan_file(repo_root, fpath, zid)
			z_loc += loc
			total_files += 1
			total_loc += loc
			rel = normalize_rel(str(fpath.relative_to(repo_root)))
			largest_files.append((loc, rel, zid))
			if not is_test_path(rel):
				if loc > DEFAULT_THRESHOLDS["file_size_warning_loc"]:
					files_over_800 += 1
				if loc > DEFAULT_THRESHOLDS["file_size_critical_loc"]:
					files_over_1500 += 1
			z_findings.extend(f_findings)
			warnings.extend(f_warnings)

		churn = git_churn_for_paths(repo_root, zone.get("paths", []), zone.get("last_audited_commit"))
		rule_counts = Counter(item["rule"] for item in z_findings)
		zone_summaries[zid] = {
			"files": len(files),
			"loc": z_loc,
			"git_commits_since_audit": churn,
			"finding_count": len(z_findings),
			"findings_by_rule": dict(rule_counts),
		}
		all_findings.extend(z_findings)

	largest_files.sort(reverse=True)
	by_rule = Counter(item["rule"] for item in all_findings)
	by_category = Counter(item["category"] for item in all_findings)

	return {
		"scanned_at": utc_now_iso(),
		"git": git_info(repo_root),
		"totals": {
			"files": total_files,
			"loc": total_loc,
			"non_test_files_over_800": files_over_800,
			"non_test_files_over_1500": files_over_1500,
			"findings": len(all_findings),
		},
		"findings_by_rule": dict(by_rule),
		"findings_by_category": dict(by_category),
		"largest_files": [{"loc": l, "path": p, "zone": z} for l, p, z in largest_files[:15]],
		"zones": zone_summaries,
		"findings": all_findings,
		"warnings": warnings,
	}


def upsert_issues(
	backlog_data: dict[str, Any],
	zones_by_id: dict[str, dict[str, Any]],
	new_items: list[dict[str, Any]],
	run_id: str | None = None,
) -> dict[str, Any]:
	existing_fps = {item["fingerprint"]: item for item in backlog_data.get("items", []) if item.get("fingerprint")}
	added: list[str] = []
	skipped_duplicate: list[str] = []
	errors: list[str] = []
	next_id = int(backlog_data.get("next_id", 1))

	for raw in new_items:
		zone_id = raw.get("zone", "")
		if zone_id not in zones_by_id:
			errors.append(f"Unknown zone '{zone_id}' for issue '{raw.get('title')}'")
			continue
		category = str(raw.get("category", "hygiene")).lower()
		if category not in VALID_CATEGORIES:
			errors.append(f"Invalid category '{category}' for issue '{raw.get('title')}'")
			continue
		severity = str(raw.get("severity", "medium")).lower()
		if severity not in VALID_SEVERITIES:
			errors.append(f"Invalid severity '{severity}' for issue '{raw.get('title')}'")
			continue
		effort = str(raw.get("estimated_effort", "medium")).lower()
		if effort not in VALID_EFFORTS:
			effort = "medium"

		files = [normalize_rel(f) for f in raw.get("files", []) if f]
		primary_file = files[0] if files else zone_id
		rule = str(raw.get("rule") or category)
		symbol = str(raw.get("symbol") or raw.get("title", "")[:60])
		fp = raw.get("fingerprint") or compute_fingerprint(rule, primary_file, symbol)

		if fp in existing_fps:
			skipped_duplicate.append(existing_fps[fp]["id"])
			continue

		item_id = f"CH-{next_id:03d}"
		next_id += 1
		zone_meta = zones_by_id[zone_id]
		verification_cmd = raw.get("verification_cmd") or zone_meta.get("verification_cmd", "")

		record = {
			"id": item_id,
			"fingerprint": fp,
			"rule": rule,
			"title": str(raw["title"]).strip(),
			"category": category,
			"severity": severity,
			"zone": zone_id,
			"files": files,
			"line_hint": raw.get("line_hint"),
			"symbol": symbol,
			"source": str(raw.get("source") or "llm-audit"),
			"safe_for_auto_fix": bool(raw.get("safe_for_auto_fix", False)),
			"estimated_effort": effort,
			"status": "open",
			"discovered_at": utc_now_iso(),
			"discovered_run": run_id or raw.get("discovered_run") or "manual",
			"resolved_run": None,
			"commit": None,
			"branch": None,
			"evidence": str(raw.get("evidence", "")).strip(),
			"proposed_fix": str(raw.get("proposed_fix", "")).strip(),
			"verification_cmd": verification_cmd,
			"notes": [],
		}
		backlog_data["items"].append(record)
		existing_fps[fp] = record
		added.append(item_id)

	backlog_data["next_id"] = next_id
	return {"added": added, "skipped_duplicate": skipped_duplicate, "errors": errors}


def score_audit_zones(
	repo_root: Path,
	zones_data: dict[str, Any],
	backlog_data: dict[str, Any],
) -> list[dict[str, Any]]:
	now = dt.datetime.now(dt.timezone.utc)
	open_by_zone = Counter(
		item["zone"] for item in backlog_data.get("items", []) if item.get("status") == "open"
	)
	scored: list[dict[str, Any]] = []

	for zone in zones_data.get("zones", []):
		zid = zone["id"]
		crit = zone.get("criticality", "medium")
		cooldown = int(zone.get("cooldown_days", 7))
		last_at_raw = zone.get("last_audited_at")
		churn = git_churn_for_paths(repo_root, zone.get("paths", []), zone.get("last_audited_commit"))

		score = CRITICALITY_WEIGHT.get(crit, 15)
		reasons: list[str] = [f"criticality={crit}"]

		days_since: int | None = None
		if not last_at_raw:
			score += 100
			reasons.append("never-audited")
		else:
			try:
				last_dt = dt.datetime.fromisoformat(last_at_raw.replace("Z", "+00:00"))
				days_since = max(0, (now - last_dt).days)
			except ValueError:
				days_since = cooldown
			if days_since < cooldown and churn == 0:
				score -= 30
				reasons.append(f"in-cooldown({days_since}d/{cooldown}d)")
			else:
				age_bonus = min(days_since * 2, 40)
				score += age_bonus
				reasons.append(f"age={days_since}d")

		if churn > 0:
			churn_bonus = min(churn * 3, 45)
			score += churn_bonus
			reasons.append(f"git-churn={churn}")

		model_hint = "gemini-4-neon-high" if crit == "critical" else "gemini-4-neon-medium"
		scored.append(
			{
				"zone": zid,
				"repo_area": zone.get("repo_area"),
				"criticality": crit,
				"score": score,
				"days_since_audit": days_since,
				"git_commits_since_audit": churn,
				"open_backlog_count": open_by_zone.get(zid, 0),
				"recommended_auditor_model": model_hint,
				"paths": zone.get("paths", []),
				"reasons": reasons,
			}
		)

	scored.sort(key=lambda z: (-z["score"], z["zone"]))
	return scored


def plan_next_batch(
	repo_root: Path,
	zones_data: dict[str, Any],
	backlog_data: dict[str, Any],
	preset: str,
	audit_zones_override: int | None = None,
	fix_tracks_override: int | None = None,
) -> dict[str, Any]:
	cfg = PRESETS[preset]
	n_audit = audit_zones_override if audit_zones_override is not None else cfg["audit_zones"]
	n_fix = fix_tracks_override if fix_tracks_override is not None else cfg["fix_tracks"]
	max_items = cfg["max_items_per_fixer"]
	max_loc = cfg["max_loc_per_fixer"]

	zones_by_id = {z["id"]: z for z in zones_data.get("zones", [])}
	ranked_audits = score_audit_zones(repo_root, zones_data, backlog_data)
	selected_audits = ranked_audits[:n_audit]

	# Group open auto-fixable items by zone
	open_auto = [
		item
		for item in backlog_data.get("items", [])
		if item.get("status") == "open" and item.get("safe_for_auto_fix") is True and item.get("zone") in zones_by_id
	]
	by_zone: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for item in open_auto:
		by_zone[item["zone"]].append(item)

	zone_candidates: list[tuple[tuple[int, int, str], str, list[dict[str, Any]]]] = []
	for zid, items in by_zone.items():
		items.sort(
			key=lambda it: (
				-SEVERITY_RANK.get(it.get("severity", "low"), 1),
				-EFFORT_RANK.get(it.get("estimated_effort", "medium"), 2),
				it.get("id", ""),
			)
		)
		top = items[0]
		sort_key = (
			-SEVERITY_RANK.get(top.get("severity", "low"), 1),
			-EFFORT_RANK.get(top.get("estimated_effort", "medium"), 2),
			zid,
		)
		zone_candidates.append((sort_key, zid, items[:max_items]))

	zone_candidates.sort(key=lambda entry: entry[0])

	# Pick up to n_fix strictly disjoint zones; prefer pairing 1 agent-os with 1 frappe if severities tie
	selected_fix_tracks: list[dict[str, Any]] = []
	used_zones: set[str] = set()
	chosen_entries: list[tuple[str, list[dict[str, Any]]]] = []

	for _, zid, items in zone_candidates:
		if len(chosen_entries) >= n_fix:
			break
		if zid in used_zones:
			continue
		used_zones.add(zid)
		chosen_entries.append((zid, items))

	# If we have 2 frappe zones and an equally severe agent-os candidate exists, swap second to save a bench slot
	if len(chosen_entries) == 2 and n_fix >= 2:
		z0, z1 = chosen_entries[0][0], chosen_entries[1][0]
		if zones_by_id[z0].get("requires_bench_slot") and zones_by_id[z1].get("requires_bench_slot"):
			z1_sev = SEVERITY_RANK.get(chosen_entries[1][1][0].get("severity", "low"), 1)
			for _, zid, items in zone_candidates:
				if zid not in used_zones and not zones_by_id[zid].get("requires_bench_slot"):
					if SEVERITY_RANK.get(items[0].get("severity", "low"), 1) >= z1_sev:
						chosen_entries[1] = (zid, items)
						break

	next_slot = 1
	any_neon_fixer = False
	for idx, (zid, items) in enumerate(chosen_entries):
		worker_name = f"fixer-{chr(ord('a') + idx)}"
		zmeta = zones_by_id[zid]
		slot: int | None = None
		if zmeta.get("requires_bench_slot"):
			slot = next_slot
			next_slot += 1
		verify_cmd = zmeta.get("verification_cmd", "").replace("{slot}", str(slot or 3))

		# Model triage: routine tests/hygiene/small fixes -> hydrogen-medium; perf/file-size/security/medium+ -> neon-medium
		needs_neon = any(
			it.get("category") in {"security", "performance", "file-size"} or it.get("estimated_effort") != "small"
			for it in items
		)
		model = "gemini-4-neon-medium" if needs_neon else "gemini-4-hydrogen-medium"
		if needs_neon:
			any_neon_fixer = True

		selected_fix_tracks.append(
			{
				"worker": worker_name,
				"workspace": "share",
				"zone": zid,
				"repo_area": zmeta.get("repo_area"),
				"bench_slot": slot,
				"max_loc": max_loc,
				"recommended_model": model,
				"allowed_paths": zmeta.get("paths", []),
				"verification_cmd": verify_cmd,
				"items": [
					{
						"id": it["id"],
						"title": it["title"],
						"category": it["category"],
						"severity": it["severity"],
						"files": it.get("files", []),
						"line_hint": it.get("line_hint"),
						"proposed_fix": it.get("proposed_fix", ""),
					}
					for it in items
				],
			}
		)

	verifier_model = "gemini-4-carbon-high" if any_neon_fixer else "gemini-4-neon-high"
	fallback_note = None
	if n_fix > len(selected_fix_tracks):
		fallback_note = (
			f"Requested {n_fix} fix track(s), found {len(selected_fix_tracks)} zone(s) with open "
			"safe_for_auto_fix items. Run `scan --seed` first, run an `audit-only` night, or assign "
			"the remaining fixer slot to additive test-coverage hardening in a disjoint zone."
		)

	return {
		"preset": preset,
		"coordinator_model": "gemini-4-neon-medium",
		"monitor_model": "gemini-4-hydrogen-low",
		"verifier_model": verifier_model if selected_fix_tracks else None,
		"verifier_bench_slot": 3 if any(t["bench_slot"] is not None for t in selected_fix_tracks) else None,
		"audit_tracks": selected_audits,
		"fix_tracks": selected_fix_tracks,
		"fallback_note": fallback_note,
	}


def append_metrics_snapshot(
	state_dir: Path,
	scan_result: dict[str, Any],
	backlog_data: dict[str, Any],
	zones_data: dict[str, Any],
) -> dict[str, Any]:
	status_counts = Counter(item.get("status", "open") for item in backlog_data.get("items", []))
	open_sev_counts = Counter(
		item.get("severity", "low")
		for item in backlog_data.get("items", [])
		if item.get("status") == "open"
	)
	audited_zones = sum(1 for z in zones_data.get("zones", []) if z.get("last_audited_at"))
	snapshot = {
		"timestamp": utc_now_iso(),
		"git_branch": scan_result["git"].get("branch"),
		"git_sha": scan_result["git"].get("short_sha"),
		"totals": scan_result["totals"],
		"findings_by_rule": scan_result["findings_by_rule"],
		"backlog_by_status": dict(status_counts),
		"open_by_severity": dict(open_sev_counts),
		"zones_audited": audited_zones,
		"zones_total": len(zones_data.get("zones", [])),
	}
	metrics_path = state_dir / "metrics_history.jsonl"
	with metrics_path.open("a", encoding="utf-8") as fh:
		fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
	return snapshot


def next_run_id(runs_dir: Path) -> str:
	max_num = 0
	pattern = re.compile(r"_run-(\d+)\.md$")
	if runs_dir.exists():
		for fpath in runs_dir.iterdir():
			m = pattern.search(fpath.name)
			if m:
				max_num = max(max_num, int(m.group(1)))
	return f"run-{max_num + 1:03d}"


def cmd_preflight(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_data = load_json(state_dir / "zones.json")
	backlog_data = load_json(state_dir / "backlog.json")
	ginfo = git_info(repo_root)
	zone_check = validate_zones_disjoint(repo_root, zones_data)

	errors: list[str] = []
	warnings: list[str] = []

	branch = ginfo.get("branch")
	if not ginfo.get("available"):
		errors.append("Git repository metadata unavailable.")
	elif branch in {"main", "master", "HEAD", None} and not args.allow_main:
		errors.append(
			f"Current branch is '{branch}'. Create and check out a dedicated nightly branch "
			"(e.g., `git checkout -b nightly/YYYY-MM-DD`) before running autonomous ops."
		)

	dirty_non_state = [f for f in ginfo.get("dirty_files", []) if not f.startswith("code-health/")]
	if dirty_non_state and not args.allow_dirty:
		errors.append(f"Working tree has uncommitted non-ledger files: {dirty_non_state[:10]}")

	if not zone_check["ok"]:
		if zone_check["overlaps"]:
			errors.append(f"Overlapping zone paths detected: {zone_check['overlaps']}")
		if zone_check["missing_paths"]:
			errors.append(f"Missing zone paths detected: {zone_check['missing_paths']}")

	agents_md = repo_root / "ism-agent-os" / "rules" / "AGENTS.md"
	agents_bytes = agents_md.stat().st_size if agents_md.exists() else None
	if agents_bytes is not None and agents_bytes >= DEFAULT_THRESHOLDS["agents_md_max_bytes"]:
		errors.append(
			f"ism-agent-os/rules/AGENTS.md is {agents_bytes} bytes "
			f"(must be < {DEFAULT_THRESHOLDS['agents_md_max_bytes']})."
		)

	payload = {
		"ok": len(errors) == 0,
		"git": ginfo,
		"zone_validation": {
			"ok": zone_check["ok"],
			"total_zones": len(zones_data.get("zones", [])),
			"total_mapped_files": zone_check["total_mapped_files"],
			"overlaps": zone_check["overlaps"],
			"missing_paths": zone_check["missing_paths"],
		},
		"backlog_items": len(backlog_data.get("items", [])),
		"agents_md_bytes": agents_bytes,
		"errors": errors,
		"warnings": warnings,
	}

	if args.json:
		print(json.dumps(payload, indent=2))
	else:
		status_label = "PASS" if payload["ok"] else "FAIL"
		print(f"Preflight: {status_label}")
		print(f"  Branch: {branch} ({ginfo.get('short_sha')})")
		print(
			f"  Zones: {payload['zone_validation']['total_zones']} disjoint "
			f"({payload['zone_validation']['total_mapped_files']} mapped files)"
		)
		print(f"  Backlog items: {payload['backlog_items']}")
		if agents_bytes is not None:
			print(f"  ism-agent-os/rules/AGENTS.md: {agents_bytes} / {DEFAULT_THRESHOLDS['agents_md_max_bytes']} bytes")
		for err in errors:
			print(f"  ERROR: {err}", file=sys.stderr)

	return 0 if payload["ok"] else 1


def cmd_scan(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_data = load_json(state_dir / "zones.json")
	backlog_path = state_dir / "backlog.json"
	backlog_data = load_json(backlog_path)

	scan_result = run_scan(repo_root, zones_data, only_zone=args.zone)

	seed_summary = None
	if args.seed:
		zones_by_id = {z["id"]: z for z in zones_data.get("zones", [])}
		seed_summary = upsert_issues(backlog_data, zones_by_id, scan_result["findings"], run_id=args.run or "scan")
		save_json(backlog_path, backlog_data)

	metrics_snapshot = None
	if args.record_metrics:
		metrics_snapshot = append_metrics_snapshot(state_dir, scan_result, backlog_data, zones_data)

	output = {
		"totals": scan_result["totals"],
		"findings_by_rule": scan_result["findings_by_rule"],
		"findings_by_category": scan_result["findings_by_category"],
		"largest_files": scan_result["largest_files"][:10],
		"zones": scan_result["zones"],
		"warnings": scan_result["warnings"],
		"seed_summary": seed_summary,
		"metrics_recorded": metrics_snapshot is not None,
	}
	if args.include_findings:
		output["findings"] = scan_result["findings"]

	if args.json:
		print(json.dumps(output, indent=2))
	else:
		t = scan_result["totals"]
		print(
			f"Scanned {t['files']} files ({t['loc']:,} LOC) — "
			f"{t['findings']} static signals | "
			f">800 LOC: {t['non_test_files_over_800']} | >1500 LOC: {t['non_test_files_over_1500']}"
		)
		if scan_result["findings_by_rule"]:
			rules_str = ", ".join(f"{k}={v}" for k, v in sorted(scan_result["findings_by_rule"].items()))
			print(f"  Rules: {rules_str}")
		if seed_summary:
			print(
				f"  Seeded backlog: +{len(seed_summary['added'])} added, "
				f"{len(seed_summary['skipped_duplicate'])} deduplicated"
			)
		if metrics_snapshot:
			print("  Recorded metrics snapshot to metrics_history.jsonl")
	return 0


def cmd_next_batch(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_data = load_json(state_dir / "zones.json")
	backlog_data = load_json(state_dir / "backlog.json")
	plan = plan_next_batch(
		repo_root,
		zones_data,
		backlog_data,
		preset=args.preset,
		audit_zones_override=args.audit_zones,
		fix_tracks_override=args.fix_tracks,
	)

	if args.json:
		print(json.dumps(plan, indent=2))
	else:
		print(f"Batch Plan (preset={plan['preset']}):")
		print(f"  Coordinator: {plan['coordinator_model']} | Monitor: {plan['monitor_model']} | Verifier: {plan['verifier_model']}")
		print("  Audit Track(s):")
		for z in plan["audit_tracks"]:
			print(
				f"    - {z['zone']} (score={z['score']}, model={z['recommended_auditor_model']}, "
				f"reasons={', '.join(z['reasons'])})"
			)
		print("  Fix Track(s):")
		if not plan["fix_tracks"]:
			print("    - (none selected)")
		for t in plan["fix_tracks"]:
			ids = ", ".join(i["id"] for i in t["items"])
			print(
				f"    - {t['worker']} -> {t['zone']} (slot={t['bench_slot']}, model={t['recommended_model']}, "
				f"items=[{ids}], max_loc={t['max_loc']})"
			)
		if plan["fallback_note"]:
			print(f"  Note: {plan['fallback_note']}")
	return 0


def cmd_add_issue(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_data = load_json(state_dir / "zones.json")
	backlog_path = state_dir / "backlog.json"
	backlog_data = load_json(backlog_path)
	zones_by_id = {z["id"]: z for z in zones_data.get("zones", [])}

	if args.from_json:
		raw_payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
		incoming = raw_payload if isinstance(raw_payload, list) else [raw_payload]
	else:
		if not args.title or not args.zone:
			print("ERROR: --title and --zone are required when --from-json is not provided.", file=sys.stderr)
			return 2
		incoming = [
			{
				"title": args.title,
				"category": args.category,
				"severity": args.severity,
				"zone": args.zone,
				"files": [f.strip() for f in (args.files or "").split(",") if f.strip()],
				"line_hint": args.line_hint,
				"symbol": args.symbol or "",
				"source": args.source,
				"safe_for_auto_fix": args.safe_for_auto_fix,
				"estimated_effort": args.effort,
				"evidence": args.evidence or "",
				"proposed_fix": args.proposed_fix or "",
				"verification_cmd": args.verification_cmd or "",
			}
		]

	result = upsert_issues(backlog_data, zones_by_id, incoming, run_id=args.run)
	save_json(backlog_path, backlog_data)

	if args.json:
		print(json.dumps(result, indent=2))
	else:
		print(
			f"Added: {result['added']} | Skipped duplicates: {result['skipped_duplicate']} | "
			f"Errors: {len(result['errors'])}"
		)
		for err in result["errors"]:
			print(f"  ERROR: {err}", file=sys.stderr)
	return 0 if not result["errors"] else 1


def cmd_update_issue(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	backlog_path = state_dir / "backlog.json"
	backlog_data = load_json(backlog_path)
	target = next((it for it in backlog_data.get("items", []) if it.get("id") == args.id), None)
	if not target:
		print(f"ERROR: Issue '{args.id}' not found.", file=sys.stderr)
		return 1

	if args.status:
		if args.status not in VALID_STATUSES:
			print(f"ERROR: Invalid status '{args.status}'.", file=sys.stderr)
			return 2
		target["status"] = args.status
	if args.resolved_run:
		target["resolved_run"] = args.resolved_run
	if args.commit:
		target["commit"] = args.commit
	if args.branch:
		target["branch"] = args.branch
	if args.safe_for_auto_fix is not None:
		target["safe_for_auto_fix"] = args.safe_for_auto_fix == "true"
	if args.note:
		target.setdefault("notes", []).append({"at": utc_now_iso(), "text": args.note})
	target["updated_at"] = utc_now_iso()

	save_json(backlog_path, backlog_data)
	if args.json:
		print(json.dumps(target, indent=2))
	else:
		print(f"Updated {target['id']}: status={target['status']}")
	return 0


def cmd_mark_audited(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_path = state_dir / "zones.json"
	zones_data = load_json(zones_path)
	target = next((z for z in zones_data.get("zones", []) if z.get("id") == args.zone), None)
	if not target:
		print(f"ERROR: Zone '{args.zone}' not found.", file=sys.stderr)
		return 1

	ginfo = git_info(repo_root)
	commit = args.commit or ginfo.get("sha")
	target["last_audited_at"] = utc_now_iso()
	target["last_audited_run"] = args.run
	target["last_audited_commit"] = commit
	target["audit_count"] = int(target.get("audit_count", 0)) + 1

	save_json(zones_path, zones_data)
	if args.json:
		print(json.dumps(target, indent=2))
	else:
		print(f"Marked zone '{target['id']}' audited in {args.run} (commit={commit or 'n/a'}).")
	return 0


def cmd_init_run(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	runs_dir = state_dir / "runs"
	runs_dir.mkdir(parents=True, exist_ok=True)
	run_id = args.run_id or next_run_id(runs_dir)
	today = dt.date.today().isoformat()
	ginfo = git_info(repo_root)
	branch = ginfo.get("branch") or "unknown"
	base_sha = ginfo.get("short_sha") or "unknown"

	template_path = runs_dir / "_TEMPLATE.md"
	template = template_path.read_text(encoding="utf-8")
	content = (
		template.replace("{{RUN_ID}}", run_id)
		.replace("{{DATE}}", today)
		.replace("{{PRESET}}", args.preset)
		.replace("{{BRANCH}}", branch)
		.replace("{{BASE_SHA}}", base_sha)
		.replace("{{AUDIT_ZONES}}", args.audit_zones or "TBD")
		.replace("{{FIX_ZONES}}", args.fix_zones or "TBD")
	)

	out_path = runs_dir / f"{today}_{run_id}.md"
	if out_path.exists() and not args.force:
		print(f"ERROR: Run file already exists: {out_path}", file=sys.stderr)
		return 1
	out_path.write_text(content, encoding="utf-8")

	payload = {
		"run_id": run_id,
		"path": normalize_rel(str(out_path.relative_to(repo_root))),
		"branch": branch,
		"base_sha": base_sha,
		"preset": args.preset,
	}
	if args.json:
		print(json.dumps(payload, indent=2))
	else:
		print(f"Initialized {run_id}: {payload['path']}")
	return 0


def cmd_status(args: argparse.Namespace, repo_root: Path, state_dir: Path) -> int:
	zones_data = load_json(state_dir / "zones.json")
	backlog_data = load_json(state_dir / "backlog.json")
	ginfo = git_info(repo_root)
	due_zones = score_audit_zones(repo_root, zones_data, backlog_data)

	items = backlog_data.get("items", [])
	by_status = Counter(it.get("status", "open") for it in items)
	open_by_sev = Counter(it.get("severity", "low") for it in items if it.get("status") == "open")
	open_auto = sum(1 for it in items if it.get("status") == "open" and it.get("safe_for_auto_fix"))

	runs_dir = state_dir / "runs"
	run_files = sorted(
		p.name for p in runs_dir.iterdir() if p.is_file() and p.name.endswith(".md") and not p.name.startswith("_")
	) if runs_dir.exists() else []

	metrics_path = state_dir / "metrics_history.jsonl"
	latest_metric = None
	if metrics_path.exists():
		lines = [ln.strip() for ln in metrics_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
		if lines:
			latest_metric = json.loads(lines[-1])

	payload = {
		"branch": ginfo.get("branch"),
		"head_sha": ginfo.get("short_sha"),
		"backlog_total": len(items),
		"backlog_by_status": dict(by_status),
		"open_by_severity": dict(open_by_sev),
		"open_safe_for_auto_fix": open_auto,
		"zones_total": len(zones_data.get("zones", [])),
		"zones_audited": sum(1 for z in zones_data.get("zones", []) if z.get("last_audited_at")),
		"top_due_zones": due_zones[:5],
		"recent_runs": run_files[-5:],
		"latest_metric": latest_metric,
	}

	if args.json:
		print(json.dumps(payload, indent=2))
	else:
		print(f"Code Health Status — branch={payload['branch']} ({payload['head_sha']})")
		print(
			f"  Zones audited: {payload['zones_audited']}/{payload['zones_total']} | "
			f"Backlog: {payload['backlog_total']} total "
			f"(open={by_status.get('open', 0)}, auto-fixable={open_auto}, fixed={by_status.get('fixed', 0)}, "
			f"wontfix={by_status.get('wontfix', 0)})"
		)
		if open_by_sev:
			sev_str = ", ".join(f"{s}={open_by_sev.get(s, 0)}" for s in ("critical", "high", "medium", "low"))
			print(f"  Open by severity: {sev_str}")
		print("  Top due zones for next audit:")
		for z in due_zones[:5]:
			print(f"    - {z['zone']:24s} score={z['score']:3d} ({', '.join(z['reasons'])})")
		print(f"  Recorded runs: {len(run_files)} (latest: {run_files[-1] if run_files else 'none'})")
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="ISM Code Health scanner and Swarm state CLI.")
	parser.add_argument("--root", type=Path, default=None, help="Override repository root path.")
	parser.add_argument("--state-dir", type=Path, default=None, help="Override code-health directory path.")
	sub = parser.add_subparsers(dest="command", required=True)

	p_pre = sub.add_parser("preflight", help="Verify non-main branch, zone disjointness, and repo invariants.")
	p_pre.add_argument("--allow-main", action="store_true")
	p_pre.add_argument("--allow-dirty", action="store_true")
	p_pre.add_argument("--json", action="store_true")

	p_scan = sub.add_parser("scan", help="Run zero-token deterministic AST & git-churn scan.")
	p_scan.add_argument("--zone", default=None, help="Limit scan to a single zone ID.")
	p_scan.add_argument("--seed", action="store_true", help="Upsert findings into backlog.json.")
	p_scan.add_argument("--record-metrics", action="store_true", help="Append snapshot to metrics_history.jsonl.")
	p_scan.add_argument("--run", default=None, help="Run ID to tag newly seeded items.")
	p_scan.add_argument("--include-findings", action="store_true", help="Include full findings array in JSON output.")
	p_scan.add_argument("--json", action="store_true")

	p_batch = sub.add_parser("next-batch", help="Select due audit zones and disjoint fix tracks.")
	p_batch.add_argument("--preset", choices=sorted(PRESETS.keys()), default="standard")
	p_batch.add_argument("--audit-zones", type=int, default=None)
	p_batch.add_argument("--fix-tracks", type=int, default=None)
	p_batch.add_argument("--json", action="store_true")

	p_add = sub.add_parser("add-issue", help="Add one or more issues to backlog.json with deduplication.")
	p_add.add_argument("--from-json", default=None, help="Path to JSON file containing an issue object or list.")
	p_add.add_argument("--title", default=None)
	p_add.add_argument("--category", default="correctness", choices=sorted(VALID_CATEGORIES))
	p_add.add_argument("--severity", default="medium", choices=sorted(VALID_SEVERITIES))
	p_add.add_argument("--zone", default=None)
	p_add.add_argument("--files", default=None, help="Comma-separated repo-relative file paths.")
	p_add.add_argument("--line-hint", type=int, default=None)
	p_add.add_argument("--symbol", default=None)
	p_add.add_argument("--source", default="llm-audit")
	p_add.add_argument("--safe-for-auto-fix", action="store_true")
	p_add.add_argument("--effort", default="medium", choices=sorted(VALID_EFFORTS))
	p_add.add_argument("--evidence", default=None)
	p_add.add_argument("--proposed-fix", default=None)
	p_add.add_argument("--verification-cmd", default=None)
	p_add.add_argument("--run", default=None)
	p_add.add_argument("--json", action="store_true")

	p_upd = sub.add_parser("update-issue", help="Update an existing backlog issue's status or metadata.")
	p_upd.add_argument("--id", required=True)
	p_upd.add_argument("--status", choices=sorted(VALID_STATUSES), default=None)
	p_upd.add_argument("--resolved-run", default=None)
	p_upd.add_argument("--commit", default=None)
	p_upd.add_argument("--branch", default=None)
	p_upd.add_argument("--safe-for-auto-fix", choices=["true", "false"], default=None)
	p_upd.add_argument("--note", default=None)
	p_upd.add_argument("--json", action="store_true")

	p_aud = sub.add_parser("mark-audited", help="Record that a zone completed an LLM audit.")
	p_aud.add_argument("--zone", required=True)
	p_aud.add_argument("--run", required=True)
	p_aud.add_argument("--commit", default=None)
	p_aud.add_argument("--json", action="store_true")

	p_init = sub.add_parser("init-run", help="Create the next sequential run dossier in runs/.")
	p_init.add_argument("--preset", choices=sorted(PRESETS.keys()), default="standard")
	p_init.add_argument("--run-id", default=None)
	p_init.add_argument("--audit-zones", default=None)
	p_init.add_argument("--fix-zones", default=None)
	p_init.add_argument("--force", action="store_true")
	p_init.add_argument("--json", action="store_true")

	p_stat = sub.add_parser("status", help="Show backlog, zone rotation, and metrics dashboard.")
	p_stat.add_argument("--json", action="store_true")

	return parser


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	repo_root = (args.root or default_repo_root()).resolve()
	state_dir = (args.state_dir or default_state_dir(repo_root)).resolve()

	handlers = {
		"preflight": cmd_preflight,
		"scan": cmd_scan,
		"next-batch": cmd_next_batch,
		"add-issue": cmd_add_issue,
		"update-issue": cmd_update_issue,
		"mark-audited": cmd_mark_audited,
		"init-run": cmd_init_run,
		"status": cmd_status,
	}
	return handlers[args.command](args, repo_root, state_dir)


if __name__ == "__main__":
	raise SystemExit(main())

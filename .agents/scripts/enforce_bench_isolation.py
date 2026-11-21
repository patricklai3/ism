"""PreToolUse hook enforcing deterministic Frappe bench isolation across Git worktrees."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_PRIMARY_REPO = r"D:\ism"

PRIMARY_SYNC_SCRIPTS = {
	"sync-to-wsl.ps1",
	"watch-sync.ps1",
	"watch-sync.mjs",
	"promote-to-main.ps1",
}

DIRECT_WSL_OR_SLOT_SCRIPTS = {
	"provision-slots.sh",
	"run-in-slot.sh",
	"slot-common.sh",
}

PROTECTED_BENCH_MARKERS = (
	"/workspace/development/frappe-bench",
	"/home/real2/projects/.project/frappe_docker/development/frappe-bench",
	"development.localhost",
	"_c6b75eebb8edc84d",
	"bench-slots/slot-",
	"_ism_slot_",
	"wt1.localhost",
	"wt2.localhost",
	"wt3.localhost",
	"wt4.localhost",
	"wt5.localhost",
	"wt6.localhost",
	"wt7.localhost",
	"wt8.localhost",
)

EXEC_INFRA_TOOLS = {
	"wsl",
	"wsl.exe",
	"docker",
	"docker.exe",
	"rsync",
	"bench",
	"mariadb",
	"mysql",
	"redis-cli",
}

BENCH_MUTATION_SUBCOMMANDS = {
	"migrate",
	"run-tests",
	"serve",
	"start",
	"execute",
	"clear-cache",
	"clear-website-cache",
	"new-site",
	"drop-site",
	"restore",
	"console",
	"mariadb",
	"mysql",
	"build",
	"watch",
	"worker",
	"schedule",
}

CD_COMMANDS = {"cd", "chdir", "set-location", "sl", "push-location", "pushd"}
POP_COMMANDS = {"pop-location", "popd"}
PATH_VALUE_FLAGS = {"-file", "-f", "-source", "-path", "-worktree", "-watchdir"}
SOURCE_TARGET_FLAGS = {"-file", "-f", "-source", "-path", "-worktree", "-watchdir"}
POWERSHELL_HOSTS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
POSIX_SHELL_HOSTS = {"bash", "sh", "zsh", "dash"}


def allow() -> None:
	print(json.dumps({"decision": "allow"}))


def deny(reason: str) -> None:
	print(json.dumps({"decision": "deny", "reason": reason}))


def _split_shell_segments(command_line: str) -> list[str]:
	"""Split a shell command string on separators outside single/double quotes."""
	segments: list[str] = []
	buf: list[str] = []
	in_single = False
	in_double = False
	i = 0
	n = len(command_line)
	while i < n:
		ch = command_line[i]
		if ch == "'" and not in_double:
			in_single = not in_single
			buf.append(ch)
			i += 1
			continue
		if ch == '"' and not in_single:
			in_double = not in_double
			buf.append(ch)
			i += 1
			continue
		if ch == "\\" and in_double and i + 1 < n:
			buf.append(command_line[i : i + 2])
			i += 2
			continue
		if not in_single and not in_double:
			two = command_line[i : i + 2]
			if two in ("&&", "||"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 2
				continue
			if ch in (";", "|", "\n", "\r"):
				segments.append("".join(buf).strip())
				buf.clear()
				i += 1
				continue
		buf.append(ch)
		i += 1
	tail = "".join(buf).strip()
	if tail:
		segments.append(tail)
	return [s for s in segments if s]


def _tokenize_segment(segment: str) -> list[str]:
	escaped = re.sub(r'\\(?![\\"\'])', r"\\\\", segment)
	try:
		return shlex.split(escaped)
	except ValueError:
		return segment.split()


def _strip_leading_wrappers(tokens: list[str]) -> list[str]:
	"""Strip rtk prefixes and PowerShell call operators from leading tokens."""
	idx = 0
	while idx < len(tokens):
		tok = tokens[idx]
		base = os.path.basename(tok).lower()
		if base in ("rtk", "rtk.exe"):
			idx += 1
			if idx < len(tokens) and tokens[idx].lower() == "bypass":
				idx += 1
			continue
		if tok in ("&", ".", "call"):
			idx += 1
			continue
		break
	return tokens[idx:]


def _get_primary_root() -> Path:
	return Path(os.environ.get("ISM_PRIMARY_REPO") or DEFAULT_PRIMARY_REPO)


def _norm_abs_path(path: Path) -> str:
	try:
		resolved = str(path.resolve())
	except OSError:
		resolved = os.path.abspath(str(path))
	return os.path.normcase(os.path.normpath(resolved))


def _nearest_existing_dir(path: Path) -> Optional[Path]:
	cur = path
	try:
		if cur.is_file():
			cur = cur.parent
	except OSError:
		pass
	while True:
		try:
			if cur.is_dir():
				return cur
		except OSError:
			pass
		parent = cur.parent
		if parent == cur:
			return None
		cur = parent


def _run_git(cwd: Path, *git_args: str) -> Optional[str]:
	try:
		proc = subprocess.run(
			["git", "-C", str(cwd), *git_args],
			capture_output=True,
			text=True,
			timeout=3,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return None
	if proc.returncode != 0:
		return None
	return proc.stdout.strip()


def is_primary_trunk(
	repo_path: Optional[Path],
	primary_root: Path,
	cache: Optional[dict[str, tuple[bool, str]]] = None,
) -> tuple[bool, str]:
	"""Verify path belongs to the canonical primary repo checkout on branch 'main'."""
	if repo_path is None:
		return (False, "working directory is unknown")

	existing_dir = _nearest_existing_dir(repo_path)
	if existing_dir is None:
		return (False, f"path '{repo_path}' does not exist")

	cache_key = _norm_abs_path(existing_dir)
	if cache is not None and cache_key in cache:
		return cache[cache_key]

	def _store(result: tuple[bool, str]) -> tuple[bool, str]:
		if cache is not None:
			cache[cache_key] = result
		return result

	toplevel = _run_git(existing_dir, "rev-parse", "--show-toplevel")
	if not toplevel:
		return _store((False, f"'{existing_dir}' is not inside a git repository"))

	if _norm_abs_path(Path(toplevel)) != _norm_abs_path(primary_root):
		return _store((False, f"repo root '{toplevel}' is not primary checkout '{primary_root}'"))

	git_dir = _run_git(Path(toplevel), "rev-parse", "--path-format=absolute", "--git-dir")
	common_dir = _run_git(Path(toplevel), "rev-parse", "--path-format=absolute", "--git-common-dir")
	if not git_dir or not common_dir:
		return _store((False, "unable to inspect git worktree metadata"))
	if _norm_abs_path(Path(git_dir)) != _norm_abs_path(Path(common_dir)):
		return _store((False, "linked git worktree detected"))

	branch = _run_git(Path(toplevel), "rev-parse", "--abbrev-ref", "HEAD")
	if branch != "main":
		return _store((False, f"active branch is '{branch}' (expected 'main')"))

	return _store((True, "ok"))


def _resolve_candidate_path(raw: str, cwd_path: Optional[Path]) -> Path:
	p = Path(raw)
	if p.is_absolute() or cwd_path is None:
		return p
	return cwd_path / p


def _extract_cd_target(tokens: list[str]) -> Optional[str]:
	for tok in tokens[1:]:
		low = tok.lower()
		if low in ("-path", "-literalpath", "/d"):
			continue
		if low.startswith("-path:") or low.startswith("-literalpath:"):
			return tok.split(":", 1)[1]
		if tok.startswith("-") or tok.startswith("/"):
			continue
		return tok
	return None


def _is_powershell_encoded_flag(token: str) -> bool:
	low = token.lower()
	if not low.startswith("-") or len(low) < 2:
		return False
	# PowerShell accepts -e, -en, -enc, ..., -encodedcommand (exclude -ep / -executionpolicy).
	return "-encodedcommand".startswith(low)


def _is_powershell_command_flag(token: str) -> bool:
	low = token.lower()
	if not low.startswith("-") or len(low) < 2:
		return False
	return "-command".startswith(low)


def _extract_nested_commands(tokens: list[str]) -> tuple[list[str], bool]:
	"""Extract embedded command strings and whether an unreadable encoded command appeared."""
	if not tokens:
		return ([], False)
	head = os.path.basename(tokens[0]).lower()
	nested: list[str] = []
	bad_encoded = False

	if head in POWERSHELL_HOSTS:
		i = 1
		while i < len(tokens):
			tok = tokens[i]
			if _is_powershell_encoded_flag(tok):
				if i + 1 < len(tokens):
					raw_b64 = tokens[i + 1]
					try:
						decoded = base64.b64decode(raw_b64, validate=True).decode("utf-16-le")
						if decoded.strip():
							nested.append(decoded)
					except Exception:
						bad_encoded = True
					i += 2
					continue
				bad_encoded = True
			elif _is_powershell_command_flag(tok):
				if i + 1 < len(tokens):
					nested.append(" ".join(tokens[i + 1 :]))
				break
			i += 1
	elif head in ("cmd", "cmd.exe"):
		for idx, tok in enumerate(tokens[1:], start=1):
			if tok.lower() in ("/c", "/k") and idx + 1 < len(tokens):
				nested.append(" ".join(tokens[idx + 1 :]))
				break
	elif head in ("wsl", "wsl.exe") or head in POSIX_SHELL_HOSTS:
		for idx, tok in enumerate(tokens[1:], start=1):
			if tok in ("-c", "-lc", "-ic") and idx + 1 < len(tokens):
				nested.append(" ".join(tokens[idx + 1 :]))
				break
			if tok == "--" and idx + 1 < len(tokens):
				break

	return (nested, bad_encoded)


def _extract_flag_paths(tokens: list[str], cwd_path: Optional[Path]) -> tuple[list[Path], list[Path]]:
	"""Return (all_referenced_paths, source_override_paths) from explicit command arguments."""
	all_paths: list[Path] = []
	source_paths: list[Path] = []

	i = 0
	while i < len(tokens):
		tok = tokens[i]
		low = tok.lower()

		# Script path directly in token
		base = os.path.basename(tok.replace("\\", "/")).lower()
		if base in PRIMARY_SYNC_SCRIPTS or base in ("provision-bench-slots.ps1", "test-in-slot.ps1"):
			if "/" in tok or "\\" in tok:
				p = _resolve_candidate_path(tok, cwd_path)
				all_paths.append(p)
				source_paths.append(p)

		# git -C <path>
		if low == "git" and i + 2 < len(tokens) and tokens[i + 1] == "-C":
			p = _resolve_candidate_path(tokens[i + 2], cwd_path)
			all_paths.append(p)
			source_paths.append(p)
			i += 3
			continue

		# -Flag:value or -Flag=value
		matched_inline = False
		for flag in PATH_VALUE_FLAGS:
			for sep in (":", "="):
				prefix = flag + sep
				if low.startswith(prefix):
					val = tok[len(prefix) :]
					if val:
						p = _resolve_candidate_path(val, cwd_path)
						all_paths.append(p)
						if flag in SOURCE_TARGET_FLAGS:
							source_paths.append(p)
					matched_inline = True
					break
			if matched_inline:
				break
		if matched_inline:
			i += 1
			continue

		if low in PATH_VALUE_FLAGS and i + 1 < len(tokens):
			val = tokens[i + 1]
			p = _resolve_candidate_path(val, cwd_path)
			all_paths.append(p)
			if low in SOURCE_TARGET_FLAGS:
				source_paths.append(p)
			i += 2
			continue

		i += 1

	return (all_paths, source_paths)


def _segment_script_names(tokens: list[str]) -> set[str]:
	names: set[str] = set()
	for tok in tokens:
		norm = tok.replace("\\", "/")
		base = os.path.basename(norm).lower()
		if base:
			names.add(base)
		# Also handle -File:foo.ps1 or embedded script path
		for sep in (":", "="):
			if sep in norm:
				tail = os.path.basename(norm.split(sep, 1)[1]).lower()
				if tail:
					names.add(tail)
	return names


PROVISION_MUTATING_FLAGS = {
	"-recreate",
	"-refreshgolden",
	"-forcegolden",
	"-rebuildbase",
	"-warmstale",
	"-slotsync",
	"-slots",
}


def _is_provision_status_only(tokens: list[str]) -> bool:
	low_tokens = {tok.lower() for tok in tokens}
	has_readonly = ("-status" in low_tokens) or any(
		tok == "-history" or tok.startswith("-history:") or tok.startswith("-history=") for tok in low_tokens
	)
	has_mutating = bool(low_tokens & PROVISION_MUTATING_FLAGS) or any(
		tok.startswith(flag + ":") or tok.startswith(flag + "=")
		for tok in low_tokens
		for flag in PROVISION_MUTATING_FLAGS
	)
	return has_readonly and not has_mutating


def _has_infra_exec_tool(tokens: list[str]) -> bool:
	for tok in tokens:
		base = os.path.basename(tok.replace("\\", "/")).lower()
		if base in EXEC_INFRA_TOOLS:
			return True
	return False


def _is_python_or_node_inline_bypass(tokens: list[str], segment_lower: str) -> bool:
	if not tokens:
		return False
	head = os.path.basename(tokens[0]).lower()
	is_inline_py = head in ("python", "python.exe", "python3", "python3.exe", "py", "py.exe") and "-c" in tokens
	is_inline_node = head in ("node", "node.exe") and ("-e" in tokens or "--eval" in tokens)
	if not (is_inline_py or is_inline_node):
		return False
	if not any(m in segment_lower for m in PROTECTED_BENCH_MARKERS):
		return False
	return any(w in segment_lower for w in ("wsl", "docker", "bench", "mariadb", "mysql", "redis-cli", "rsync"))


def _segment_violates_isolation(segment: str, tokens: list[str]) -> Optional[str]:
	"""Return violation label if this segment touches protected bench/slot surfaces."""
	scripts = _segment_script_names(tokens)
	seg_low = segment.lower()

	# Explicitly allowed safe wrappers in any worktree
	if "test-in-slot.ps1" in scripts:
		return None
	if "provision-bench-slots.ps1" in scripts and _is_provision_status_only(tokens):
		return None

	for s in PRIMARY_SYNC_SCRIPTS:
		if s in scripts or s in seg_low:
			return f"invokes '{s}' outside primary trunk"

	if "provision-bench-slots.ps1" in scripts or "provision-bench-slots.ps1" in seg_low:
		return "invokes mutating 'provision-bench-slots.ps1' outside primary trunk"

	for s in DIRECT_WSL_OR_SLOT_SCRIPTS:
		if s in scripts or s in seg_low:
			return f"invokes '{s}' directly bypassing slot wrapper"

	has_infra = _has_infra_exec_tool(tokens)
	if has_infra or _is_python_or_node_inline_bypass(tokens, seg_low):
		for marker in PROTECTED_BENCH_MARKERS:
			if marker in seg_low:
				return f"targets protected bench path/site '{marker}' directly"

		if "redis-cli" in seg_low and (
			"flushall" in seg_low or "flushdb" in seg_low or re.search(r"(?:^|\s)-n\s+0(?:\s|$)", seg_low)
		):
			return "issues destructive redis-cli command outside run-in-slot.sh"

		if re.search(r"(?:^|\s)(?:--port\s+8000|:8000)(?:\s|$|/)", seg_low):
			return "targets primary port 8000 directly"

		# Direct `bench ... migrate|run-tests|...` inside container/WSL
		if re.search(r"(?:^|\s|/)bench(?:\s|$)", seg_low):
			tok_lows = {t.lower() for t in tokens}
			if tok_lows & BENCH_MUTATION_SUBCOMMANDS or any(
				re.search(rf"(?:^|\s){re.escape(sub)}(?:\s|$)", seg_low) for sub in BENCH_MUTATION_SUBCOMMANDS
			):
				return "invokes bench directly instead of test-in-slot.ps1"

	return None


def _denial_message(context_reason: str, violation: str) -> str:
	return (
		f"Blocked by bench isolation policy ({context_reason}; {violation}). "
		"Primary bench and development.localhost may only be modified from primary checkout 'D:\\ism' on branch 'main'. "
		"Run `powershell -ExecutionPolicy Bypass -File scripts\\test-in-slot.ps1 ...` "
		"to sync, migrate, and test in an isolated bench slot."
	)


def _evaluate_command_text(
	command_line: str,
	cwd_path: Optional[Path],
	primary_root: Path,
	cache: dict[str, tuple[bool, str]],
	depth: int = 0,
) -> tuple[str, Optional[str]]:
	if depth > 5:
		return ("allow", None)

	dir_stack: list[Path] = []
	cur_cwd = cwd_path

	for seg in _split_shell_segments(command_line):
		raw_tokens = _tokenize_segment(seg)
		tokens = _strip_leading_wrappers(raw_tokens)
		if not tokens:
			continue

		head_low = tokens[0].lower()
		if head_low in CD_COMMANDS:
			target_raw = _extract_cd_target(tokens)
			if target_raw:
				if head_low in ("push-location", "pushd") and cur_cwd is not None:
					dir_stack.append(cur_cwd)
				cur_cwd = _resolve_candidate_path(target_raw, cur_cwd)
			continue
		if head_low in POP_COMMANDS:
			if dir_stack:
				cur_cwd = dir_stack.pop()
			continue

		# Check context authorization for current cwd and any explicit -Source/-Path arguments
		_, source_paths = _extract_flag_paths(tokens, cur_cwd)
		cwd_ok, cwd_why = is_primary_trunk(cur_cwd, primary_root, cache)

		unauth_reason: Optional[str] = None
		if not cwd_ok:
			unauth_reason = cwd_why
		else:
			for sp in source_paths:
				sp_ok, sp_why = is_primary_trunk(sp, primary_root, cache)
				if not sp_ok:
					unauth_reason = f"argument path '{sp}': {sp_why}"
					break

		# Inspect nested shells (-EncodedCommand, -Command, cmd /c, bash -c)
		nested_cmds, bad_encoded = _extract_nested_commands(tokens)
		if bad_encoded and unauth_reason is not None:
			return ("deny", _denial_message(unauth_reason, "unreadable PowerShell -EncodedCommand from non-primary context"))

		for inner in nested_cmds:
			decision, reason = _evaluate_command_text(inner, cur_cwd, primary_root, cache, depth + 1)
			if decision == "deny":
				return (decision, reason)

		if unauth_reason is not None:
			violation = _segment_violates_isolation(seg, tokens)
			if violation is not None:
				return ("deny", _denial_message(unauth_reason, violation))

	return ("allow", None)


def evaluate_hook_payload(payload: dict[str, Any]) -> tuple[str, Optional[str]]:
	"""Evaluate a PreToolUse JSON payload and return ('allow'|'deny', optional_reason)."""
	tool_call = payload.get("toolCall") or {}
	if tool_call.get("name") != "run_command":
		return ("allow", None)

	args = tool_call.get("args") or {}
	command_line = args.get("CommandLine") or ""
	if not command_line:
		return ("allow", None)

	initial_cwd = payload.get("cwd") or payload.get("workingDirectory")
	cwd_path = Path(initial_cwd) if initial_cwd else None
	primary_root = _get_primary_root()
	cache: dict[str, tuple[bool, str]] = {}
	return _evaluate_command_text(command_line, cwd_path, primary_root, cache)


def main() -> None:
	try:
		payload = json.load(sys.stdin)
	except Exception:
		allow()
		return

	decision, reason = evaluate_hook_payload(payload)
	if decision == "deny":
		deny(reason or "Blocked by bench isolation policy.")
	else:
		allow()


if __name__ == "__main__":
	main()

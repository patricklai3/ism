"""PreToolUse hook enforcing required fields and ISM Pre-Dispatch Summary callbacks."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_OS_ROOT = REPO_ROOT / "ism-agent-os"
if str(AGENT_OS_ROOT) not in sys.path:
	sys.path.insert(0, str(AGENT_OS_ROOT))

from tools.cli import (  # noqa: E402
	READ_ONLY_COMMANDS,
	_batch_item_to_argv,
	_build_parser,
	_load_batch_items,
	validate_write_command_args,
)

PRE_DISPATCH_MARKER = "ISM Pre-Dispatch Summary"


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


def _is_python_token(token: str) -> bool:
	base = os.path.basename(token).lower()
	return base in ("python", "python.exe", "python3", "python3.exe", "py", "py.exe")


def _extract_cli_argv(tokens: list[str]) -> Optional[list[str]]:
	"""Return CLI argv tokens if a command segment invokes the ISM Agent OS CLI."""
	if not tokens:
		return None
	first_base = os.path.basename(tokens[0]).lower()
	if first_base in ("ism-agent-os", "ism-agent-os.exe"):
		return tokens[1:]
	if _is_python_token(tokens[0]):
		for idx in range(1, len(tokens)):
			tok = tokens[idx]
			if tok == "-m" and idx + 1 < len(tokens):
				if tokens[idx + 1] in ("tools.cli", "ism_agent_os.cli"):
					return tokens[idx + 2 :]
				return None
			if tok.startswith("-"):
				continue
			if os.path.basename(tok).lower() == "cli.py" and (
				"tools" in tok.replace("\\", "/").lower() or tok.lower() == "cli.py"
			):
				return tokens[idx + 1 :]
			return None
	return None


def _collect_cli_invocations(command_line: str, initial_cwd: Optional[str]) -> list[tuple[list[str], Optional[Path]]]:
	cwd_path = Path(initial_cwd) if initial_cwd else None
	invocations: list[tuple[list[str], Optional[Path]]] = []
	for seg in _split_shell_segments(command_line):
		tokens = _tokenize_segment(seg)
		if not tokens:
			continue
		cmd_head = tokens[0].lower()
		if cmd_head in ("cd", "chdir", "set-location", "push-location", "pushd") and len(tokens) >= 2:
			target = Path(tokens[1])
			cwd_path = target if target.is_absolute() or cwd_path is None else (cwd_path / target)
			continue
		argv = _extract_cli_argv(tokens)
		if argv is not None:
			invocations.append((argv, cwd_path))
	return invocations


def _load_full_step_map(transcript_path: Path) -> dict[Any, dict[str, Any]]:
	full_path = transcript_path.with_name("transcript_full.jsonl")
	if not full_path.is_file():
		return {}
	by_index: dict[Any, dict[str, Any]] = {}
	for raw in full_path.read_text(encoding="utf-8", errors="replace").splitlines():
		if not raw.strip():
			continue
		try:
			entry = json.loads(raw)
		except json.JSONDecodeError:
			continue
		if "step_index" in entry:
			by_index[entry["step_index"]] = entry
	return by_index


def _load_transcript_entries(transcript_path: Optional[str]) -> list[dict[str, Any]]:
	if not transcript_path:
		return []
	path = Path(transcript_path)
	if not path.is_file():
		return []
	full_map: Optional[dict[Any, dict[str, Any]]] = None
	entries: list[dict[str, Any]] = []
	for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
		if not raw.strip():
			continue
		try:
			entry = json.loads(raw)
		except json.JSONDecodeError:
			continue
		truncated = set(entry.get("truncated_fields") or [])
		if "content" in truncated or "tool_calls" in truncated:
			if full_map is None:
				full_map = _load_full_step_map(path)
			full_entry = full_map.get(entry.get("step_index"))
			if full_entry:
				entry = full_entry
		entries.append(entry)
	return entries


_AGENT_MSG_SENDER_RE = re.compile(r"\[Message\][^\n]*\bsender=([^\s]+)")


def _is_turn_boundary(entry: dict[str, Any]) -> bool:
	"""Return True for explicit user inputs or direct parent/peer agent replies (never background tasks)."""
	etype = entry.get("type")
	if etype == "USER_INPUT":
		return True
	if etype == "SYSTEM_MESSAGE":
		content = entry.get("content") or ""
		match = _AGENT_MSG_SENDER_RE.search(content)
		if match and "/task-" not in match.group(1):
			return True
	return False


def _extract_model_texts(entry: dict[str, Any]) -> list[str]:
	texts: list[str] = []
	content = entry.get("content") or ""
	if content:
		texts.append(content)
	for tc in entry.get("tool_calls") or []:
		if tc.get("name") == "send_message":
			args = tc.get("arguments") or tc.get("args") or {}
			if isinstance(args, str):
				try:
					args = json.loads(args)
				except json.JSONDecodeError:
					args = {}
			if isinstance(args, dict):
				msg = args.get("Message") or args.get("message") or ""
				if msg:
					texts.append(msg)
	return texts


def _has_confirmed_summary_in_transcript(entries: list[dict[str, Any]], write_commands: list[str]) -> tuple[bool, str]:
	"""Verify the assistant turn immediately preceding the latest user/caller confirmation presented a ready summary."""
	user_indices = [idx for idx, e in enumerate(entries) if _is_turn_boundary(e)]
	if len(user_indices) < 2:
		return (
			False,
			"No preceding ISM Pre-Dispatch Summary and user confirmation found in transcript.",
		)

	prev_user_idx = user_indices[-2]
	latest_user_idx = user_indices[-1]
	prior_turn_texts: list[str] = []
	for entry in entries[prev_user_idx + 1 : latest_user_idx]:
		if entry.get("type") == "PLANNER_RESPONSE" and entry.get("source") == "MODEL":
			prior_turn_texts.extend(_extract_model_texts(entry))

	if not prior_turn_texts:
		return (
			False,
			"No ISM Pre-Dispatch Summary found in the assistant response preceding the user's latest reply.",
		)

	combined = "\n".join(prior_turn_texts)
	if PRE_DISPATCH_MARKER not in combined:
		return (
			False,
			f"Missing '{PRE_DISPATCH_MARKER}' callback in the preceding assistant response.",
		)

	# Prefer the last structured heading block so casual inline mentions later in the reply don't mask it.
	heading_blocks = re.split(r"(?m)^#{1,6}\s+" + re.escape(PRE_DISPATCH_MARKER) + r"\b", combined)
	if len(heading_blocks) > 1:
		summary_block = heading_blocks[-1]
	else:
		summary_block = combined.split(PRE_DISPATCH_MARKER)[-1]
	if "[MISSING" in summary_block or "[INVALID" in summary_block:
		return (
			False,
			"The preceding ISM Pre-Dispatch Summary still had [MISSING] or [INVALID] fields. Collect valid values and present an updated READY summary before dispatching.",
		)

	missing_cmds = [cmd for cmd in write_commands if cmd not in summary_block]
	if missing_cmds:
		return (
			False,
			f"The preceding ISM Pre-Dispatch Summary did not cover write command(s): {', '.join(missing_cmds)}.",
		)
	return (True, "")


def _parse_write_commands(
	argv: list[str], cwd_path: Optional[Path], parser: argparse.ArgumentParser
) -> tuple[Optional[list[argparse.Namespace]], Optional[str]]:
	"""Return list of write sub-command Namespaces, or an error string if blocked before parsing."""
	if any(tok in ("-h", "--help") for tok in argv):
		return ([], None)

	err_buf = io.StringIO()
	old_cwd = os.getcwd()
	try:
		if cwd_path and cwd_path.is_dir():
			os.chdir(cwd_path)
		import contextlib

		with contextlib.redirect_stderr(err_buf):
			args = parser.parse_args(argv)
		if bool(getattr(args, "dry_run", False)):
			return ([], None)
		if args.command in READ_ONLY_COMMANDS:
			return ([], None)
		if args.command == "batch":
			if (
				not getattr(args, "cmds", None)
				and getattr(args, "file", None) is None
				and getattr(args, "input", None) is None
			):
				return (
					None,
					"Blocked ISM write batch: piped stdin batch cannot be verified; pass --cmd or --file @path.json.",
				)
			items = _load_batch_items(args)
			writes: list[argparse.Namespace] = []
			for item in items:
				sub_argv = _batch_item_to_argv(item)
				with contextlib.redirect_stderr(err_buf):
					sub_args = parser.parse_args(sub_argv)
				if sub_args.command not in READ_ONLY_COMMANDS:
					writes.append(sub_args)
			return (writes, None)
		return ([args], None)
	except SystemExit:
		if any(tok in ("--dry-run", "--preview") for tok in argv):
			return ([], None)
		# Only block parse errors if a known write subcommand token appears in argv.
		for tok in argv:
			if tok not in READ_ONLY_COMMANDS and tok in parser._subparsers._group_actions[0].choices:
				msg = err_buf.getvalue().strip() or "invalid or incomplete write CLI arguments"
				return (None, f"Blocked ISM write command '{tok}': {msg}")
		return ([], None)
	except (OSError, ValueError) as exc:
		return (None, f"Blocked ISM write batch: {exc}")
	finally:
		os.chdir(old_cwd)


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
	invocations = _collect_cli_invocations(command_line, initial_cwd)
	if not invocations:
		return ("allow", None)

	parser = _build_parser()
	write_namespaces: list[argparse.Namespace] = []
	for cli_argv, seg_cwd in invocations:
		writes, err = _parse_write_commands(cli_argv, seg_cwd, parser)
		if err:
			return ("deny", err)
		if writes:
			write_namespaces.extend(writes)

	if not write_namespaces:
		return ("allow", None)

	for ns in write_namespaces:
		missing = validate_write_command_args(ns)
		if missing:
			return (
				"deny",
				f"Blocked ISM write command '{ns.command}': missing required field(s): {', '.join(missing)}. "
				"Run with --dry-run and present '### ISM Pre-Dispatch Summary' to the user first.",
			)

	transcript_path = payload.get("transcriptPath") or payload.get("transcript_path")
	entries = _load_transcript_entries(transcript_path)
	ok, reason = _has_confirmed_summary_in_transcript(entries, [ns.command for ns in write_namespaces])
	if not ok:
		return (
			"deny",
			f"Blocked live ISM write command(s) [{', '.join(ns.command for ns in write_namespaces)}]: {reason} "
			"Run `--dry-run`, output '### ISM Pre-Dispatch Summary' (with no [MISSING] fields), and wait for explicit user confirmation.",
		)

	return ("allow", None)


def main() -> None:
	try:
		payload = json.load(sys.stdin)
	except Exception:
		allow()
		return

	decision, reason = evaluate_hook_payload(payload)
	if decision == "deny":
		deny(reason or "Blocked by ISM write gate.")
	else:
		allow()


if __name__ == "__main__":
	main()

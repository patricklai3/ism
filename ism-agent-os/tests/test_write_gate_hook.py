"""Unit tests for the Antigravity PreToolUse ISM write-gate hook."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parents[2] / ".agents" / "scripts" / "enforce_ism_write_gate.py"


def _load_hook_module():
	spec = importlib.util.spec_from_file_location("enforce_ism_write_gate", HOOK_PATH)
	mod = importlib.util.module_from_spec(spec)
	assert spec and spec.loader
	spec.loader.exec_module(mod)
	return mod


hook_mod = _load_hook_module()


def _write_transcript(tmp_dir: str, entries: list[dict], full_entries: list[dict] | None = None) -> str:
	root = Path(tmp_dir)
	compact = root / "transcript.jsonl"
	compact.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
	if full_entries is not None:
		full = root / "transcript_full.jsonl"
		full.write_text("\n".join(json.dumps(e) for e in full_entries), encoding="utf-8")
	return str(compact)


class TestIsmWriteGateHook(unittest.TestCase):
	def test_allows_non_cli_and_read_only_cli_commands(self) -> None:
		for cmd in (
			"ast-outline d:/ism/ism-agent-os/tools/cli.py",
			"python -m unittest discover -s tests -v",
			"python -m tools.cli --json status --date 2026-11-17",
			"python -m tools.cli --json batch --cmd \"status\" --cmd \"slots --date 2026-11-17\"",
		):
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
			})
			self.assertEqual(decision, "allow", f"Unexpected deny for {cmd}: {reason}")

	def test_allows_dry_run_write_commands_without_summary(self) -> None:
		for cmd in (
			"python -m tools.cli --dry-run quote --customer Rob --vehicle VIN1 --service 'Lateral Link'",
			"python -m tools.cli quote --dry-run --customer Rob --vehicle VIN1 --service 'Lateral Link'",
			"ism-agent-os --preview checkout --repair-order RO-1",
			"python -m tools.cli batch --dry-run --cmd \"quote --customer Rob --vehicle VIN1 --odometer-in 48200 --notes Test\"",
		):
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
			})
			self.assertEqual(decision, "allow", f"Expected dry-run allow for {cmd}: {reason}")

	def test_denies_live_write_missing_required_fields(self) -> None:
		cases = [
			"python -m tools.cli --json quote --customer Rob --vehicle VIN1 --service 'Lateral Link'",
			"python -m tools.cli --json intake-slots --customer Rob --vehicle VIN1 --service 'Alignment'",
			"python -m tools.cli --json create-ro-from-entry --schedule-entry SE-1",
			"python -m tools.cli --json job-status --repair-order RO-1 --action complete",
			"python -m tools.cli --json checkout --repair-order RO-1 --odometer-out 48260 --confirm",
			"python -m tools.cli --json pay-purchase --purchase-order PO-1",
			"python -m tools.cli --json purchase-invoice",
			"python -m tools.cli --json batch --cmd \"status\" --cmd \"quote --customer Rob --vehicle VIN1 --service Brake\"",
		]
		for cmd in cases:
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
			})
			self.assertEqual(decision, "deny", f"Expected deny for {cmd}")
			self.assertIn("missing required field", reason or "")

	def test_denies_without_prior_summary_or_with_missing_marker(self) -> None:
		valid_cmd = (
			"python -m tools.cli --json quote --customer Rob --vehicle VIN1 "
			"--odometer-in 48250 --service 'Lateral Link Diagnosis'"
		)
		with tempfile.TemporaryDirectory() as tmp:
			no_summary_path = _write_transcript(
				tmp,
				[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Book Rob"},
					{"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "Checking vehicle."},
					{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Proceed"},
				],
			)
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": no_summary_path,
			})
			self.assertEqual(decision, "deny")
			self.assertIn("ISM Pre-Dispatch Summary", reason or "")

		with tempfile.TemporaryDirectory() as tmp:
			incomplete_path = _write_transcript(
				tmp,
				[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Book Rob"},
					{
						"step_index": 2,
						"type": "PLANNER_RESPONSE",
						"source": "MODEL",
						"content": "### ISM Pre-Dispatch Summary\n- Command: `quote`\n- odometer_in: [MISSING — REQUIRED]",
					},
					{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Odo 48250 go ahead"},
				],
			)
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": incomplete_path,
			})
			self.assertEqual(decision, "deny")
			self.assertIn("[MISSING]", reason or "")

	def test_allows_when_confirmed_summary_exists_including_full_transcript_fallback(self) -> None:
		valid_cmd = (
			"Push-Location d:/ism/ism-agent-os; "
			"python -m tools.cli --json quote --customer Rob --vehicle VIN1 "
			"--odometer-in 48250 --service 'Lateral Link Diagnosis'"
		)
		full_summary = (
			"### ISM Pre-Dispatch Summary\n"
			"- **Command**: `quote`\n"
			"- **Target**: ism.api.agent.create_quote\n"
			"- **Status**: `READY`\n"
			"- **Missing Required Fields**: `None`"
		)
		with tempfile.TemporaryDirectory() as tmp:
			transcript_path = _write_transcript(
				tmp,
				entries=[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Odo 48250"},
					{
						"step_index": 2,
						"type": "PLANNER_RESPONSE",
						"source": "MODEL",
						"content": "truncated...",
						"truncated_fields": ["content"],
					},
					{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Confirmed, proceed"},
				],
				full_entries=[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Odo 48250"},
					{"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": full_summary},
					{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Confirmed, proceed"},
				],
			)
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": transcript_path,
			})
			self.assertEqual(decision, "allow", f"Unexpected deny: {reason}")

	def test_subagent_send_message_and_background_task_distinction(self) -> None:
		valid_cmd = (
			"python -m tools.cli --json quote --customer Rob --vehicle VIN1 "
			"--odometer-in 48250 --service 'Lateral Link Diagnosis'"
		)
		summary = "### ISM Pre-Dispatch Summary\n- **Command**: `quote`\n- **Status**: `READY`\n- **Missing Required Fields**: `None`"
		with tempfile.TemporaryDirectory() as tmp:
			# Background task SYSTEM_MESSAGE must NOT count as confirmation
			task_path = _write_transcript(
				tmp,
				entries=[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Run test"},
					{"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": summary},
					{"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": "[Message] sender=conv-1/task-99 content=done"},
				],
			)
			decision, _ = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": task_path,
			})
			self.assertEqual(decision, "deny")

		with tempfile.TemporaryDirectory() as tmp:
			# Subagent send_message summary + parent agent confirmation allows live write
			subagent_path = _write_transcript(
				tmp,
				entries=[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Verify"},
					{
						"step_index": 2,
						"type": "PLANNER_RESPONSE",
						"source": "MODEL",
						"content": "Sent `### ISM Pre-Dispatch Summary` via send_message; waiting.",
						"tool_calls": [{"name": "send_message", "arguments": {"Recipient": "parent-uuid", "Message": summary}}],
					},
					{"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": "[Message] sender=parent-uuid content=Confirmed proceed"},
				],
			)
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": subagent_path,
			})
			self.assertEqual(decision, "allow", f"Expected subagent allow: {reason}")

		with tempfile.TemporaryDirectory() as tmp:
			invalid_summary_path = _write_transcript(
				tmp,
				entries=[
					{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Book tomorrow 9am"},
					{
						"step_index": 2,
						"type": "PLANNER_RESPONSE",
						"source": "MODEL",
						"content": "### ISM Pre-Dispatch Summary\n- Command: `book`\n- date: [INVALID: `tomorrow`]",
					},
					{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Yes proceed"},
				],
			)
			decision, reason = hook_mod.evaluate_hook_payload({
				"toolCall": {"name": "run_command", "args": {"CommandLine": valid_cmd}},
				"transcriptPath": invalid_summary_path,
			})
			self.assertEqual(decision, "deny")
			self.assertIn("[INVALID]", reason)

	def test_allows_standalone_procure_pay_and_invoice_after_summary(self) -> None:
		cases = [
			(
				"python -m tools.cli --json procure --parts-json '[{\"item_code\":\"P1\",\"qty\":1,\"supplier\":\"S1\"}]'",
				"### ISM Pre-Dispatch Summary\n- **Command**: `procure`\n- **Status**: `READY`",
			),
			(
				"python -m tools.cli --json pay-purchase --purchase-order PO-1 --payment-method Cash",
				"### ISM Pre-Dispatch Summary\n- **Command**: `pay-purchase`\n- **Status**: `READY`",
			),
			(
				"python -m tools.cli --json purchase-invoice --purchase-order PO-1 --update-stock",
				"### ISM Pre-Dispatch Summary\n- **Command**: `purchase-invoice`\n- **Status**: `READY`",
			),
		]
		with tempfile.TemporaryDirectory() as tmp:
			for idx, (cmd, summary) in enumerate(cases, start=1):
				sub_dir = Path(tmp) / f"case_{idx}"
				sub_dir.mkdir(parents=True, exist_ok=True)
				sub_p = _write_transcript(
					str(sub_dir),
					entries=[
						{"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Execute"},
						{"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": summary},
						{"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Confirmed"},
					],
				)
				decision, reason = hook_mod.evaluate_hook_payload({
					"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
					"transcriptPath": sub_p,
				})
				self.assertEqual(decision, "allow", f"Expected allow for {cmd}: {reason}")

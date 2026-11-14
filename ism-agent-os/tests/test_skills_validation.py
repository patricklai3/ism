"""Validation tests ensuring skills, rules, CLI references, and manifests stay consistent."""

from __future__ import annotations

import argparse
import json
import re
import unittest
from pathlib import Path

from tools import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
_CLI_REF_RE = re.compile(r"(?:ism-agent-os|tools\.cli(?:\s+--[a-z-]+)*)\s+([a-z][a-z-]*)")


def _known_cli_subcommands() -> set[str]:
	parser = cli._build_parser()
	commands: set[str] = set()
	for action in parser._actions:
		if isinstance(action, argparse._SubParsersAction):
			commands.update(action.choices.keys())
	return commands


class TestSkillsAndConfigs(unittest.TestCase):
	def test_all_skills_have_valid_frontmatter_and_cli_refs(self) -> None:
		skills_dir = REPO_ROOT / "skills"
		skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
		self.assertGreaterEqual(len(skill_dirs), 5)

		known_commands = _known_cli_subcommands()
		for skill_dir in skill_dirs:
			skill_md = skill_dir / "SKILL.md"
			self.assertTrue(skill_md.is_file(), f"Missing SKILL.md in {skill_dir}")
			text = skill_md.read_text(encoding="utf-8")
			self.assertTrue(text.startswith("---\n"), f"{skill_md} missing YAML frontmatter")

			end_idx = text.find("\n---", 4)
			self.assertGreater(end_idx, 4, f"{skill_md} missing closing frontmatter fence")
			frontmatter = text[4:end_idx]
			self.assertIn(f"name: {skill_dir.name}", frontmatter)
			self.assertIn("description:", frontmatter)

			cli_refs = _CLI_REF_RE.findall(text)
			self.assertTrue(cli_refs, f"{skill_md} should reference at least one CLI subcommand")
			for cmd in cli_refs:
				self.assertIn(cmd, known_commands, f"{skill_md} references unknown CLI command '{cmd}'")

	def test_rules_file_valid_and_within_size_budget(self) -> None:
		rules_path = REPO_ROOT / "rules" / "AGENTS.md"
		self.assertTrue(rules_path.is_file())
		raw = rules_path.read_bytes()
		self.assertLess(len(raw), 24000)
		text = raw.decode("utf-8")
		self.assertFalse(text.startswith("---"), "Plugin rules/AGENTS.md must not include frontmatter")

		known_commands = _known_cli_subcommands()
		for cmd in _CLI_REF_RE.findall(text):
			self.assertIn(cmd, known_commands, f"rules/AGENTS.md references unknown CLI command '{cmd}'")

	def test_platform_adapters_and_readme_have_valid_cli_refs(self) -> None:
		known_commands = _known_cli_subcommands()
		for rel in ("AGENTS.md", "README.md"):
			path = REPO_ROOT / rel
			self.assertTrue(path.is_file(), f"Missing platform file: {rel}")
			text = path.read_text(encoding="utf-8")
			for cmd in _CLI_REF_RE.findall(text):
				self.assertIn(cmd, known_commands, f"{rel} references unknown CLI command '{cmd}'")

	def test_json_manifests_are_valid(self) -> None:
		for rel in ("plugin.json", ".agents/skills.json"):
			path = REPO_ROOT / rel
			self.assertTrue(path.is_file(), f"Missing JSON config: {rel}")
			data = json.loads(path.read_text(encoding="utf-8"))
			self.assertIsInstance(data, dict)

	def test_no_stale_legacy_protocol_references_in_ism_agent_os(self) -> None:
		token = "mc" + "p"
		offenders: list[str] = []
		for path in sorted(REPO_ROOT.rglob("*")):
			if not path.is_file():
				continue
			if any(part in ("__pycache__", ".codegraph", ".git") for part in path.parts):
				continue
			if path.name == ".env":
				continue
			try:
				text = path.read_text(encoding="utf-8")
			except UnicodeDecodeError:
				continue
			if token in text.lower() or token in path.name.lower():
				offenders.append(path.relative_to(REPO_ROOT).as_posix())
		self.assertEqual(offenders, [], f"Stale protocol references found in: {offenders}")


if __name__ == "__main__":
	unittest.main()

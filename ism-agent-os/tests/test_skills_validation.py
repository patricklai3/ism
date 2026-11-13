"""Validation tests ensuring skills, rules, and JSON configs stay consistent."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tools.tool_registry import get_mcp_tools

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSkillsAndConfigs(unittest.TestCase):
	def test_all_skills_have_valid_frontmatter_and_tool_refs(self) -> None:
		skills_dir = REPO_ROOT / "skills"
		skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
		self.assertGreaterEqual(len(skill_dirs), 5)

		known_tools = {t["name"] for t in get_mcp_tools()}
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

			# Verify every backticked ism_* or frappe_* tool reference exists in tool_registry
			for tool_ref in re.findall(r"`((?:ism|frappe)_[a-z_]+)`", text):
				self.assertIn(tool_ref, known_tools, f"{skill_md} references unknown tool '{tool_ref}'")

	def test_rules_file_valid_and_within_size_budget(self) -> None:
		rules_path = REPO_ROOT / "rules" / "AGENTS.md"
		self.assertTrue(rules_path.is_file())
		raw = rules_path.read_bytes()
		self.assertLess(len(raw), 24000)
		text = raw.decode("utf-8")
		self.assertFalse(text.startswith("---"), "Plugin rules/AGENTS.md must not include frontmatter")

		known_tools = {t["name"] for t in get_mcp_tools()}
		for tool_ref in re.findall(r"`((?:ism|frappe)_[a-z_]+)`", text):
			self.assertIn(tool_ref, known_tools, f"rules/AGENTS.md references unknown tool '{tool_ref}'")

	def test_json_manifests_are_valid(self) -> None:
		json_files = [
			"plugin.json",
			"mcp_config.json",
			".mcp.json",
			".agents/skills.json",
			".agents/mcp_config.json",
			"claude_desktop_config.json",
			"cursor_mcp.json",
		]
		for rel in json_files:
			path = REPO_ROOT / rel
			self.assertTrue(path.is_file(), f"Missing JSON config: {rel}")
			data = json.loads(path.read_text(encoding="utf-8"))
			self.assertIsInstance(data, dict)


if __name__ == "__main__":
	unittest.main()

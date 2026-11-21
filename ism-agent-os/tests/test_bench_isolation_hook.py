"""Unit tests for the Antigravity PreToolUse bench-isolation hook and trunk guards."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".agents" / "scripts" / "enforce_bench_isolation.py"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_hook_module():
	spec = importlib.util.spec_from_file_location("enforce_bench_isolation", HOOK_PATH)
	mod = importlib.util.module_from_spec(spec)
	assert spec and spec.loader
	spec.loader.exec_module(mod)
	return mod


hook_mod = _load_hook_module()


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path, branch: str = "main") -> None:
	path.mkdir(parents=True, exist_ok=True)
	_git(path, "init", "-b", branch)
	_git(path, "config", "user.email", "test@example.com")
	_git(path, "config", "user.name", "Test User")
	(path / "README.md").write_text("init\n", encoding="utf-8")
	_git(path, "add", "README.md")
	_git(path, "commit", "-m", "init")


class TestBenchIsolationHook(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.tmp = tempfile.TemporaryDirectory()
		cls.root = Path(cls.tmp.name)
		cls.primary = cls.root / "primary"
		cls.linked_wt = cls.root / "linked_wt"
		cls.branch_clone = cls.root / "branch_clone"
		cls.feature_repo = cls.root / "feature_repo"

		_init_repo(cls.primary, branch="main")
		_git(cls.primary, "worktree", "add", "-b", "wt-1", str(cls.linked_wt))
		subprocess.run(
			["git", "clone", "--branch", "main", str(cls.primary), str(cls.branch_clone)],
			check=True,
			capture_output=True,
			text=True,
		)
		subprocess.run(
			["git", "clone", "--branch", "main", str(cls.primary), str(cls.feature_repo)],
			check=True,
			capture_output=True,
			text=True,
		)
		_git(cls.feature_repo, "checkout", "-b", "feat/isolation")

	@classmethod
	def tearDownClass(cls) -> None:
		try:
			_git(cls.primary, "worktree", "remove", "--force", str(cls.linked_wt))
		except subprocess.SubprocessError:
			pass
		cls.tmp.cleanup()

	def _eval(self, cmd: str, cwd: Path) -> tuple[str, str | None]:
		with mock.patch.dict(os.environ, {"ISM_PRIMARY_REPO": str(self.primary)}):
			return hook_mod.evaluate_hook_payload({
				"cwd": str(cwd),
				"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
			})

	def test_allows_safe_commands_from_worktree_and_branch_clone(self) -> None:
		safe_cmds = [
			"git status",
			"ast-outline ism-frappe/ism/api/agent.py",
			"python -m unittest discover -s ism-agent-os/tests -v",
			"try { $null = 1 } catch {}; rtk git status",
			"powershell -ExecutionPolicy Bypass -File scripts\\test-in-slot.ps1 -Module ism.tests.test_estimation_math",
			"powershell -NoProfile -File scripts\\provision-bench-slots.ps1 -Status",
			"powershell -NoProfile -File scripts\\provision-bench-slots.ps1 -History 20",
			"powershell -NoProfile -File scripts\\provision-bench-slots.ps1 -Status -History 10",
		]
		for ctx in (self.linked_wt, self.branch_clone):
			for cmd in safe_cmds:
				decision, reason = self._eval(cmd, ctx)
				self.assertEqual(decision, "allow", f"Expected allow in {ctx} for {cmd}, got: {reason}")

	def test_allows_primary_trunk_sync_and_promote(self) -> None:
		allowed_primary = [
			"powershell -ExecutionPolicy Bypass -File scripts\\sync-to-wsl.ps1 -Delete",
			"powershell -ExecutionPolicy Bypass -File scripts\\watch-sync.ps1",
			"node scripts/watch-sync.mjs",
			"powershell -ExecutionPolicy Bypass -File scripts\\promote-to-main.ps1",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1 -RefreshGolden",
			"wsl -d Ubuntu docker exec devcontainer-frappe-1 bench --site development.localhost migrate",
		]
		for cmd in allowed_primary:
			decision, reason = self._eval(cmd, self.primary)
			self.assertEqual(decision, "allow", f"Expected primary allow for {cmd}, got: {reason}")

	def test_denies_sync_watch_promote_and_mutating_provision_from_non_primary(self) -> None:
		blocked = [
			"powershell -ExecutionPolicy Bypass -File scripts\\sync-to-wsl.ps1",
			"powershell -ExecutionPolicy Bypass -File ism-frappe\\scripts\\sync-to-wsl.ps1",
			"powershell -ExecutionPolicy Bypass -File scripts\\sync-to-wsl.ps1 -AllowWorktreeOnMain",
			"ISM_ALLOW_WORKTREE_MAIN=1 powershell -File scripts\\sync-to-wsl.ps1",
			"powershell -ExecutionPolicy Bypass -File scripts\\watch-sync.ps1",
			"node scripts/watch-sync.mjs",
			"powershell -ExecutionPolicy Bypass -File scripts\\promote-to-main.ps1",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1 -Recreate",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1 -RefreshGolden",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1 -Status -Recreate",
			"powershell -ExecutionPolicy Bypass -File scripts\\provision-bench-slots.ps1 -History 20 -Recreate",
		]
		for ctx in (self.linked_wt, self.branch_clone):
			for cmd in blocked:
				decision, reason = self._eval(cmd, ctx)
				self.assertEqual(decision, "deny", f"Expected deny in {ctx} for {cmd}")
				assert reason is not None
				self.assertIn("test-in-slot.ps1", reason)
				self.assertNotIn("AllowWorktreeOnMain", reason)
				self.assertNotIn("ISM_ALLOW_WORKTREE_MAIN", reason)

	def test_denies_primary_on_feature_branch(self) -> None:
		_git(self.primary, "checkout", "-b", "feat/on-primary")
		try:
			decision, reason = self._eval(
				"powershell -ExecutionPolicy Bypass -File scripts\\sync-to-wsl.ps1",
				self.primary,
			)
			self.assertEqual(decision, "deny", f"Expected deny on non-main branch: {reason}")
		finally:
			_git(self.primary, "checkout", "main")

	def test_denies_cross_repo_source_or_file_even_from_primary(self) -> None:
		for cmd in (
			f'powershell -File scripts\\sync-to-wsl.ps1 -Source "{self.linked_wt}\\ism-frappe"',
			f'powershell -File "{self.linked_wt}\\scripts\\sync-to-wsl.ps1"',
			f'cd "{self.linked_wt}" && powershell -File scripts\\sync-to-wsl.ps1',
		):
			decision, reason = self._eval(cmd, self.primary)
			self.assertEqual(decision, "deny", f"Expected deny for cross-repo target {cmd}: {reason}")

	def test_denies_direct_wsl_docker_bench_mariadb_redis_and_slot_bypass(self) -> None:
		bypasses = [
			f'cd "{self.linked_wt}" && wsl -d Ubuntu docker exec devcontainer-frappe-1 bench --site development.localhost migrate',
			"wsl -d Ubuntu docker exec -w /workspace/development/bench-slots/slot-1 devcontainer-frappe-1 bench --site wt1.localhost run-tests",
			"wsl -d Ubuntu docker exec -w /workspace/development/bench-slots/slot-6 devcontainer-frappe-1 bench --site wt6.localhost run-tests",
			"wsl -d Ubuntu rsync -rtvc /mnt/c/wt/ /home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/",
			"wsl -d Ubuntu docker exec devcontainer-mariadb-1 mariadb -uroot -p123 _c6b75eebb8edc84d -e 'SHOW TABLES'",
			"wsl -d Ubuntu docker exec devcontainer-mariadb-1 mariadb -uroot -p123 _ism_slot_1 -e 'SHOW TABLES'",
			"wsl -d Ubuntu docker exec devcontainer-redis-cache-1 redis-cli FLUSHALL",
			"wsl -d Ubuntu docker exec devcontainer-redis-queue-1 redis-cli -n 0 FLUSHDB",
			"wsl -d Ubuntu bash scripts/wsl/run-in-slot.sh --slot 1",
			"python -c \"import subprocess; subprocess.run(['wsl', 'docker', 'exec', 'devcontainer-frappe-1', 'bench', '--site', 'development.localhost', 'migrate'])\"",
		]
		for cmd in bypasses:
			decision, reason = self._eval(cmd, self.linked_wt)
			self.assertEqual(decision, "deny", f"Expected deny for bypass: {cmd} (got {reason})")

	def test_denies_encoded_command_bypass_from_worktree(self) -> None:
		raw_ps = "powershell -File scripts\\sync-to-wsl.ps1"
		b64 = base64.b64encode(raw_ps.encode("utf-16-le")).decode("ascii")
		decision, reason = self._eval(f"powershell -EncodedCommand {b64}", self.linked_wt)
		self.assertEqual(decision, "deny", f"Expected deny for EncodedCommand bypass: {reason}")

		decision_bad, _ = self._eval("powershell -enc NOT_VALID_BASE64!!!", self.linked_wt)
		self.assertEqual(decision_bad, "deny")

	def test_cli_stdin_stdout_contract(self) -> None:
		payload = {
			"cwd": str(self.linked_wt),
			"toolCall": {
				"name": "run_command",
				"args": {"CommandLine": "wsl docker exec devcontainer-frappe-1 bench --site development.localhost migrate"},
			},
		}
		env = dict(os.environ, ISM_PRIMARY_REPO=str(self.primary))
		proc = subprocess.run(
			["python", str(HOOK_PATH)],
			input=json.dumps(payload),
			capture_output=True,
			text=True,
			env=env,
			check=True,
		)
		out = json.loads(proc.stdout)
		self.assertEqual(out["decision"], "deny")
		self.assertIn("test-in-slot.ps1", out["reason"])

	def test_script_level_primary_trunk_guards_reject_non_primary(self) -> None:
		# Copy sync/watch scripts into linked worktree and branch clone so $PSScriptRoot resolves there.
		for target_repo in (self.linked_wt, self.branch_clone):
			(target_repo / "ism-frappe").mkdir(exist_ok=True)
			dest_scripts = target_repo / "scripts"
			dest_scripts.mkdir(parents=True, exist_ok=True)
			for fname in ("sync-to-wsl.ps1", "watch-sync.ps1", "watch-sync.mjs"):
				shutil.copy2(SCRIPTS_DIR / fname, dest_scripts / fname)

			env = dict(os.environ, ISM_PRIMARY_REPO=str(self.primary))
			env.pop("ISM_ALLOW_WORKTREE_MAIN", None)
			env.pop("ISM_WSL_DEST", None)

			for ps_script in ("sync-to-wsl.ps1", "watch-sync.ps1"):
				proc = subprocess.run(
					["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(dest_scripts / ps_script)],
					cwd=str(target_repo),
					env=env,
					capture_output=True,
					text=True,
					timeout=15,
					check=False,
				)
				self.assertNotEqual(proc.returncode, 0, f"Expected non-zero exit for {ps_script} in {target_repo}")
				self.assertIn("test-in-slot.ps1", proc.stderr + proc.stdout)

			node_proc = subprocess.run(
				["node", str(dest_scripts / "watch-sync.mjs")],
				cwd=str(target_repo),
				env=env,
				capture_output=True,
				text=True,
				timeout=15,
				check=False,
			)
			self.assertNotEqual(node_proc.returncode, 0, f"Expected non-zero exit for watch-sync.mjs in {target_repo}")
			self.assertIn("test-in-slot.ps1", node_proc.stderr + node_proc.stdout)


if __name__ == "__main__":
	unittest.main()

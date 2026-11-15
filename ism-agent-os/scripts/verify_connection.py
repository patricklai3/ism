#!/usr/bin/env python3
"""4-step Frappe REST and ISM Agent Facade connection verifier."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

try:
	from ism_agent_os.config import IsmConfig
	from ism_agent_os.diagnostics import run_diagnostics
except ImportError:
	_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
	if _REPO_ROOT not in sys.path:
		sys.path.insert(0, _REPO_ROOT)
	from tools.config import IsmConfig  # noqa: E402
	from tools.diagnostics import run_diagnostics  # noqa: E402


def verify(config: IsmConfig, full_e2e: bool = False) -> bool:
	"""Run end-to-end REST and facade verification against Frappe + ISM."""
	return run_diagnostics(config, full_e2e=full_e2e)


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Verify ISM Agent OS connection")
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--full-e2e", action="store_true", help="Create a _TEST_ISM_ draft quote to verify writes")
	args = parser.parse_args(argv)
	config = IsmConfig.from_env(env_path=args.env_file)
	return 0 if verify(config, full_e2e=args.full_e2e) else 1


if __name__ == "__main__":
	sys.exit(main())

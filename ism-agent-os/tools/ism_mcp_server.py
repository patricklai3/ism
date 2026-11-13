#!/usr/bin/env python3
"""Zero-dependency Stdio JSON-RPC 2.0 MCP server for ISM Agent OS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure repository root is importable when invoked directly as a script
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from tools.client import IsmAgentClient, IsmApiError  # noqa: E402
from tools.config import IsmConfig  # noqa: E402
from tools.tool_registry import dispatch_tool, get_mcp_tools  # noqa: E402

SERVER_NAME = "ism-agent-os"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSION = "2024-11-05"


class IsmMcpServer:
	"""Model Context Protocol server routing shop tools to Frappe + ISM."""

	def __init__(self, client: Optional[IsmAgentClient] = None) -> None:
		self.client = client or IsmAgentClient()
		self._handlers = {
			"initialize": self._handle_initialize,
			"ping": self._handle_ping,
			"tools/list": self._handle_tools_list,
			"tools/call": self._handle_tools_call,
			"resources/list": self._handle_empty_list("resources"),
			"prompts/list": self._handle_empty_list("prompts"),
		}

	def handle_message(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
		"""Process a single JSON-RPC 2.0 request or notification."""
		req_id = payload.get("id")
		method = payload.get("method", "")
		params = payload.get("params") or {}

		# JSON-RPC notifications have no id and require no response
		if req_id is None:
			return None

		handler = self._handlers.get(method)
		if not handler:
			return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

		try:
			result = handler(params)
			return {"jsonrpc": "2.0", "id": req_id, "result": result}
		except IsmApiError as exc:
			return _jsonrpc_error(req_id, -32602, str(exc))
		except Exception as exc:
			return _jsonrpc_error(req_id, -32603, f"Internal server error: {exc}")

	def serve_stdio(self) -> None:
		"""Run blocking JSON-RPC 2.0 loop over stdin/stdout."""
		_configure_utf8_stdio()
		for raw_line in sys.stdin:
			line = raw_line.strip()
			if not line:
				continue
			try:
				payload = json.loads(line)
			except json.JSONDecodeError as exc:
				_write_stdout(_jsonrpc_error(None, -32700, f"Parse error: {exc}"))
				continue

			response = self.handle_message(payload)
			if response is not None:
				_write_stdout(response)

	def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
		client_version = params.get("protocolVersion") or SUPPORTED_PROTOCOL_VERSION
		return {
			"protocolVersion": client_version,
			"capabilities": {"tools": {"listChanged": False}},
			"serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
			"instructions": (
				"ISM Agent OS is active. Follow rules/AGENTS.md invariants: delegate math to "
				"Frappe, enforce customer authorization locks, and keep new quotes in Draft."
			),
		}

	def _handle_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
		return {}

	def _handle_tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
		return {"tools": get_mcp_tools()}

	def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
		tool_name = params.get("name", "")
		arguments = params.get("arguments") or {}
		try:
			raw_result = dispatch_tool(self.client, tool_name, arguments)
			return _format_tool_success(raw_result)
		except IsmApiError as exc:
			return _format_tool_error(str(exc))
		except Exception as exc:
			return _format_tool_error(f"Unexpected error running {tool_name}: {exc}")

	def _handle_empty_list(self, key: str):
		return lambda _params: {key: []}


def run_diagnostics(config: IsmConfig) -> bool:
	"""Verify local MCP registry and live Frappe + ISM connectivity."""
	tools = get_mcp_tools()
	print("=" * 60)
	print(f"ISM AGENT OS DIAGNOSTICS (v{SERVER_VERSION})")
	print(f"Target Site: {config.base_url} (transport={config.transport})")
	print("=" * 60)
	print(f"[1/4] Loaded {len(tools)} MCP tools from registry.")

	cfg_errors = config.validate()
	if cfg_errors:
		print("[2/4] Configuration warnings:")
		for err in cfg_errors:
			print(f"  - {err}")
		return False
	print("[2/4] Credentials and URL validated.")

	client = IsmAgentClient(config)
	try:
		init_resp = client.initialize_mcp()
		srv = init_resp.get("result", {}).get("serverInfo", {})
		print(f"[3/4] Frappe MCP connected: {srv.get('name', 'frappe')} v{srv.get('version', 'unknown')}")
	except IsmApiError as exc:
		print(f"[3/4] Frappe MCP unreachable ({exc}); testing REST fallback...")

	try:
		floor = client.get_shop_floor_status()
		summary = floor.get("summary") or floor.get("data", {}).get("summary", {})
		print(
			f"[4/4] Shop floor telemetry OK — Bays: {summary.get('active_bays', '?')}, "
			f"Techs: {summary.get('active_technicians', '?')}, "
			f"Active ROs: {summary.get('active_repair_orders', '?')}"
		)
		return True
	except IsmApiError as exc:
		print(f"[4/4] Shop floor check failed: {exc}")
		return False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="ISM Agent OS Stdio MCP Server")
	parser.add_argument("--url", help="Frappe base URL (overrides FRAPPE_URL)")
	parser.add_argument("--key", help="Frappe API key (overrides FRAPPE_API_KEY)")
	parser.add_argument("--secret", help="Frappe API secret (overrides FRAPPE_API_SECRET)")
	parser.add_argument("--site", help="Frappe site name header (overrides FRAPPE_SITE)")
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--test", action="store_true", help="Run connection and tool self-test")
	return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
	args = parse_args(argv)
	overrides = {
		"FRAPPE_URL": args.url,
		"FRAPPE_API_KEY": args.key,
		"FRAPPE_API_SECRET": args.secret,
		"FRAPPE_SITE": args.site,
	}
	config = IsmConfig.from_env(env_path=args.env_file, overrides=overrides)
	if args.test:
		return 0 if run_diagnostics(config) else 1

	server = IsmMcpServer(IsmAgentClient(config))
	server.serve_stdio()
	return 0


def _format_tool_success(raw_result: Any) -> dict[str, Any]:
	is_err = isinstance(raw_result, dict) and raw_result.get("success") is False
	if isinstance(raw_result, dict):
		card = raw_result.get("summary_card") or raw_result.get("summary_markdown")
		structured = raw_result
		if card:
			meta = {k: v for k, v in raw_result.items() if k not in ("summary_card", "summary_markdown", "data")}
			text = f"{card}\n\n```json\n{json.dumps(meta, indent=2, default=str)}\n```"
		else:
			text = json.dumps(raw_result, indent=2, default=str)
	else:
		structured = {"result": raw_result}
		text = json.dumps(raw_result, indent=2, default=str)

	return {
		"content": [{"type": "text", "text": text}],
		"structuredContent": structured,
		"isError": is_err,
	}


def _format_tool_error(message: str) -> dict[str, Any]:
	return {
		"content": [{"type": "text", "text": message}],
		"isError": True,
	}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
	return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _configure_utf8_stdio() -> None:
	if hasattr(sys.stdin, "reconfigure"):
		sys.stdin.reconfigure(encoding="utf-8")
	if hasattr(sys.stdout, "reconfigure"):
		sys.stdout.reconfigure(encoding="utf-8")


def _write_stdout(payload: dict[str, Any]) -> None:
	sys.stdout.write(json.dumps(payload, default=str) + "\n")
	sys.stdout.flush()


if __name__ == "__main__":
	sys.exit(main())

"""Low-level HTTP/MCP JSON-RPC 2.0 and REST transport for Frappe."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .config import IsmConfig


class IsmApiError(Exception):
	"""Raised when Frappe or MCP returns an API or transport error."""

	def __init__(self, message: str, status_code: int = 500, details: Any = None) -> None:
		super().__init__(message)
		self.status_code = status_code
		self.details = details


class FrappeTransport:
	"""Handles authenticated requests over Frappe MCP (/api/v2/mcp) and REST."""

	def __init__(self, config: IsmConfig) -> None:
		self.config = config
		self.mcp_available: Optional[bool] = None
		self._req_counter = 0

	def should_use_mcp(self) -> bool:
		if self.config.transport == "rest":
			return False
		if self.config.transport == "mcp":
			return True
		return self.mcp_available is not False

	def post_mcp(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		self._req_counter += 1
		payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._req_counter, "method": method}
		if params is not None:
			payload["params"] = params
		body = self.request_json(self.config.mcp_endpoint, "POST", payload)
		if isinstance(body, dict) and "error" in body:
			err = body["error"]
			msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
			raise IsmApiError(msg, status_code=400, details=err)
		self.mcp_available = True
		return body

	def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
		res = self.post_mcp("tools/call", {"name": tool_name, "arguments": arguments})
		result = res.get("result", {})
		if result.get("isError"):
			raise IsmApiError(_extract_mcp_error_text(result), status_code=400, details=result)
		if "structuredContent" in result and isinstance(result["structuredContent"], dict):
			sc = result["structuredContent"]
			return sc.get("result", sc)
		return _extract_mcp_text_or_json(result)

	def call_rest_method(self, method: str, args: dict[str, Any]) -> Any:
		res = self.request_json(f"{self.config.base_url}/api/method/{method}", "POST", args)
		if isinstance(res, dict) and "message" in res:
			return res["message"]
		return res

	def rest_list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]],
		fields: Optional[list[str]],
		limit: int,
		order_by: Optional[str],
	) -> Any:
		query: dict[str, str] = {"limit_page_length": str(limit)}
		if filters is not None:
			query["filters"] = json.dumps(filters)
		if fields is not None:
			query["fields"] = json.dumps(fields)
		if order_by:
			query["order_by"] = order_by
		encoded_dt = urllib.parse.quote(doctype, safe="")
		qs = urllib.parse.urlencode(query)
		res = self.request_json(f"{self.config.base_url}/api/resource/{encoded_dt}?{qs}", "GET")
		return res.get("data", res)

	def request_json(self, url: str, http_method: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		headers = {"Accept": "application/json", "Content-Type": "application/json"}
		if self.config.auth_header:
			headers["Authorization"] = self.config.auth_header
		if self.config.site:
			headers["X-Frappe-Site-Name"] = self.config.site

		data = json.dumps(body).encode("utf-8") if body is not None else None
		req = urllib.request.Request(url, data=data, headers=headers, method=http_method)
		try:
			with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
				raw = resp.read().decode("utf-8")
				return json.loads(raw) if raw else {}
		except urllib.error.HTTPError as exc:
			raw_err = exc.read().decode("utf-8", errors="replace")
			raise IsmApiError(_format_http_error(exc.code, raw_err), status_code=exc.code, details=raw_err) from exc
		except urllib.error.URLError as exc:
			raise IsmApiError(f"Connection failed to {url}: {exc.reason}", status_code=503) from exc


def _extract_mcp_error_text(result: dict[str, Any]) -> str:
	texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("text")]
	return "\n".join(texts) or "MCP tool execution failed."


def _extract_mcp_text_or_json(result: dict[str, Any]) -> Any:
	texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("text")]
	joined = "\n".join(texts)
	if not joined:
		return result
	try:
		return json.loads(joined)
	except Exception:
		return joined


def _format_http_error(status_code: int, raw_body: str) -> str:
	try:
		parsed = json.loads(raw_body)
		if isinstance(parsed, dict):
			if parsed.get("_server_messages"):
				msgs = json.loads(parsed["_server_messages"])
				extracted = [json.loads(m).get("message", m) if isinstance(m, str) else str(m) for m in msgs]
				return f"HTTP {status_code}: " + "; ".join(extracted)
			if parsed.get("exception"):
				return f"HTTP {status_code}: {parsed['exception']}"
			if parsed.get("message"):
				return f"HTTP {status_code}: {parsed['message']}"
	except Exception:
		pass
	return f"HTTP {status_code}: {raw_body[:300]}"

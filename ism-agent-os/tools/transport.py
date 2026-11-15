"""Low-level HTTP REST transport for Frappe."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .config import IsmConfig

_DEFAULT_URLOPEN = urllib.request.urlopen
_MAX_POOL_PER_HOST = 8


class IsmApiError(Exception):
	"""Raised when Frappe returns an API or REST transport error."""

	def __init__(self, message: str, status_code: int = 500, details: Any = None) -> None:
		super().__init__(message)
		self.status_code = status_code
		self.details = details


class FrappeTransport:
	"""Handles authenticated requests over Frappe REST (/api/method/* and /api/resource/*)."""

	def __init__(self, config: IsmConfig) -> None:
		self.config = config
		self._pool_lock = threading.Lock()
		self._pool: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}

	def close(self) -> None:
		"""Close all pooled HTTP(S) keep-alive connections."""
		with self._pool_lock:
			conns = [c for bucket in self._pool.values() for c in bucket]
			self._pool.clear()
		for conn in conns:
			try:
				conn.close()
			except Exception:
				pass

	def _acquire_connection(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
		key = (scheme, host, port)
		with self._pool_lock:
			bucket = self._pool.get(key)
			if bucket:
				conn = bucket.pop()
				conn.timeout = self.config.timeout
				if conn.sock is not None:
					conn.sock.settimeout(self.config.timeout)
				return conn
		if scheme == "https":
			return http.client.HTTPSConnection(host, port, timeout=self.config.timeout)
		return http.client.HTTPConnection(host, port, timeout=self.config.timeout)

	def _release_connection(
		self,
		scheme: str,
		host: str,
		port: int,
		conn: http.client.HTTPConnection,
		reusable: bool,
	) -> None:
		if not reusable:
			try:
				conn.close()
			except Exception:
				pass
			return
		key = (scheme, host, port)
		with self._pool_lock:
			bucket = self._pool.setdefault(key, [])
			if len(bucket) < _MAX_POOL_PER_HOST:
				bucket.append(conn)
				return
		try:
			conn.close()
		except Exception:
			pass

	def call_rest_method(self, method: str, args: dict[str, Any]) -> Any:
		"""Invoke a whitelisted Frappe method over POST /api/method/{method}."""
		res = self.request_json(f"{self.config.base_url}/api/method/{method}", "POST", args)
		if isinstance(res, dict) and "message" in res:
			return res["message"]
		return res

	def rest_list_documents(
		self,
		doctype: str,
		filters: Optional[dict[str, Any] | list[Any]] = None,
		fields: Optional[list[str]] = None,
		limit: int = 20,
		order_by: Optional[str] = None,
	) -> Any:
		"""Query a Frappe DocType list over GET /api/resource/{doctype}."""
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

	def rest_get_document(self, doctype: str, name: str) -> Any:
		"""Fetch a single Frappe document over GET /api/resource/{doctype}/{name}."""
		url = f"{self.config.base_url}/api/resource/{_encode_part(doctype)}/{_encode_part(name)}"
		res = self.request_json(url, "GET")
		return res.get("data", res)

	def rest_create_document(self, doctype: str, data: dict[str, Any]) -> Any:
		"""Create a Frappe document over POST /api/resource/{doctype}."""
		url = f"{self.config.base_url}/api/resource/{_encode_part(doctype)}"
		res = self.request_json(url, "POST", data)
		return res.get("data", res)

	def rest_update_document(self, doctype: str, name: str, data: dict[str, Any]) -> Any:
		"""Update an existing Frappe document over PUT /api/resource/{doctype}/{name}."""
		url = f"{self.config.base_url}/api/resource/{_encode_part(doctype)}/{_encode_part(name)}"
		res = self.request_json(url, "PUT", data)
		return res.get("data", res)

	def request_json(self, url: str, http_method: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		"""Send an authenticated JSON request with keep-alive retry on stale connections."""
		headers = {
			"Accept": "application/json",
			"Content-Type": "application/json",
			"Connection": "keep-alive",
		}
		if self.config.auth_header:
			headers["Authorization"] = self.config.auth_header
		if self.config.site:
			headers["X-Frappe-Site-Name"] = self.config.site

		data = json.dumps(body).encode("utf-8") if body is not None else None
		parsed = urllib.parse.urlsplit(url)
		scheme = parsed.scheme.lower()
		use_pool = urllib.request.urlopen is _DEFAULT_URLOPEN and scheme in ("http", "https") and bool(parsed.hostname)

		if not use_pool:
			req = urllib.request.Request(url, data=data, headers=headers, method=http_method)
			for attempt in range(2):
				try:
					with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
						raw = resp.read().decode("utf-8")
						return json.loads(raw) if raw else {}
				except http.client.RemoteDisconnected as exc:
					if attempt == 0:
						continue
					raise IsmApiError(f"Connection closed by {url}: {exc}", status_code=503) from exc
				except urllib.error.HTTPError as exc:
					raw_err = exc.read().decode("utf-8", errors="replace")
					raise IsmApiError(_format_http_error(exc.code, raw_err), status_code=exc.code, details=raw_err) from exc
				except urllib.error.URLError as exc:
					raise IsmApiError(f"Connection failed to {url}: {exc.reason}", status_code=503) from exc
			return {}

		host = parsed.hostname or ""
		port = parsed.port or (443 if scheme == "https" else 80)
		path = parsed.path or "/"
		if parsed.query:
			path = f"{path}?{parsed.query}"

		for attempt in range(2):
			conn = self._acquire_connection(scheme, host, port)
			try:
				conn.request(http_method, path, body=data, headers=headers)
				resp = conn.getresponse()
				raw_bytes = resp.read()
				conn_hdr = (resp.getheader("Connection") or "").lower()
				reusable = (not resp.will_close) and (conn_hdr != "close")
				self._release_connection(scheme, host, port, conn, reusable=reusable)

				if resp.status >= 400:
					raw_err = raw_bytes.decode("utf-8", errors="replace")
					raise IsmApiError(_format_http_error(resp.status, raw_err), status_code=resp.status, details=raw_err)

				raw = raw_bytes.decode("utf-8")
				return json.loads(raw) if raw else {}
			except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError, BrokenPipeError) as exc:
				self._release_connection(scheme, host, port, conn, reusable=False)
				if attempt == 0:
					continue
				raise IsmApiError(f"Connection closed by {url}: {exc}", status_code=503) from exc
			except IsmApiError:
				raise
			except (OSError, http.client.HTTPException) as exc:
				self._release_connection(scheme, host, port, conn, reusable=False)
				raise IsmApiError(f"Connection failed to {url}: {exc}", status_code=503) from exc
		return {}


def _encode_part(value: str) -> str:
	return urllib.parse.quote(value, safe="")


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

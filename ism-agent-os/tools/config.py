"""Environment and connection configuration for ISM Agent OS."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse, urlsplit

TESLA_ALLOWED_HOSTS = ("service.tesla.com", "service.teslamotors.com")


def validate_tesla_url(url: str) -> None:
	"""Validate that a Tesla service manual URL is safe to fetch."""
	if not isinstance(url, str) or not url.strip():
		raise ValueError(
			"URL must be an https:// link on service.tesla.com or service.teslamotors.com."
		)
	cleaned = url.strip()
	if any(ch in cleaned for ch in ("\r", "\n")) or any(ch.isspace() for ch in cleaned):
		raise ValueError(
			"URL must be an https:// link on service.tesla.com or service.teslamotors.com."
		)
	try:
		parsed = urlparse(cleaned)
	except ValueError as exc:
		raise ValueError(
			"URL must be an https:// link on service.tesla.com or service.teslamotors.com."
		) from exc
	host = (parsed.hostname or "").lower()
	if (
		parsed.scheme.lower() != "https"
		or parsed.username is not None
		or parsed.password is not None
		or "@" in parsed.netloc
		or host not in TESLA_ALLOWED_HOSTS
	):
		raise ValueError(
			"URL must be an https:// link on service.tesla.com or service.teslamotors.com."
		)


@dataclass(frozen=True)
class IsmConfig:
	"""Connection settings for a Frappe site with ISM installed."""

	url: str = "http://localhost:8000"
	api_key: str = ""
	api_secret: str = field(default="", repr=False)
	site: str = ""
	company: str = ""
	timeout: int = 30

	@property
	def base_url(self) -> str:
		return self.url.rstrip("/")

	@property
	def auth_header(self) -> str:
		if not self.api_key or not self.api_secret:
			return ""
		return f"token {self.api_key}:{self.api_secret}"

	def validate(self) -> list[str]:
		"""Return a list of configuration errors, or empty list when valid."""
		errors: list[str] = []
		if not self.url or not self.url.strip():
			errors.append("FRAPPE_URL is required.")
		elif "\n" not in self.url and "\r" not in self.url:
			try:
				parsed = urlsplit(self.url.strip())
			except ValueError:
				errors.append("FRAPPE_URL is not a valid URL.")
			else:
				if parsed.scheme.lower() not in ("http", "https"):
					errors.append("FRAPPE_URL must use http:// or https://.")
				if not parsed.hostname:
					errors.append("FRAPPE_URL must include a valid hostname.")
				if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
					errors.append("FRAPPE_URL must not contain embedded userinfo credentials.")

		if not self.api_key:
			errors.append("FRAPPE_API_KEY is missing.")
		if not self.api_secret:
			errors.append("FRAPPE_API_SECRET is missing.")

		for field_name, field_value in (
			("FRAPPE_URL", self.url),
			("FRAPPE_API_KEY", self.api_key),
			("FRAPPE_API_SECRET", self.api_secret),
			("FRAPPE_SITE", self.site),
		):
			if field_value and ("\n" in field_value or "\r" in field_value):
				errors.append(f"{field_name} contains invalid newline characters.")
		return errors

	@classmethod
	def from_env(
		cls,
		env_path: Optional[str | Path] = None,
		overrides: Optional[Mapping[str, str]] = None,
	) -> IsmConfig:
		"""Load config from .env file, process environment, and explicit overrides."""
		merged: dict[str, str] = {}
		for candidate in _candidate_env_files(env_path):
			if candidate.is_file():
				merged.update(_parse_dotenv(candidate))

		for key in _ENV_KEYS:
			if key in os.environ and os.environ[key] != "":
				merged[key] = os.environ[key]

		if overrides:
			for key, value in overrides.items():
				if value is not None and value != "":
					merged[key] = str(value)

		return cls(
			url=merged.get("FRAPPE_URL", "http://localhost:8000").strip(),
			api_key=merged.get("FRAPPE_API_KEY", "").strip(),
			api_secret=merged.get("FRAPPE_API_SECRET", "").strip(),
			site=merged.get("FRAPPE_SITE", "").strip(),
			company=merged.get("ISM_DEFAULT_COMPANY", "").strip(),
			timeout=_parse_int(merged.get("ISM_TIMEOUT", "30"), default=30),
		)


_ENV_KEYS = (
	"FRAPPE_URL",
	"FRAPPE_API_KEY",
	"FRAPPE_API_SECRET",
	"FRAPPE_SITE",
	"ISM_DEFAULT_COMPANY",
	"ISM_TIMEOUT",
)


def _candidate_env_files(explicit_path: Optional[str | Path]) -> list[Path]:
	if explicit_path:
		return [Path(explicit_path)]
	repo_root = Path(__file__).resolve().parent.parent
	candidates = [repo_root / ".env", Path.cwd() / ".env"]
	# Preserve order while deduplicating identical paths
	unique: list[Path] = []
	for path in candidates:
		resolved = path.resolve()
		if resolved not in unique:
			unique.append(resolved)
	return unique


def _parse_dotenv(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, raw_val = line.split("=", 1)
		key = key.strip()
		if key.startswith("export "):
			key = key[7:].strip()
		val = raw_val.strip()
		if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
			val = val[1:-1]
		values[key] = val
	return values


def _parse_int(raw: str, default: int) -> int:
	try:
		return max(1, int(raw))
	except (TypeError, ValueError):
		return default

"""Deterministic Tesla-offset parts pricing engine for ISM Agent OS."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

DEFAULT_PRICING_CONFIG_PATH = Path(__file__).resolve().parent.parent / "rules" / "parts_pricing.json"


@dataclass(frozen=True)
class PricingConfig:
	"""Editable parts pricing parameters loaded from rules/parts_pricing.json."""

	currency: str = "CAD"
	tesla_search_url_template: str = "https://parts.tesla.com/en-CA/find-part?searchTerm={query}"
	baseline_discount_pct: float = 30.0
	condition_discount_overrides: dict[str, Optional[float]] = field(
		default_factory=lambda: {"New": None, "Used": None, "Reconditioned": None}
	)
	small_ticket_max_tesla_price: float = 20.0
	small_ticket_discount_pct: float = 0.0
	min_sell_price: float = 5.0
	min_cost_markup_pct: float = 25.0
	min_cost_margin_dollars: float = 5.0
	fallback_cost_markup_pct: float = 40.0
	rounding_increment: float = 0.01

	def validate(self) -> None:
		"""Validate numeric bounds so bad config values fail loudly."""
		for name, val in (
			("baseline_discount_pct", self.baseline_discount_pct),
			("small_ticket_discount_pct", self.small_ticket_discount_pct),
		):
			if not (0.0 <= val < 100.0):
				raise ValueError(f"{name} must be in [0, 100), got {val}")

		for cond, override in self.condition_discount_overrides.items():
			if override is not None and not (0.0 <= override < 100.0):
				raise ValueError(f"condition_discount_overrides[{cond!r}] must be in [0, 100), got {override}")

		for name, val in (
			("small_ticket_max_tesla_price", self.small_ticket_max_tesla_price),
			("min_sell_price", self.min_sell_price),
			("min_cost_markup_pct", self.min_cost_markup_pct),
			("min_cost_margin_dollars", self.min_cost_margin_dollars),
			("fallback_cost_markup_pct", self.fallback_cost_markup_pct),
		):
			if val < 0.0:
				raise ValueError(f"{name} must be >= 0, got {val}")

		if self.rounding_increment <= 0.0:
			raise ValueError(f"rounding_increment must be > 0, got {self.rounding_increment}")
		if "{query}" not in self.tesla_search_url_template:
			raise ValueError("tesla_search_url_template must include '{query}' placeholder.")


def load_pricing_config(path: Optional[str | Path] = None) -> PricingConfig:
	"""Load and validate PricingConfig from JSON (defaults to rules/parts_pricing.json)."""
	cfg_path = Path(path) if path else DEFAULT_PRICING_CONFIG_PATH
	raw = json.loads(cfg_path.read_text(encoding="utf-8"))
	if not isinstance(raw, dict):
		raise ValueError(f"Pricing config must be a JSON object: {cfg_path}")

	overrides_raw = raw.get("condition_discount_overrides") or {}
	overrides: dict[str, Optional[float]] = {
		str(k): (float(v) if v is not None else None)
		for k, v in overrides_raw.items()
	}

	cfg = PricingConfig(
		currency=str(raw.get("currency", "CAD")).strip() or "CAD",
		tesla_search_url_template=str(
			raw.get("tesla_search_url_template", "https://parts.tesla.com/en-CA/find-part?searchTerm={query}")
		).strip(),
		baseline_discount_pct=float(raw.get("baseline_discount_pct", 30.0)),
		condition_discount_overrides=overrides,
		small_ticket_max_tesla_price=float(raw.get("small_ticket_max_tesla_price", 20.0)),
		small_ticket_discount_pct=float(raw.get("small_ticket_discount_pct", 0.0)),
		min_sell_price=float(raw.get("min_sell_price", 5.0)),
		min_cost_markup_pct=float(raw.get("min_cost_markup_pct", 25.0)),
		min_cost_margin_dollars=float(raw.get("min_cost_margin_dollars", 5.0)),
		fallback_cost_markup_pct=float(raw.get("fallback_cost_markup_pct", 40.0)),
		rounding_increment=float(raw.get("rounding_increment", 0.01)),
	)
	cfg.validate()
	return cfg


def build_tesla_part_search_url(query: str, config: Optional[PricingConfig] = None) -> str:
	"""Build a Tesla Canada parts catalog search URL for a part number or part name."""
	cleaned = (query or "").strip()
	if not cleaned:
		raise ValueError("Search query cannot be empty.")
	cfg = config or load_pricing_config()
	return cfg.tesla_search_url_template.format(query=quote_plus(cleaned))


def _coerce_positive_or_none(value: Optional[float], label: str) -> Optional[float]:
	if value is None:
		return None
	num = float(value)
	if num < 0.0:
		raise ValueError(f"{label} cannot be negative (got {num}).")
	return num if num > 0.0 else None


def _round_to_increment(value: float, increment: float) -> float:
	inc_dec = Decimal(str(increment))
	val_dec = Decimal(str(value))
	rounded = (val_dec / inc_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * inc_dec
	return float(rounded)


def _compute_margin(sell_price: float, cost: Optional[float]) -> tuple[Optional[float], Optional[float]]:
	if cost is None or cost <= 0.0:
		return None, None
	margin_dollars = round(sell_price - cost, 2)
	margin_pct = round(((sell_price - cost) / cost) * 100.0, 2)
	return margin_dollars, margin_pct


def calculate_part_price(
	*,
	tesla_oem_price: Optional[float] = None,
	cost: Optional[float] = None,
	part_source: str = "Aftermarket",
	part_condition: str = "New",
	override_rate: Optional[float] = None,
	part_number: Optional[str] = None,
	part_name: Optional[str] = None,
	config: Optional[PricingConfig] = None,
) -> dict[str, Any]:
	"""Calculate customer selling price in CAD using Tesla offset and floor rules."""
	cfg = config or load_pricing_config()
	tesla_price = _coerce_positive_or_none(tesla_oem_price, "tesla_oem_price")
	purchase_cost = _coerce_positive_or_none(cost, "cost")
	explicit_override = _coerce_positive_or_none(override_rate, "override_rate")

	norm_source = "OEM" if str(part_source or "").strip().upper().startswith("OEM") else "Aftermarket"
	norm_condition = str(part_condition or "New").strip() or "New"

	search_query = (part_number or "").strip() or (part_name or "").strip()
	tesla_search_url = build_tesla_part_search_url(search_query, cfg) if search_query else None
	warnings: list[str] = []

	if explicit_override is not None:
		sell_price = _round_to_increment(explicit_override, cfg.rounding_increment)
		rule_applied = "manual_override"
	elif tesla_price is None and purchase_cost is None:
		raise ValueError("Either tesla_oem_price or cost must be greater than zero.")
	elif tesla_price is None:
		assert purchase_cost is not None
		raw_sell = max(
			purchase_cost + max(purchase_cost * (cfg.fallback_cost_markup_pct / 100.0), cfg.min_cost_margin_dollars),
			cfg.min_sell_price,
		)
		sell_price = _round_to_increment(raw_sell, cfg.rounding_increment)
		rule_applied = "cost_plus_fallback"
		warnings.append("Tesla OEM price unavailable — cost-plus fallback applied.")
	else:
		cond_override = cfg.condition_discount_overrides.get(norm_condition)
		if tesla_price <= cfg.small_ticket_max_tesla_price:
			tesla_based = tesla_price * (1.0 - cfg.small_ticket_discount_pct / 100.0)
			base_rule = "small_ticket_match"
		elif cond_override is not None:
			tesla_based = tesla_price * (1.0 - cond_override / 100.0)
			base_rule = "condition_override"
		else:
			tesla_based = tesla_price * (1.0 - cfg.baseline_discount_pct / 100.0)
			base_rule = "baseline_offset"

		if tesla_based < cfg.min_sell_price:
			tesla_based = cfg.min_sell_price
			base_rule = "min_sell_floor"

		if purchase_cost is not None:
			cost_floor = purchase_cost + max(
				purchase_cost * (cfg.min_cost_markup_pct / 100.0),
				cfg.min_cost_margin_dollars,
			)
			if cost_floor > tesla_based:
				raw_sell = cost_floor
				rule_applied = "cost_floor"
			else:
				raw_sell = tesla_based
				rule_applied = base_rule
		else:
			raw_sell = tesla_based
			rule_applied = base_rule
			warnings.append("Cost unknown — verify supplier cost before ordering.")

		sell_price = _round_to_increment(raw_sell, cfg.rounding_increment)

	above_tesla = bool(tesla_price is not None and sell_price > tesla_price)
	if above_tesla and tesla_price is not None:
		warnings.append(
			f"Calculated price (${sell_price:.2f}) exceeds Tesla OEM price (${tesla_price:.2f}) to protect margin."
		)

	margin_dollars, margin_pct = _compute_margin(sell_price, purchase_cost)
	return {
		"currency": cfg.currency,
		"sell_price": sell_price,
		"rule_applied": rule_applied,
		"tesla_oem_price": tesla_price,
		"cost": purchase_cost,
		"part_source": norm_source,
		"part_condition": norm_condition,
		"margin_dollars": margin_dollars,
		"margin_pct": margin_pct,
		"above_tesla": above_tesla,
		"warnings": warnings,
		"tesla_search_url": tesla_search_url,
	}

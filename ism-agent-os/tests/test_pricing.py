"""Unit tests for the ISM Agent OS Tesla-offset parts pricing mechanism."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ingestion.supplier_catalog import SupplierCatalogSanitizer
from tools import cli
from tools.pricing import (
	PricingConfig,
	build_tesla_part_search_url,
	calculate_part_price,
	load_pricing_config,
)

HOOK_PATH = Path(__file__).resolve().parents[2] / ".agents" / "scripts" / "enforce_ism_write_gate.py"


class TestPartsPricing(unittest.TestCase):
	def test_default_config_loads_and_rounds_to_cent(self) -> None:
		cfg = load_pricing_config()
		self.assertEqual(cfg.currency, "CAD")
		self.assertEqual(cfg.baseline_discount_pct, 30.0)
		self.assertEqual(cfg.rounding_increment, 0.01)

	def test_baseline_thirty_percent_discount_ignores_low_cost(self) -> None:
		res = calculate_part_price(tesla_oem_price=200.0, cost=40.0, part_source="Aftermarket")
		self.assertEqual(res["sell_price"], 140.0)
		self.assertEqual(res["rule_applied"], "baseline_offset")
		self.assertFalse(res["above_tesla"])
		self.assertEqual(res["margin_dollars"], 100.0)
		self.assertEqual(res["warnings"], [])

	def test_high_cost_triggers_cost_floor_and_above_tesla_flag(self) -> None:
		res = calculate_part_price(tesla_oem_price=100.0, cost=90.0, part_source="OEM")
		self.assertEqual(res["sell_price"], 112.5)
		self.assertEqual(res["rule_applied"], "cost_floor")
		self.assertTrue(res["above_tesla"])
		self.assertTrue(any("exceeds Tesla OEM price" in w for w in res["warnings"]))

	def test_small_ticket_match_and_cost_floor(self) -> None:
		matched = calculate_part_price(tesla_oem_price=10.0, cost=4.0)
		self.assertEqual(matched["sell_price"], 10.0)
		self.assertEqual(matched["rule_applied"], "small_ticket_match")
		self.assertFalse(matched["above_tesla"])

		protected = calculate_part_price(tesla_oem_price=10.0, cost=9.0)
		self.assertEqual(protected["sell_price"], 14.0)
		self.assertEqual(protected["rule_applied"], "cost_floor")
		self.assertTrue(protected["above_tesla"])

	def test_min_sell_floor_on_tiny_part_without_cost(self) -> None:
		res = calculate_part_price(tesla_oem_price=3.0, cost=None)
		self.assertEqual(res["sell_price"], 5.0)
		self.assertEqual(res["rule_applied"], "min_sell_floor")
		self.assertTrue(res["above_tesla"])
		self.assertTrue(any("Cost unknown" in w for w in res["warnings"]))

	def test_condition_override_in_custom_config(self) -> None:
		cfg = PricingConfig(condition_discount_overrides={"New": None, "Used": 50.0, "Reconditioned": 40.0})
		res = calculate_part_price(tesla_oem_price=200.0, cost=30.0, part_condition="Used", config=cfg)
		self.assertEqual(res["sell_price"], 100.0)
		self.assertEqual(res["rule_applied"], "condition_override")

	def test_fallbacks_and_missing_both_error(self) -> None:
		no_tesla = calculate_part_price(tesla_oem_price=None, cost=25.0)
		self.assertEqual(no_tesla["sell_price"], 35.0)
		self.assertEqual(no_tesla["rule_applied"], "cost_plus_fallback")
		self.assertTrue(any("cost-plus fallback" in w for w in no_tesla["warnings"]))

		with self.assertRaises(ValueError):
			calculate_part_price(tesla_oem_price=None, cost=None)

	def test_manual_override_and_search_url_builder(self) -> None:
		url = build_tesla_part_search_url("1044341-00-D")
		self.assertEqual(url, "https://parts.tesla.com/en-CA/find-part?searchTerm=1044341-00-D")

		res = calculate_part_price(
			tesla_oem_price=200.0,
			cost=50.0,
			override_rate=155.55,
			part_number="1044341-00-D",
		)
		self.assertEqual(res["sell_price"], 155.55)
		self.assertEqual(res["rule_applied"], "manual_override")
		self.assertEqual(res["tesla_search_url"], url)

	def test_supplier_catalog_sanitizer_applies_and_preserves_rates(self) -> None:
		raw = (
			"SKU,Title,Source,Condition,Price,Cost,Qty,OEM#,Tesla Price\n"
			"AFT-1,Control Arm,Aftermarket,New,199.00,60.00,2,1044341-00-D,200.00\n"
			"CLIP-1,Fender Clip,OEM,New,5.00,9.00,10,1001234-00-A,10.00\n"
		)
		sanitizer = SupplierCatalogSanitizer()
		items = sanitizer.parse_rows(raw)
		self.assertEqual(items[0]["unit_rate"], 140.0)
		self.assertEqual(items[0]["_pricing"]["rule_applied"], "baseline_offset")
		self.assertEqual(items[1]["unit_rate"], 14.0)
		self.assertEqual(items[1]["_pricing"]["rule_applied"], "cost_floor")

		preserved = sanitizer.parse_rows(raw, preserve_explicit_rate=True)
		self.assertEqual(preserved[0]["unit_rate"], 199.0)
		self.assertEqual(preserved[0]["_pricing"]["rule_applied"], "manual_override")

	def test_cli_price_part_and_write_gate_allow(self) -> None:
		buf = io.StringIO()
		with redirect_stdout(buf):
			code = cli.main(
				["--json", "price-part", "--tesla-price", "200", "--cost", "40", "--part-number", "1044341-00-D"]
			)
		self.assertEqual(code, 0)
		payload = json.loads(buf.getvalue())
		self.assertTrue(payload["success"])
		self.assertEqual(payload["data"]["sell_price"], 140.0)
		self.assertEqual(
			payload["data"]["tesla_search_url"],
			"https://parts.tesla.com/en-CA/find-part?searchTerm=1044341-00-D",
		)

		if HOOK_PATH.is_file():
			spec = importlib.util.spec_from_file_location("enforce_ism_write_gate", HOOK_PATH)
			mod = importlib.util.module_from_spec(spec)
			assert spec and spec.loader
			spec.loader.exec_module(mod)
			decision, reason = mod.evaluate_hook_payload({
				"toolCall": {
					"name": "run_command",
					"args": {"CommandLine": "python -m tools.cli --json price-part --tesla-price 100 --cost 50"},
				}
			})
			self.assertEqual(decision, "allow", f"Expected price-part allow, got: {reason}")


if __name__ == "__main__":
	unittest.main()

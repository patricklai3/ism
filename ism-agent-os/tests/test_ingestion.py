"""Tests for supplier catalog normalization and Tesla manual parsing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ingestion.supplier_catalog import SupplierCatalogSanitizer
from ingestion.tesla_manual_parser import TeslaManualParser


class TestSupplierCatalogSanitizer(unittest.TestCase):
	def test_normalizes_csv_headers_and_strips_currency(self) -> None:
		raw = (
			"SKU,Title,Unit Price,Wholesale,Qty,OEM#\n"
			'1044341-00-D,"Front Lower Lateral Link, LH","$185.00","$125.00",10,1044341-00-D\n'
			"1044354-00-A,Upper Control Arm,$210.50,$140.00,4,\n"
		)
		normalized = SupplierCatalogSanitizer().normalize_text(raw, default_source="OEM", default_condition="New")
		lines = normalized.splitlines()
		self.assertEqual(len(lines), 3)
		self.assertTrue(lines[0].startswith("Part# | Description"))
		self.assertIn("1044341-00-D | Front Lower Lateral Link, LH | OEM | New | 185.00 | 125.00 | 10 |  | 1044341-00-D", lines[1])

	def test_ingest_batches_rows(self) -> None:
		mock_client = MagicMock()
		mock_client.import_supplier_catalog.return_value = {"success": True}
		raw = "Part#,Description,Rate\nP1,Part One,10\nP2,Part Two,20\nP3,Part Three,30\n"

		results = SupplierCatalogSanitizer().ingest(mock_client, raw, supplier="Tesla", chunk_size=2)
		self.assertEqual(len(results), 2)
		self.assertEqual(mock_client.import_supplier_catalog.call_count, 2)


class TestTeslaManualParser(unittest.TestCase):
	def test_parse_url_metadata(self) -> None:
		parser = TeslaManualParser()
		meta = parser.parse_url_metadata(
			"https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/GUID-167932E7-D6D1-4890-B113-5751559B89B4.html"
		)
		self.assertEqual(meta["model"], "Model 3")
		self.assertEqual(meta["generation"], "2024+")
		self.assertEqual(meta["doc_guid"], "GUID-167932E7-D6D1-4890-B113-5751559B89B4")

	def test_parse_html_and_markdown(self) -> None:
		parser = TeslaManualParser()
		html = (
			'<h1 class="topictitle1">Link - Lower - Front - LH (Remove and Replace)</h1>'
			'<span class="ph correction-code">31011402</span>'
			'<span class="ph frt">0.85</span>'
		)
		parsed_html = parser.parse_html(html, "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-123.html")
		self.assertEqual(parsed_html["correction_code"], "31011402")
		self.assertEqual(parsed_html["labor_hours"], 0.85)
		self.assertEqual(parsed_html["model"], "Model Y")

		md = "# Lateral Link Replacement\nCorrection Code: 31011402\nFRT: 1.20\nParts: 1044341-00-D, 1044354-00-A"
		parsed_md = parser.parse_markdown(md)
		self.assertEqual(parsed_md["procedure_title"], "Lateral Link Replacement")
		self.assertEqual(len(parsed_md["parts"]), 2)

	def test_rejects_non_tesla_domain(self) -> None:
		parser = TeslaManualParser(client=MagicMock())
		with self.assertRaises(ValueError):
			parser.ingest_from_url("https://example.com/manual.html")


if __name__ == "__main__":
	unittest.main()

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
		raw = "Part#,Description,Rate\nP1,Part One,$10.00\nP2,Part Two,20\nP3,Part Three,30\n"

		results = SupplierCatalogSanitizer().ingest(
			mock_client, raw, supplier="Tesla", chunk_size=2, service_package="SP-1"
		)
		self.assertEqual(len(results), 2)
		self.assertEqual(mock_client.import_supplier_catalog.call_count, 2)

		first_kwargs = mock_client.import_supplier_catalog.call_args_list[0].kwargs
		second_kwargs = mock_client.import_supplier_catalog.call_args_list[1].kwargs
		self.assertNotIn("raw_text", first_kwargs)
		self.assertEqual(first_kwargs["supplier"], "Tesla")
		self.assertEqual(first_kwargs["service_package"], "SP-1")
		self.assertEqual(len(first_kwargs["items"]), 2)
		self.assertEqual(first_kwargs["items"][0]["part_number"], "P1")
		self.assertEqual(first_kwargs["items"][0]["unit_rate"], 10.0)
		self.assertEqual(len(second_kwargs["items"]), 1)
		self.assertEqual(second_kwargs["items"][0]["part_number"], "P3")

	def test_normalizes_reman_condition_to_reconditioned(self) -> None:
		raw = "Part#,Description,Condition,Rate\nP9,Steering Rack,Reman,450.00\n"
		normalized = SupplierCatalogSanitizer().normalize_text(raw, default_source="OEM", default_condition="Reman")
		self.assertIn("| OEM | Reconditioned |", normalized)


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

	def test_ingest_from_url_forwards_conflict_and_side_options(self) -> None:
		mock_client = MagicMock()
		mock_client.fetch_tesla_procedure.return_value = {
			"success": True,
			"urls": {"repair_order": "https://shop.local/app/repair-order/RO-1"},
			"data": {
				"title": "Link - Lower - Front - LH",
				"correction_code": "31011402",
				"frt": 0.85,
				"applicable_model": "Model Y",
				"parts": [{"part_number": "1044341-00-D", "qty": 1}],
			},
		}
		mock_client.import_tesla_procedure.return_value = {"success": True, "data": {"action": "updated"}}
		parser = TeslaManualParser(client=mock_client)
		res = parser.ingest_from_url(
			"https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-123.html",
			repair_order="RO-1",
			on_conflict="overwrite",
			force=True,
			side="LH",
		)
		self.assertTrue(res["success"])
		mock_client.fetch_tesla_procedure.assert_called_once_with(
			"https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-123.html", repair_order="RO-1"
		)
		imp_kwargs = mock_client.import_tesla_procedure.call_args.kwargs
		self.assertEqual(imp_kwargs["repair_order"], "RO-1")
		self.assertEqual(imp_kwargs["procedure_title"], "Link - Lower - Front - LH")
		self.assertEqual(imp_kwargs["correction_code"], "31011402")
		self.assertEqual(imp_kwargs["labor_hours"], 0.85)
		self.assertEqual(imp_kwargs["model"], "Model Y")
		self.assertEqual(imp_kwargs["parts"], [{"part_number": "1044341-00-D", "qty": 1}])
		self.assertEqual(imp_kwargs["on_conflict"], "overwrite")
		self.assertTrue(imp_kwargs["force"])
		self.assertEqual(imp_kwargs["side"], "LH")
		self.assertIsNotNone(imp_kwargs["data"])

	def test_preserves_zero_frt_and_rejects_userinfo_in_tesla_url(self) -> None:
		mock_client = MagicMock()
		mock_client.fetch_tesla_procedure.return_value = {
			"success": True,
			"data": {
				"title": "Inspection Note Only",
				"correction_code": "10000100",
				"frt": 0.0,
				"applicable_model": "Model 3",
				"parts": [],
			},
		}
		mock_client.import_tesla_procedure.return_value = {"success": True}
		parser = TeslaManualParser(client=mock_client)
		parser.ingest_from_url("https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-ZERO.html")
		self.assertEqual(mock_client.import_tesla_procedure.call_args.kwargs["labor_hours"], 0.0)

		norm_zero = parser.normalize_procedure({"title": "Zero Time", "frt": 0.0})
		self.assertEqual(norm_zero["frt"], 0.0)
		self.assertEqual(norm_zero["labor_hours"], 0.0)

		for bad_url in (
			"https://service.tesla.com@evil.example/manual.html",
			"https://user:pass@service.tesla.com/manual.html",
			"http://service.tesla.com/manual.html",
			"https://service.tesla.com/manual.html\nEvil",
		):
			with self.assertRaises(ValueError):
				parser.ingest_from_url(bad_url)

	def test_supplier_catalog_condition_delimiter_and_float_regressions(self) -> None:
		from ingestion.supplier_catalog import _normalize_condition_label, _parse_float

		self.assertEqual(_normalize_condition_label("Unused"), "New")
		self.assertEqual(_normalize_condition_label("Direct OEM"), "New")
		self.assertEqual(_normalize_condition_label(123), "New")
		self.assertEqual(_normalize_condition_label(None), "New")
		self.assertEqual(_normalize_condition_label("Used - Good"), "Used")
		self.assertEqual(_normalize_condition_label("OEM Reman"), "Reconditioned")

		self.assertEqual(_parse_float(True, 5.0), 5.0)
		self.assertEqual(_parse_float(False, 5.0), 5.0)
		self.assertEqual(_parse_float(float("nan"), 5.0), 5.0)
		self.assertEqual(_parse_float(float("inf"), 5.0), 5.0)

		csv_with_pipe_in_quotes = (
			'Part#,Description,Source,Condition,Rate\n'
			'P-42,"Sensor | Front Bumper",OEM,Direct OEM,"$99.50"\n'
		)
		items = SupplierCatalogSanitizer().parse_rows(csv_with_pipe_in_quotes)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["part_number"], "P-42")
		self.assertEqual(items[0]["part_name"], "Sensor / Front Bumper")
		self.assertEqual(items[0]["part_condition"], "New")
		self.assertEqual(items[0]["unit_rate"], 99.5)


if __name__ == "__main__":
	unittest.main()

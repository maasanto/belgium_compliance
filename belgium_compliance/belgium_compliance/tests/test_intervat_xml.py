# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Phase 3 — INTERVAT XML serialisation.

Two layers of tests:
- Pure helpers (`_normalise_be_vat_number`, `_declarant_reference`,
  `GRID_TO_INTERVAT` collapsing) — no DB.
- `build_xml` / `validate_xml` against a stub declaration with a real
  Belgian Company in the test site (FrappeTestCase rollback).
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from lxml import etree

from belgium_compliance import intervat_xml
from belgium_compliance.intervat_xml import (
	GRID_TO_INTERVAT,
	NS_VAT,
	_declarant_reference,
	_normalise_be_vat_number,
	build_xml,
	validate_xml,
)


class TestPureHelpers(unittest.TestCase):
	"""Tests for the helpers that don't touch the database."""

	def test_normalise_strips_prefix_and_punctuation(self):
		self.assertEqual(_normalise_be_vat_number("BE 0123.456.789", "Co"), "0123456789")
		self.assertEqual(_normalise_be_vat_number("0123-456-789", "Co"), "0123456789")

	def test_normalise_pads_9_digit_legacy_format(self):
		# Legacy KBO/BCE numbers were 9 digits — INTERVAT XSD requires 10.
		self.assertEqual(_normalise_be_vat_number("123456789", "Co"), "0123456789")

	def test_normalise_rejects_wrong_length(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			_normalise_be_vat_number("12345", "Co")

	def test_normalise_rejects_empty(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			_normalise_be_vat_number(None, "Co")
		with self.assertRaises(frappe.ValidationError):
			_normalise_be_vat_number("", "Co")

	def test_declarant_reference_quarterly_format(self):
		decl = SimpleNamespace(period_type="Quarterly", period_year=2026, period_month_or_quarter=1)
		self.assertEqual(_declarant_reference(decl), "Y2026Q1")

	def test_declarant_reference_monthly_format(self):
		decl = SimpleNamespace(period_type="Monthly", period_year=2026, period_month_or_quarter=7)
		# Monthly is zero-padded to 2 digits.
		self.assertEqual(_declarant_reference(decl), "Y2026M07")

	def test_grid_46l_and_46t_collapse_to_46(self):
		self.assertEqual(GRID_TO_INTERVAT["46L"], "46")
		self.assertEqual(GRID_TO_INTERVAT["46T"], "46")

	def test_grid_codes_map_to_strings(self):
		# XSD GridNumber attribute is xs:string; build_xml must not emit ints.
		for k, v in GRID_TO_INTERVAT.items():
			self.assertIsInstance(v, str, f"{k} maps to non-string")


# ---------------------------------------------------------------------------
# Build / validate — needs Frappe DB (test Company)
# ---------------------------------------------------------------------------

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

import frappe

from belgium_compliance.belgium_compliance.tests.utils import (
	TEST_COMPANY,
	ensure_belgian_company,
)


class TestBuildXml(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_belgian_company()

	def _make_declaration_stub(self, **overrides):
		"""Return an object that quacks like a Belgian VAT Declaration for
		intervat_xml.build_xml. Real declarations live in the DB; stubbing
		keeps the test focused on serialisation."""
		base = {
			"company": TEST_COMPANY,
			"period_type": "Quarterly",
			"period_year": 2026,
			"period_month_or_quarter": 1,
			# Grids — only a few non-zero so we can assert what made it into XML.
			"g_03": 1000.00,
			"g_54": 210.00,
			"g_71": 210.00,
			"g_46l": 50.00,
			"g_46t": 75.00,
		}
		base.update(overrides)

		def get(field):
			return base.get(field, 0)

		stub = SimpleNamespace(**base, get=get)
		return stub

	def test_root_element_is_vatconsignment(self):
		stub = self._make_declaration_stub()
		xml_bytes = build_xml(stub)
		root = etree.fromstring(xml_bytes)
		self.assertEqual(root.tag, f"{{{NS_VAT}}}VATConsignment")
		self.assertEqual(root.attrib["VATDeclarationsNbr"], "1")

	def test_period_block_quarterly(self):
		stub = self._make_declaration_stub(period_type="Quarterly", period_month_or_quarter=2)
		root = etree.fromstring(build_xml(stub))
		quarter = root.find(f".//{{{NS_VAT}}}Quarter")
		year = root.find(f".//{{{NS_VAT}}}Year")
		self.assertEqual(quarter.text, "2")
		self.assertEqual(year.text, "2026")
		# Monthly element must not be present in quarterly mode.
		self.assertIsNone(root.find(f".//{{{NS_VAT}}}Month"))

	def test_period_block_monthly(self):
		stub = self._make_declaration_stub(period_type="Monthly", period_month_or_quarter=7)
		root = etree.fromstring(build_xml(stub))
		self.assertIsNone(root.find(f".//{{{NS_VAT}}}Quarter"))
		month = root.find(f".//{{{NS_VAT}}}Month")
		self.assertEqual(month.text, "7")

	def test_data_block_emits_one_amount_per_grid(self):
		stub = self._make_declaration_stub(g_03=1000.00, g_54=210.00, g_71=210.00)
		root = etree.fromstring(build_xml(stub))
		amounts = root.findall(f".//{{{NS_VAT}}}Amount")
		grids_emitted = {a.attrib["GridNumber"]: a.text for a in amounts}
		self.assertEqual(grids_emitted["3"], "1000.00")
		self.assertEqual(grids_emitted["54"], "210.00")
		self.assertEqual(grids_emitted["71"], "210.00")

	def test_46l_and_46t_collapsed_into_single_46(self):
		stub = self._make_declaration_stub(g_46l=100.00, g_46t=50.00)
		root = etree.fromstring(build_xml(stub))
		amounts_46 = [a for a in root.findall(f".//{{{NS_VAT}}}Amount") if a.attrib["GridNumber"] == "46"]
		self.assertEqual(len(amounts_46), 1, "46L and 46T must collapse to a single GridNumber=46")
		self.assertEqual(amounts_46[0].text, "150.00")

	def test_zero_grids_are_omitted(self):
		stub = self._make_declaration_stub(g_03=0, g_54=0, g_71=0)
		root = etree.fromstring(build_xml(stub))
		amounts = root.findall(f".//{{{NS_VAT}}}Amount")
		# Only the non-zero defaults from the stub remain (g_46l + g_46t collapsed).
		self.assertGreater(len(amounts), 0)
		for a in amounts:
			self.assertNotEqual(a.text, "0.00")

	def test_tail_elements_present(self):
		stub = self._make_declaration_stub()
		root = etree.fromstring(build_xml(stub))
		self.assertEqual(root.find(f".//{{{NS_VAT}}}ClientListingNihil").text, "NO")
		ask = root.find(f".//{{{NS_VAT}}}Ask")
		self.assertEqual(ask.attrib["Restitution"], "NO")
		self.assertEqual(ask.attrib["Payment"], "NO")

	def test_grid_71_satisfies_intervat_check_over_declared_amounts(self):
		# End-to-end regression for issue #2: grids fed with sub-cent precision
		# must yield an XML where grid 71 equals INTERVAT's arithmetic check
		# 71 = (54+55+56+57+61+63) − (59+62+64) computed over the *declared*
		# (2-decimal) amounts — that is what INTERVAT validates on submission.
		decl = frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Quarterly",
				"period_year": 2098,  # far-future period — guaranteed empty
				"period_month_or_quarter": 1,
				"status": "Draft",
				"adjustments": [
					{"grid": "54", "amount_type": "Tax", "amount": 28538.39417, "reason": "test"},
					{"grid": "56", "amount_type": "Tax", "amount": 521.8941, "reason": "test"},
					{"grid": "59", "amount_type": "Tax", "amount": 12571.0774, "reason": "test"},
					{"grid": "64", "amount_type": "Tax", "amount": 1899.11757, "reason": "test"},
				],
			}
		)
		decl.flags.ignore_permissions = True
		decl.insert()
		decl.compute()

		root = etree.fromstring(build_xml(decl))
		declared = {a.attrib["GridNumber"]: Decimal(a.text) for a in root.findall(f".//{{{NS_VAT}}}Amount")}
		check_71 = (declared["54"] + declared["56"]) - (declared["59"] + declared["64"])
		self.assertEqual(declared["71"], check_71)
		self.assertEqual(declared["71"], Decimal("14590.08"))

	def test_throws_when_company_has_no_tax_id(self):
		company = frappe.get_doc("Company", TEST_COMPANY)
		original = company.tax_id
		try:
			company.db_set("tax_id", None)
			stub = self._make_declaration_stub()
			with self.assertRaises(frappe.ValidationError):
				build_xml(stub)
		finally:
			company.db_set("tax_id", original)


class TestValidateXml(unittest.TestCase):
	def test_invalid_xml_returns_invalid(self):
		result = intervat_xml.validate_xml(b"<not-well-formed")
		self.assertEqual(result["status"], "invalid")
		self.assertGreater(len(result["errors"]), 0)

	def test_well_formed_but_wrong_root_returns_invalid_or_skipped(self):
		# The XSD may not load cleanly without the EU TAXUD transitive schemas.
		# When skipped, that's reported as "skipped"; otherwise it must be
		# "invalid" because a <foo/> root doesn't match VATConsignment.
		result = intervat_xml.validate_xml(b"<?xml version='1.0'?><foo/>")
		self.assertIn(result["status"], {"invalid", "skipped"})

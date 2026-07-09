# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Phase 2 — Belgian VAT Declaration: validation, period dates, total
formulas and an end-to-end compute() smoke test.

The full multi-rate extraction is exercised indirectly through the
fixture-driven materialise + a minimal sales invoice. Heavier multi-rate
matrix tests live in `test_belgian_vat_declaration_extraction.py` and
require more PCMN account scaffolding — deferred to a follow-up.
"""

from datetime import date

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from belgium_compliance.belgium_compliance.doctype.belgian_vat_declaration.belgian_vat_declaration import (
	GRID_CODES,
	TOTAL_GRID_CODES,
	grid_to_fieldname,
)
from belgium_compliance.belgium_compliance.tests.utils import (
	TEST_COMPANY,
	ensure_belgian_company,
)


class TestGridFieldname(FrappeTestCase):
	def test_uppercase_letters_lowercased(self):
		self.assertEqual(grid_to_fieldname("46L"), "g_46l")
		self.assertEqual(grid_to_fieldname("46T"), "g_46t")

	def test_numeric_codes(self):
		self.assertEqual(grid_to_fieldname("03"), "g_03")
		self.assertEqual(grid_to_fieldname("71"), "g_71")

	def test_total_codes_subset_of_grid_codes(self):
		for c in TOTAL_GRID_CODES:
			self.assertIn(c, GRID_CODES)


class TestPeriodValidation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_belgian_company()

	def _new_decl(self, **kwargs):
		base = {
			"doctype": "Belgian VAT Declaration",
			"company": TEST_COMPANY,
			"period_type": "Monthly",
			"period_year": 2026,
			"period_month_or_quarter": 1,
			"status": "Draft",
		}
		base.update(kwargs)
		return frappe.get_doc(base)

	def test_monthly_period_dates_jan(self):
		decl = self._new_decl(period_type="Monthly", period_year=2026, period_month_or_quarter=1)
		decl._compute_period_dates()
		self.assertEqual(decl.start_date, date(2026, 1, 1))
		self.assertEqual(decl.end_date, date(2026, 1, 31))

	def test_monthly_period_dates_feb_leap_year(self):
		decl = self._new_decl(period_type="Monthly", period_year=2024, period_month_or_quarter=2)
		decl._compute_period_dates()
		self.assertEqual(decl.start_date, date(2024, 2, 1))
		self.assertEqual(decl.end_date, date(2024, 2, 29))

	def test_quarterly_period_dates_q1(self):
		decl = self._new_decl(period_type="Quarterly", period_year=2026, period_month_or_quarter=1)
		decl._compute_period_dates()
		self.assertEqual(decl.start_date, date(2026, 1, 1))
		self.assertEqual(decl.end_date, date(2026, 3, 31))

	def test_quarterly_period_dates_q4(self):
		decl = self._new_decl(period_type="Quarterly", period_year=2026, period_month_or_quarter=4)
		decl._compute_period_dates()
		self.assertEqual(decl.start_date, date(2026, 10, 1))
		self.assertEqual(decl.end_date, date(2026, 12, 31))

	def test_invalid_monthly_index_raises(self):
		decl = self._new_decl(period_type="Monthly", period_month_or_quarter=13)
		with self.assertRaises(frappe.ValidationError):
			decl._validate_period()

	def test_invalid_quarterly_index_raises(self):
		decl = self._new_decl(period_type="Quarterly", period_month_or_quarter=5)
		with self.assertRaises(frappe.ValidationError):
			decl._validate_period()

	def test_invalid_period_type_raises(self):
		decl = self._new_decl(period_type="Yearly")
		with self.assertRaises(frappe.ValidationError):
			decl._validate_period()


class TestTotalFormulas(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_belgian_company()

	def _new_decl(self):
		return frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Quarterly",
				"period_year": 2026,
				"period_month_or_quarter": 1,
				"status": "Draft",
			}
		)

	def test_g_71_when_due_exceeds_deductible(self):
		decl = self._new_decl()
		decl.set("g_54", 100)
		decl.set("g_55", 50)
		decl.set("g_59", 20)
		decl._apply_total_formulas()
		# Net = (100+50) - 20 = 130 → g_71 = 130, g_72 = 0
		self.assertEqual(decl.g_71, 130)
		self.assertEqual(decl.g_72, 0)

	def test_g_72_when_deductible_exceeds_due(self):
		decl = self._new_decl()
		decl.set("g_54", 50)
		decl.set("g_59", 200)
		decl._apply_total_formulas()
		# Net = 50 - 200 = -150 → g_71 = 0, g_72 = 150
		self.assertEqual(decl.g_71, 0)
		self.assertEqual(decl.g_72, 150)

	def test_total_formulas_clamp_to_zero(self):
		# Both totals must be non-negative.
		decl = self._new_decl()
		decl._apply_total_formulas()
		self.assertGreaterEqual(decl.g_71, 0)
		self.assertGreaterEqual(decl.g_72, 0)

	def test_g_71_consistent_with_declared_grid_values(self):
		# Regression for issue #2: grids carrying sub-cent precision must be
		# rounded before deriving 71/72, otherwise the XML declares a grid 71
		# that fails INTERVAT's check 71 = (54+55+56+57+61+63) − (59+62+64)
		# over the independently-rounded box values.
		decl = self._new_decl()
		decl.set("g_54", 28538.39417)
		decl.set("g_55", 3296.79)
		decl.set("g_56", 521.8941)
		decl.set("g_63", 218.6524)
		decl.set("g_59", 12571.0774)
		decl.set("g_64", 1899.11757)
		decl._round_grids()
		decl._apply_total_formulas()

		# Each box is declared rounded to the cent (55/63 were already exact)...
		self.assertEqual(decl.g_54, 28538.39)
		self.assertEqual(decl.g_55, 3296.79)
		self.assertEqual(decl.g_56, 521.89)
		self.assertEqual(decl.g_63, 218.65)
		self.assertEqual(decl.g_59, 12571.08)
		self.assertEqual(decl.g_64, 1899.12)
		# ...and 71 equals the formula over those declared values, not the
		# 18105.54 the raw floats would have produced.
		self.assertEqual(decl.g_71, 18105.52)
		self.assertEqual(decl.g_72, 0)


class TestListViewMeta(FrappeTestCase):
	"""Regression for issue #22: a DocType whose ``title_field`` points at
	something that is not a real DocField (e.g. the ``name`` primary key)
	makes the list view header crash — ``get_docfield`` returns ``None`` and
	the Subject column dereferences ``.fieldname`` on it. Guard every DocType
	in the module against reintroducing that.
	"""

	def test_module_title_fields_resolve_to_real_docfields(self):
		doctypes = frappe.get_all("DocType", filters={"module": "Belgium Compliance"}, pluck="name")
		self.assertIn("Belgian VAT Declaration", doctypes)
		for name in doctypes:
			meta = frappe.get_meta(name)
			if not meta.title_field:
				continue
			self.assertIsNotNone(
				meta.get_field(meta.title_field),
				f"{name}: title_field '{meta.title_field}' is not a DocField — "
				f"the list view header will crash (issue #22)",
			)


class TestComputeSmoke(FrappeTestCase):
	"""Compute on an empty period — no invoices, no adjustments, all grids zero."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_belgian_company()

	def test_compute_on_empty_period_yields_zero_grids(self):
		decl = frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Quarterly",
				"period_year": 2099,  # far-future period — guaranteed empty
				"period_month_or_quarter": 1,
				"status": "Draft",
			}
		)
		decl.flags.ignore_permissions = True
		decl.insert()
		decl.compute()

		for code in GRID_CODES:
			self.assertEqual(
				decl.get_grid_value(code),
				0,
				f"empty period must leave grid {code} at zero",
			)
		self.assertEqual(decl.status, "Ready")

	def test_compute_resets_grids_on_replay(self):
		decl = frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Monthly",
				"period_year": 2099,
				"period_month_or_quarter": 6,
				"status": "Draft",
			}
		)
		decl.flags.ignore_permissions = True
		decl.insert()
		# Pre-populate some bogus grid value, then compute — expect reset.
		decl.set("g_03", 999)
		decl.compute()
		self.assertEqual(decl.get_grid_value("03"), 0)

	def test_compute_rounds_grids_to_the_cent(self):
		# Full compute() path (issue #2): sub-cent amounts entering the grids
		# through adjustments must come out cent-rounded, with 71 derived from
		# the rounded values.
		decl = frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Quarterly",
				"period_year": 2099,  # far-future period — guaranteed empty
				"period_month_or_quarter": 3,
				"status": "Draft",
				"adjustments": [
					{"grid": "54", "amount_type": "Tax", "amount": 28538.39417, "reason": "test"},
					{"grid": "59", "amount_type": "Tax", "amount": 12571.0774, "reason": "test"},
				],
			}
		)
		decl.flags.ignore_permissions = True
		decl.insert()
		decl.compute()

		self.assertEqual(decl.get_grid_value("54"), 28538.39)
		self.assertEqual(decl.get_grid_value("59"), 12571.08)
		# 71 must match the formula over the declared (rounded) values:
		# 28538.39 − 12571.08.
		self.assertEqual(decl.get_grid_value("71"), 15967.31)

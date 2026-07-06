# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Line-level base extraction: a negative item line on a regular invoice — a
discount, or a returned-empties (consigne) line on a beverage invoice — must
net against the base box its positive siblings feed, not inflate it, while
return documents keep feeding the Refund grids with positive contributions.

Covers issue #18.

Submitting accounting documents commits, so the per-test transaction rollback
can't undo them — every helper registers an explicit cleanup instead, which
also keeps the suite re-runnable.
"""

from datetime import date

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from belgium_compliance.belgium_compliance.tests.utils import (
	TEST_COMPANY,
	destroy_declaration,
	destroy_submittable,
	ensure_fiscal_year,
	ensure_service_item,
	ensure_vat_settings,
	make_declaration,
	make_purchase_invoice,
	make_sales_invoice,
	template_for_definition,
)
from belgium_compliance.setup import materialise_for_company

# Own far-future year so leftovers from the other declaration test files
# (2035, 2099) can never bleed into these periods.
PERIOD_YEAR = 2036
PERIOD_QUARTER = 1
POSTING_DATE = date(PERIOD_YEAR, 2, 15)


def _consigne_line(amount: float) -> dict:
	"""A returned-empties line: positive qty × negative rate, on its own item
	so it doesn't trip ERPNext's duplicate-item guard."""
	return {"qty": 1, "rate": amount, "item_code": ensure_service_item("BE Test Consigne")}


class InvoiceLineExtractionTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		ensure_fiscal_year(PERIOD_YEAR)
		materialise_for_company(TEST_COMPANY)
		cls._allow_negative_rates()
		cls.sales_template_21 = template_for_definition(
			"be_vat_sales_21_goods", "Sales Taxes and Charges Template"
		)
		cls.purchase_template_21 = template_for_definition(
			"be_vat_purchase_21_goods", "Purchase Taxes and Charges Template"
		)

	@classmethod
	def _allow_negative_rates(cls):
		# ERPNext refuses negative unit rates unless this toggle is on — sites
		# that book discount/consigne lines (the scenario under test) enable it.
		# Upstream v15 carries the field on Selling Settings only (one global
		# guard for all doctypes); newer forks split it per direction and add
		# it to Buying Settings — set whichever exist.
		for doctype in ("Selling Settings", "Buying Settings"):
			if not frappe.get_meta(doctype).has_field("allow_negative_rates_for_items"):
				continue
			previous = frappe.db.get_single_value(doctype, "allow_negative_rates_for_items")
			frappe.db.set_single_value(doctype, "allow_negative_rates_for_items", 1)
			cls.addClassCleanup(
				frappe.db.set_single_value, doctype, "allow_negative_rates_for_items", previous
			)

	def _sales_invoice(self, items: list[dict], is_return: bool = False):
		invoice = make_sales_invoice(items, self.sales_template_21, POSTING_DATE, is_return=is_return)
		self.addCleanup(destroy_submittable, "Sales Invoice", invoice.name)
		return invoice

	def _purchase_invoice(self, items: list[dict], is_return: bool = False):
		invoice = make_purchase_invoice(items, self.purchase_template_21, POSTING_DATE, is_return=is_return)
		self.addCleanup(destroy_submittable, "Purchase Invoice", invoice.name)
		return invoice

	def _compute_declaration(self):
		declaration = make_declaration(PERIOD_YEAR, PERIOD_QUARTER)
		self.addCleanup(destroy_declaration, declaration.name)
		declaration.compute()
		return declaration


class TestNegativeLinesOnRegularInvoices(InvoiceLineExtractionTestCase):
	"""A negative line on a non-return invoice reduces the base box — the
	accountant's per-document treatment nets it against the positive lines."""

	def test_sales_negative_line_nets_against_box_03(self):
		invoice = self._sales_invoice([{"qty": 1, "rate": 1000}, _consigne_line(-150)])
		self.assertEqual(invoice.base_net_total, 850, "precondition: ERPNext nets the invoice base")

		decl = self._compute_declaration()

		self.assertEqual(decl.get_grid_value("03"), 850)
		# The negative line is not a return — nothing may reach the refund grid.
		self.assertEqual(decl.get_grid_value("49"), 0)
		# The consistency cross-check recomputes on the netted base
		# (850 × 21% = 178.50) and agrees with the booked VAT in box 54.
		self.assertEqual(decl.get_grid_value("54"), 178.5)
		self.assertAlmostEqual(decl._recomputed_tax["54"], 178.5, places=2)

	def test_purchase_negative_line_nets_against_box_81(self):
		# The issue's scenario: a beverage purchase with a returned-empties line.
		invoice = self._purchase_invoice([{"qty": 1, "rate": 1000}, _consigne_line(-150)])
		self.assertEqual(invoice.base_net_total, 850, "precondition: ERPNext nets the invoice base")

		decl = self._compute_declaration()

		self.assertEqual(decl.get_grid_value("81"), 850)
		self.assertEqual(decl.get_grid_value("85"), 0)
		self.assertEqual(decl.get_grid_value("59"), 178.5)
		self.assertAlmostEqual(decl._recomputed_tax["59"], 178.5, places=2)


class TestReturnDocumentsKeepRefundRouting(InvoiceLineExtractionTestCase):
	"""ERPNext stores return-document lines negative; the Refund grid tags
	expect positive contributions, and the regular base boxes stay untouched."""

	def test_sales_credit_note_feeds_box_49_positive(self):
		self._sales_invoice([{"qty": -1, "rate": 850}], is_return=True)

		decl = self._compute_declaration()

		self.assertEqual(decl.get_grid_value("49"), 850)
		self.assertEqual(decl.get_grid_value("03"), 0)
		# Credit-note VAT lands in the regularisation box 64 — booked (GL debit
		# on the output VAT account) and recomputed alike.
		self.assertEqual(decl.get_grid_value("64"), 178.5)
		self.assertAlmostEqual(decl._recomputed_tax["64"], 178.5, places=2)

	def test_purchase_debit_note_feeds_box_85_positive(self):
		self._purchase_invoice([{"qty": -1, "rate": 850}], is_return=True)

		decl = self._compute_declaration()

		self.assertEqual(decl.get_grid_value("85"), 850)
		self.assertEqual(decl.get_grid_value("81"), 0)
		self.assertEqual(decl.get_grid_value("63"), 178.5)
		self.assertAlmostEqual(decl._recomputed_tax["63"], 178.5, places=2)

	def test_mixed_sign_credit_note_nets_the_refund_grid(self):
		# A charge-back line on a credit note (positive amount: negative qty ×
		# negative rate) nets against the refund grid — the return-side mirror
		# of the discount/consigne treatment on regular invoices.
		charge_back = {"qty": -1, "rate": -150, "item_code": ensure_service_item("BE Test Consigne")}
		invoice = self._sales_invoice([{"qty": -1, "rate": 1000}, charge_back], is_return=True)
		self.assertEqual(invoice.base_net_total, -850, "precondition: ERPNext nets the credit note base")

		decl = self._compute_declaration()

		self.assertEqual(decl.get_grid_value("49"), 850)
		self.assertEqual(decl.get_grid_value("03"), 0)
		self.assertEqual(decl.get_grid_value("64"), 178.5)
		self.assertAlmostEqual(decl._recomputed_tax["64"], 178.5, places=2)

# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Booked-VAT reading: the declaration's tax boxes (54/59/63/64) must reflect
the VAT actually posted to the ledger — invoice- and JE-booked alike — rather
than a per-line base × rate recomputation that drifts by the cent, and JE bases
must reach the declaration through the account→grid mapping.

Covers issue #3.

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
	INPUT_VAT_ACCOUNT_NAME,
	OUTPUT_VAT_ACCOUNT_NAME,
	TEST_COMPANY,
	_ensure_account,
	destroy_declaration,
	destroy_submittable,
	ensure_bank_account,
	ensure_fiscal_year,
	ensure_income_account,
	ensure_vat_settings,
	make_declaration,
	make_sales_invoice,
	template_for_definition,
)
from belgium_compliance.setup import materialise_for_company

# A far-future year the site is guaranteed not to ship a Fiscal Year for, so
# ensure_fiscal_year always creates a fresh global one rather than mutating a
# shipped fixture. Each test runs in its own rolled-back transaction.
PERIOD_YEAR = 2035
PERIOD_QUARTER = 1
POSTING_DATE = date(PERIOD_YEAR, 2, 15)


def _abbr() -> str:
	return frappe.db.get_value("Company", TEST_COMPANY, "abbr")


def _acct(name: str) -> str:
	return f"{name} - {_abbr()}"


class BookedVatTestCase(FrappeTestCase):
	"""Shared scaffolding + self-cleaning helpers for the booked-VAT tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		ensure_fiscal_year(PERIOD_YEAR)
		cls.bank = ensure_bank_account()
		cls.income = ensure_income_account()
		cls.output_vat = _acct(OUTPUT_VAT_ACCOUNT_NAME)
		cls.input_vat = _acct(INPUT_VAT_ACCOUNT_NAME)

	def _post_journal_entry(self, lines: list[dict], posting_date=POSTING_DATE):
		cost_center = frappe.db.get_value("Company", TEST_COMPANY, "cost_center")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": TEST_COMPANY,
				"posting_date": posting_date,
				"voucher_type": "Journal Entry",
				"accounts": [
					{
						"account": line["account"],
						"debit_in_account_currency": line.get("debit", 0),
						"credit_in_account_currency": line.get("credit", 0),
						"cost_center": cost_center,
					}
					for line in lines
				],
			}
		)
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		self.addCleanup(destroy_submittable, "Journal Entry", je.name)
		return je

	def _new_declaration(self):
		declaration = make_declaration(PERIOD_YEAR, PERIOD_QUARTER)
		self.addCleanup(destroy_declaration, declaration.name)
		return declaration


class TestBookedVatFromLedger(BookedVatTestCase):
	"""Tax boxes filled from GL Entries on the configured VAT accounts."""

	def test_output_vat_credit_flows_to_box_54(self):
		self._post_journal_entry(
			[
				{"account": self.bank, "debit": 210},
				{"account": self.output_vat, "credit": 210},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("54"), 210)
		self.assertEqual(decl.get_grid_value("64"), 0)

	def test_output_vat_debit_flows_to_regularisation_box_64(self):
		self._post_journal_entry(
			[
				{"account": self.output_vat, "debit": 30},
				{"account": self.bank, "credit": 30},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("64"), 30)
		self.assertEqual(decl.get_grid_value("54"), 0)

	def test_deductible_vat_debit_flows_to_box_59(self):
		self._post_journal_entry(
			[
				{"account": self.input_vat, "debit": 100},
				{"account": self.bank, "credit": 100},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("59"), 100)
		self.assertEqual(decl.get_grid_value("63"), 0)

	def test_deductible_vat_credit_flows_to_regularisation_box_63(self):
		self._post_journal_entry(
			[
				{"account": self.bank, "debit": 15},
				{"account": self.input_vat, "credit": 15},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("63"), 15)
		self.assertEqual(decl.get_grid_value("59"), 0)

	def test_cancelled_gl_entries_are_excluded(self):
		je = self._post_journal_entry(
			[
				{"account": self.bank, "debit": 42},
				{"account": self.output_vat, "credit": 42},
			]
		)
		je.cancel()
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("54"), 0)


class TestJournalEntryBaseMapping(BookedVatTestCase):
	"""JE-booked bases reach the declaration via the account→grid mapping."""

	def _map_income_to_grid(self, grid: str, sign: int = 1):
		settings = frappe.get_doc("Belgian VAT Settings", TEST_COMPANY)
		settings.set("journal_mappings", [])
		if grid:
			settings.append("journal_mappings", {"account": self.income, "grid": grid, "sign": sign})
		settings.flags.ignore_permissions = True
		settings.save()
		self.addCleanup(self._clear_journal_mappings)

	@staticmethod
	def _clear_journal_mappings():
		settings = frappe.get_doc("Belgian VAT Settings", TEST_COMPANY)
		settings.set("journal_mappings", [])
		settings.flags.ignore_permissions = True
		settings.save()

	def test_income_journal_entry_flows_to_base_grid(self):
		self._map_income_to_grid("03")
		self._post_journal_entry(
			[
				{"account": self.bank, "debit": 1000},
				{"account": self.income, "credit": 1000},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("03"), 1000)

	def test_unmapped_account_does_not_flow(self):
		self._map_income_to_grid("")  # no mapping rows
		self._post_journal_entry(
			[
				{"account": self.bank, "debit": 500},
				{"account": self.income, "credit": 500},
			]
		)
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("03"), 0)

	def test_mapping_a_vat_account_is_rejected(self):
		# VAT is read from the ledger already — mapping a VAT account as a base
		# account would double-count, so the settings must refuse it.
		self.addCleanup(self._clear_journal_mappings)
		settings = frappe.get_doc("Belgian VAT Settings", TEST_COMPANY)
		settings.set("journal_mappings", [])
		settings.append("journal_mappings", {"account": self.output_vat, "grid": "03", "sign": 1})
		settings.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			settings.save()


class TestSettlementVoucherExclusion(BookedVatTestCase):
	"""The periodic centralisation OD — clearing the VAT accounts into the
	compte courant TVA (PCMN 4519) — is a settlement transfer, not a VAT
	event: its legs must not land in the regularisation boxes 63/64.

	Covers issue #16."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settlement = _ensure_account("BE Test VAT Current Account", "Tax", "Liability", TEST_COMPANY)

	def _set_settlement_account(self, account: str | None):
		settings = frappe.get_doc("Belgian VAT Settings", TEST_COMPANY)
		settings.vat_settlement_account = account
		settings.flags.ignore_permissions = True
		settings.save()
		self.addCleanup(self._clear_settlement_account)

	@staticmethod
	def _clear_settlement_account():
		frappe.db.set_value("Belgian VAT Settings", TEST_COMPANY, "vat_settlement_account", None)

	def _post_quarter_activity_and_centralisation(self):
		# Normal activity: 210 output VAT collected, 100 input VAT deducted.
		self._post_journal_entry(
			[
				{"account": self.bank, "debit": 210},
				{"account": self.output_vat, "credit": 210},
			]
		)
		self._post_journal_entry(
			[
				{"account": self.input_vat, "debit": 100},
				{"account": self.bank, "credit": 100},
			]
		)
		# Quarter-end centralisation OD: clear both VAT accounts into the
		# current account. Without the exclusion this reads as 64 += 210,
		# 63 += 100.
		self._post_journal_entry(
			[
				{"account": self.output_vat, "debit": 210},
				{"account": self.input_vat, "credit": 100},
				{"account": self.settlement, "credit": 110},
			]
		)

	def test_settlement_voucher_is_excluded_from_vat_boxes(self):
		self._set_settlement_account(self.settlement)
		self._post_quarter_activity_and_centralisation()
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("54"), 210)
		self.assertEqual(decl.get_grid_value("59"), 100)
		self.assertEqual(decl.get_grid_value("64"), 0)
		self.assertEqual(decl.get_grid_value("63"), 0)

	def test_without_settlement_account_transfer_lands_in_boxes(self):
		# Documented fallback: with no settlement account configured the
		# centralisation legs are read as regularisations (historic behaviour).
		self._set_settlement_account(None)
		self._post_quarter_activity_and_centralisation()
		decl = self._new_declaration()
		decl.compute()
		self.assertEqual(decl.get_grid_value("64"), 210)
		self.assertEqual(decl.get_grid_value("63"), 100)

	def test_settlement_account_must_not_be_a_vat_account(self):
		self.addCleanup(self._clear_settlement_account)
		settings = frappe.get_doc("Belgian VAT Settings", TEST_COMPANY)
		settings.vat_settlement_account = self.output_vat
		settings.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			settings.save()


class TestBookedVatInvoiceRounding(BookedVatTestCase):
	"""The canonical regression: box 54 must equal the VAT booked on the
	invoice, not the per-line base × rate recomputation that carries sub-cent
	drift."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		materialise_for_company(TEST_COMPANY)
		cls.sales_template_21 = template_for_definition(
			"be_vat_sales_21_goods", "Sales Taxes and Charges Template"
		)

	def _create_sales_invoice(self, base_net: float):
		invoice = make_sales_invoice([{"qty": 1, "rate": base_net}], self.sales_template_21, POSTING_DATE)
		self.addCleanup(destroy_submittable, "Sales Invoice", invoice.name)
		return invoice

	def test_box_54_equals_booked_vat_not_recomputed(self):
		# 100.05 × 21% = 21.0105 → booked (rounded) 21.01. The old per-line
		# recomputation would store 21.0105.
		invoice = self._create_sales_invoice(base_net=100.05)
		booked_vat = frappe.db.get_value(
			"GL Entry",
			{"voucher_no": invoice.name, "account": self.output_vat, "is_cancelled": 0},
			"credit",
		)
		self.assertEqual(booked_vat, 21.01, "precondition: ERPNext booked rounded VAT")

		decl = self._new_declaration()
		decl.compute()

		self.assertEqual(decl.get_grid_value("54"), 21.01)
		self.assertNotEqual(decl.get_grid_value("54"), 21.0105)
		# Base still comes from the invoice line.
		self.assertEqual(decl.get_grid_value("03"), 100.05)

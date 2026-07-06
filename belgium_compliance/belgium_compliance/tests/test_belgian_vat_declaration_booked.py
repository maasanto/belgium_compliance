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
	ensure_vat_settings,
)
from belgium_compliance.setup import materialise_for_company

# A far-future year the site is guaranteed not to ship a Fiscal Year for, so
# _ensure_fiscal_year always creates a fresh global one rather than mutating a
# shipped fixture. Each test runs in its own rolled-back transaction.
PERIOD_YEAR = 2035
PERIOD_QUARTER = 1
POSTING_DATE = date(PERIOD_YEAR, 2, 15)


def _abbr() -> str:
	return frappe.db.get_value("Company", TEST_COMPANY, "abbr")


def _acct(name: str) -> str:
	return f"{name} - {_abbr()}"


def _ensure_neutral_accounts() -> tuple[str, str]:
	"""A non-VAT bank and a non-VAT income account, used to balance the test
	Journal Entries without polluting the VAT boxes."""
	bank = _ensure_account("BE Test Bank", "Bank", "Asset", TEST_COMPANY)
	income = _ensure_account("BE Test Revenue", "", "Income", TEST_COMPANY)
	return bank, income


def _ensure_fiscal_year(year: int) -> None:
	"""Guarantee the test period falls in an active Fiscal Year for the company.

	A bare CI site has no Fiscal Year covering the period, or one restricted to
	other companies (ERPNext's shipped `2026` is scoped to `_Test Company`), so
	posting a JE/invoice raises FiscalYearError. Decide off the DB — never off
	`get_fiscal_year`, whose per-company cache can outlive a class rollback and
	report a year that no longer exists — then clear that cache so the posting
	sees the change."""
	name = str(year)
	if not frappe.db.exists("Fiscal Year", name):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": name,
				"year_start_date": date(year, 1, 1),
				"year_end_date": date(year, 12, 31),
			}
		).insert(ignore_permissions=True)
	else:
		fiscal_year = frappe.get_doc("Fiscal Year", name)
		# A non-empty companies list restricts the year to those companies —
		# add ours. An empty list means the year is global and already applies.
		if fiscal_year.companies and not any(row.company == TEST_COMPANY for row in fiscal_year.companies):
			fiscal_year.append("companies", {"company": TEST_COMPANY})
			fiscal_year.save(ignore_permissions=True)

	frappe.cache().delete_value("fiscal_years")


def _ensure_uom() -> str:
	"""Return a usable UOM, creating 'Nos' if the site ships none enabled."""
	existing = frappe.db.get_value("UOM", {"enabled": 1}, "name")
	if existing:
		return existing
	if frappe.db.exists("UOM", "Nos"):
		return "Nos"
	uom = frappe.get_doc({"doctype": "UOM", "uom_name": "Nos", "enabled": 1})
	uom.flags.ignore_permissions = True
	uom.insert()
	return uom.name


def _ensure_selling_price_list() -> str:
	"""Return an enabled selling Price List, creating one if the site ships none
	(a bare install without the setup wizard has no default price list)."""
	existing = frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
	if existing:
		return existing
	name = "BE Test Selling"
	if not frappe.db.exists("Price List", name):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"selling": 1,
				"enabled": 1,
				"currency": "EUR",
			}
		).insert(ignore_permissions=True)
	return name


class BookedVatTestCase(FrappeTestCase):
	"""Shared scaffolding + self-cleaning helpers for the booked-VAT tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		_ensure_fiscal_year(PERIOD_YEAR)
		cls.bank, cls.income = _ensure_neutral_accounts()
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
		self.addCleanup(self._destroy_submittable, "Journal Entry", je.name)
		return je

	def _new_declaration(self):
		name = f"TVA-BE-{_abbr()}-{PERIOD_YEAR}-Q{PERIOD_QUARTER}"
		# A prior aborted run may have left this committed — start clean.
		if frappe.db.exists("Belgian VAT Declaration", name):
			frappe.delete_doc("Belgian VAT Declaration", name, force=True, delete_permanently=True)
		decl = frappe.get_doc(
			{
				"doctype": "Belgian VAT Declaration",
				"company": TEST_COMPANY,
				"period_type": "Quarterly",
				"period_year": PERIOD_YEAR,
				"period_month_or_quarter": PERIOD_QUARTER,
				"status": "Draft",
			}
		)
		decl.flags.ignore_permissions = True
		decl.insert()
		self.addCleanup(self._destroy_declaration, decl.name)
		return decl

	@staticmethod
	def _destroy_submittable(doctype: str, name: str):
		if not frappe.db.exists(doctype, name):
			return
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		# Cancellation already excludes the doc from compute (is_cancelled=1).
		# Dokos seals accounting documents, so a hard delete is refused — that's
		# fine, the cancelled row no longer contributes to any box.
		try:
			frappe.delete_doc(doctype, name, force=True, delete_permanently=True)
		except frappe.ValidationError:
			pass

	@staticmethod
	def _destroy_declaration(name: str):
		if frappe.db.exists("Belgian VAT Declaration", name):
			frappe.delete_doc("Belgian VAT Declaration", name, force=True, delete_permanently=True)


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


class TestBookedVatInvoiceRounding(BookedVatTestCase):
	"""The canonical regression: box 54 must equal the VAT booked on the
	invoice, not the per-line base × rate recomputation that carries sub-cent
	drift."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		materialise_for_company(TEST_COMPANY)
		cls.sales_template_21 = cls._sales_template_21()

	@classmethod
	def _sales_template_21(cls) -> str:
		definition = frappe.db.get_value(
			"Belgian VAT Tax Definition",
			{"direction": "Sales", "tax_type": "Standard", "rate": 21},
			"name",
		)
		assert definition, "no Standard 21% Sales definition in fixtures"
		return frappe.db.get_value(
			"Belgian VAT Tax Template Link",
			{
				"company": TEST_COMPANY,
				"tax_definition": definition,
				"tax_template_doctype": "Sales Taxes and Charges Template",
			},
			"tax_template",
		)

	def _create_sales_invoice(self, base_net: float):
		customer = _ensure_customer()
		item = _ensure_service_item(self.income)
		cost_center = frappe.db.get_value("Company", TEST_COMPANY, "cost_center")

		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": TEST_COMPANY,
				"customer": customer,
				"set_posting_time": 1,
				"posting_date": POSTING_DATE,
				"due_date": POSTING_DATE,
				# Pin currency + pricing explicitly so a bare site (no default
				# price list / exchange rate) needs no master lookups, and no
				# round-off account (disabled) is required.
				"currency": "EUR",
				"conversion_rate": 1.0,
				"selling_price_list": _ensure_selling_price_list(),
				"price_list_currency": "EUR",
				"plc_conversion_rate": 1.0,
				"ignore_pricing_rule": 1,
				"disable_rounded_total": 1,
				"update_stock": 0,
				"taxes_and_charges": self.sales_template_21,
				"items": [
					{
						"item_code": item,
						"qty": 1,
						"rate": base_net,
						"price_list_rate": base_net,
						"income_account": self.income,
						"cost_center": cost_center,
					}
				],
			}
		)
		# Pull the template's tax rows explicitly so the VAT posts regardless of
		# whether server-side auto-population runs in the test harness.
		template = frappe.get_doc("Sales Taxes and Charges Template", self.sales_template_21)
		for row in template.taxes:
			invoice.append(
				"taxes",
				{
					"charge_type": row.charge_type,
					"account_head": row.account_head,
					"rate": row.rate,
					"description": row.description,
					"category": row.get("category"),
				},
			)
		invoice.flags.ignore_permissions = True
		invoice.insert()
		invoice.submit()
		self.addCleanup(self._destroy_submittable, "Sales Invoice", invoice.name)
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


def _ensure_leaf(doctype: str, name_field: str, parent_field: str) -> str:
	"""Return a leaf node of a nested-set master (Item Group / Customer Group /
	Territory), creating one under the root if the site ships none — a bare CI
	install has the tree roots but not always the child leaves these tests need."""
	existing = frappe.db.get_value(doctype, {"is_group": 0}, "name")
	if existing:
		return existing
	fallback = f"BE Test {doctype}"
	if frappe.db.exists(doctype, fallback):
		return fallback
	parent = frappe.db.get_value(doctype, {"is_group": 1}, "name")
	doc = frappe.get_doc({"doctype": doctype, name_field: fallback, parent_field: parent, "is_group": 0})
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_customer() -> str:
	name = "BE Test Customer"
	if frappe.db.exists("Customer", name):
		return name
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_group": _ensure_leaf("Customer Group", "customer_group_name", "parent_customer_group"),
			"territory": _ensure_leaf("Territory", "territory_name", "parent_territory"),
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert()
	return customer.name


def _ensure_service_item(income_account: str) -> str:
	code = "BE Test Service"
	if frappe.db.exists("Item", code):
		return code
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_group": _ensure_leaf("Item Group", "item_group_name", "parent_item_group"),
			"stock_uom": _ensure_uom(),
			"is_stock_item": 0,
			"is_sales_item": 1,
		}
	)
	item.flags.ignore_permissions = True
	item.insert()
	return item.name

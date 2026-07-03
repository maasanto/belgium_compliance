# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Phase 1 — Materialisation of Belgian VAT Tax Definitions into ERPNext
Sales / Purchase Taxes and Charges Templates and Item Tax Templates.
"""

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from belgium_compliance.belgium_compliance.tests.utils import (
	DOMESTIC_RC_DUE_ACCOUNT_NAME,
	RC_DUE_ACCOUNT_NAME,
	TEST_COMPANY,
	ensure_belgian_company,
	ensure_vat_settings,
)
from belgium_compliance.setup import _accounts_for, materialise_for_company


def _reset_tax_template_state(company: str) -> None:
	"""Drop any Belgian VAT Tax Template Links + their associated templates
	for the given company. Used when a test class needs to start from zero."""
	for link_name in frappe.get_all(
		"Belgian VAT Tax Template Link",
		filters={"company": company},
		pluck="name",
	):
		frappe.delete_doc(
			"Belgian VAT Tax Template Link",
			link_name,
			force=True,
			delete_permanently=True,
		)
	for doctype in (
		"Sales Taxes and Charges Template",
		"Purchase Taxes and Charges Template",
		"Item Tax Template",
	):
		for name in frappe.get_all(
			doctype,
			filters={"company": company, "title": ("like", "BE %")},
			pluck="name",
		):
			frappe.delete_doc(doctype, name, force=True, delete_permanently=True)


class TestMaterialise(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		# Other test classes (e.g. TestGetTaxDefinitionForTemplate) run before
		# this one alphabetically and leave behind Tax Template Links — those
		# would make every Tax Definition look "already materialised", so the
		# dry-run/live tests would return created=0. Reset to a clean slate.
		_reset_tax_template_state(TEST_COMPANY)

	def test_dry_run_creates_nothing(self):
		# Capture template counts before/after the dry run.
		before_sales = frappe.db.count("Sales Taxes and Charges Template", {"company": TEST_COMPANY})
		before_purchase = frappe.db.count("Purchase Taxes and Charges Template", {"company": TEST_COMPANY})

		result = materialise_for_company(TEST_COMPANY, dry_run=1)

		self.assertEqual(result["dry_run"], True)
		self.assertGreater(result["created"], 0, "dry-run should report what would be created")

		self.assertEqual(
			frappe.db.count("Sales Taxes and Charges Template", {"company": TEST_COMPANY}),
			before_sales,
		)
		self.assertEqual(
			frappe.db.count("Purchase Taxes and Charges Template", {"company": TEST_COMPANY}),
			before_purchase,
		)

	def test_live_run_creates_templates(self):
		result = materialise_for_company(TEST_COMPANY)

		self.assertEqual(result["errors"], 0, f"errors during materialisation: {result.get('error_details')}")
		self.assertGreater(result["created"], 0)

		links = frappe.db.count("Belgian VAT Tax Template Link", {"company": TEST_COMPANY})
		self.assertGreater(links, 0)

	def test_second_run_is_idempotent(self):
		# Run twice — the second pass must skip everything created by the first.
		first = materialise_for_company(TEST_COMPANY)
		second = materialise_for_company(TEST_COMPANY)

		self.assertEqual(second["created"], 0, "second run should create nothing")
		self.assertEqual(second["errors"], 0)
		self.assertGreaterEqual(second["skipped"], first["skipped"])

	def test_throws_without_settings(self):
		# Use an isolated company name that hasn't had Belgian VAT Settings set up.
		other = "Test BE NoSettings Co"
		if not frappe.db.exists("Company", other):
			doc = frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": other,
					"abbr": "TBNC",
					"default_currency": "EUR",
					"country": "Belgium",
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert()

		with self.assertRaises(frappe.ValidationError):
			materialise_for_company(other)


class TestAccountsForRouting(FrappeTestCase):
	"""Unit coverage of _accounts_for — pure routing, no DB records needed."""

	def _settings(self, **overrides):
		settings = frappe._dict(
			output_vat_account="Output VAT",
			input_vat_deductible_account="Input VAT",
			reverse_charge_vat_due_account="RC VAT Due",
			domestic_reverse_charge_vat_due_account="Domestic RC VAT Due",
			reverse_charge_vat_deductible_account="RC VAT Deductible",
		)
		settings.update(overrides)
		return settings

	def _rc_due_account(self, tax_type, settings):
		definition = frappe._dict(direction="Purchase", tax_type=tax_type, code="test")
		accounts = dict(_accounts_for(definition, settings))
		return accounts["rc_due"]

	def test_domestic_reverse_charge_uses_dedicated_account(self):
		self.assertEqual(
			self._rc_due_account("Reverse Charge Domestic", self._settings()),
			"Domestic RC VAT Due",
		)

	def test_domestic_reverse_charge_falls_back_to_shared_account(self):
		settings = self._settings(domestic_reverse_charge_vat_due_account=None)
		self.assertEqual(self._rc_due_account("Reverse Charge Domestic", settings), "RC VAT Due")

	def test_other_reverse_charge_types_keep_shared_account(self):
		for tax_type in ("Intra-EU Acquisition", "Reverse Charge EU", "Import"):
			self.assertEqual(
				self._rc_due_account(tax_type, self._settings()),
				"RC VAT Due",
				f"{tax_type} must not use the domestic cocontractant account",
			)

	def test_sales_direction_unaffected(self):
		definition = frappe._dict(direction="Sales", tax_type="Reverse Charge Domestic", code="test")
		self.assertEqual(_accounts_for(definition, self._settings()), [("output", "Output VAT")])


class TestReverseChargeDueAccountMaterialisation(FrappeTestCase):
	"""End-to-end check that materialised Purchase templates carry the right
	VAT-due account per reverse-charge regime."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		materialise_for_company(TEST_COMPANY)
		cls.abbr = frappe.db.get_value("Company", TEST_COMPANY, "abbr")

	def _materialised_due_account(self, tax_type: str) -> str:
		definition = frappe.db.get_value(
			"Belgian VAT Tax Definition",
			{"tax_type": tax_type, "direction": "Purchase", "rate": (">", 0)},
			"name",
		)
		self.assertIsNotNone(definition, f"no Purchase definition with tax_type {tax_type}")

		template_name = frappe.db.get_value(
			"Belgian VAT Tax Template Link",
			{
				"company": TEST_COMPANY,
				"tax_definition": definition,
				"tax_template_doctype": "Purchase Taxes and Charges Template",
			},
			"tax_template",
		)
		self.assertIsNotNone(template_name, f"no materialised template for {definition}")

		template = frappe.get_doc("Purchase Taxes and Charges Template", template_name)
		due_rows = [row for row in template.taxes if row.add_deduct_tax == "Add"]
		self.assertEqual(len(due_rows), 1, f"expected one VAT-due row on {template_name}")
		return due_rows[0].account_head

	def test_domestic_template_posts_to_domestic_account(self):
		self.assertEqual(
			self._materialised_due_account("Reverse Charge Domestic"),
			f"{DOMESTIC_RC_DUE_ACCOUNT_NAME} - {self.abbr}",
		)

	def test_intra_eu_template_posts_to_shared_account(self):
		self.assertEqual(
			self._materialised_due_account("Intra-EU Acquisition"),
			f"{RC_DUE_ACCOUNT_NAME} - {self.abbr}",
		)


class TestGetTaxDefinitionForTemplate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_vat_settings()
		# Ensure templates + links exist.
		materialise_for_company(TEST_COMPANY)

	def test_resolves_known_template(self):
		from belgium_compliance.setup import get_tax_definition_for_template

		# Pick any existing link and round-trip it.
		link = frappe.db.get_value(
			"Belgian VAT Tax Template Link",
			{"company": TEST_COMPANY},
			["tax_template_doctype", "tax_template", "tax_definition"],
			as_dict=True,
		)
		self.assertIsNotNone(link, "no Belgian VAT Tax Template Link in test site")

		definition = get_tax_definition_for_template(link.tax_template_doctype, link.tax_template)
		self.assertEqual(definition, link.tax_definition)

	def test_returns_none_for_unknown_template(self):
		from belgium_compliance.setup import get_tax_definition_for_template

		self.assertIsNone(
			get_tax_definition_for_template("Sales Taxes and Charges Template", "non-existent-XYZ")
		)

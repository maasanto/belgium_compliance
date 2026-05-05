# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Test fixtures shared across belgium_compliance test cases.

Bootstraps a minimal Belgian Company on the test site (along with VAT
account heads and Belgian VAT Settings) so the tests don't have to
duplicate setup. All operations are idempotent — safe to call from
any setUp / setUpClass.
"""

import frappe

TEST_COMPANY = "Test BE Co"
TEST_ABBR = "TBC"
TEST_TAX_ID = "BE0123456789"

# Account heads we need for materialise + declaration tests.
OUTPUT_VAT_ACCOUNT_NAME = "BE Output VAT Test"
INPUT_VAT_ACCOUNT_NAME = "BE Input VAT Test"
RC_DUE_ACCOUNT_NAME = "BE RC VAT Due Test"
RC_DEDUCTIBLE_ACCOUNT_NAME = "BE RC VAT Deductible Test"


def ensure_belgian_company() -> str:
	"""Create the test Belgian company once. Returns the company name."""
	if frappe.db.exists("Company", TEST_COMPANY):
		return TEST_COMPANY

	_ensure_erpnext_prereqs()

	company = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": TEST_COMPANY,
			"abbr": TEST_ABBR,
			"default_currency": "EUR",
			"country": "Belgium",
			"tax_id": TEST_TAX_ID,
		}
	)
	company.flags.ignore_permissions = True
	company.insert()
	_ensure_company_address(company.name)
	return company.name


def _ensure_erpnext_prereqs() -> None:
	"""Pre-create ERPNext masters that the Company controller's on_update step
	links to. On a freshly installed test site without the setup wizard,
	these aren't loaded automatically and Company.insert() fails with
	LinkValidationError on Warehouse Type: Transit."""
	for warehouse_type in ("Transit",):
		if not frappe.db.exists("Warehouse Type", warehouse_type):
			doc = frappe.get_doc({"doctype": "Warehouse Type", "name": warehouse_type})
			doc.flags.ignore_permissions = True
			doc.insert()


def ensure_vat_settings() -> str:
	"""Bootstrap accounts + Belgian VAT Settings for the test company.

	Idempotent. Returns the company name so callers can chain.
	"""
	company = ensure_belgian_company()
	output_vat = _ensure_account(OUTPUT_VAT_ACCOUNT_NAME, "Tax", "Liability", company)
	input_vat = _ensure_account(INPUT_VAT_ACCOUNT_NAME, "Tax", "Asset", company)
	rc_due = _ensure_account(RC_DUE_ACCOUNT_NAME, "Tax", "Liability", company)
	rc_ded = _ensure_account(RC_DEDUCTIBLE_ACCOUNT_NAME, "Tax", "Asset", company)

	if not frappe.db.exists("Belgian VAT Settings", company):
		settings = frappe.get_doc(
			{
				"doctype": "Belgian VAT Settings",
				"company": company,
				"output_vat_account": output_vat,
				"input_vat_deductible_account": input_vat,
				"reverse_charge_vat_due_account": rc_due,
				"reverse_charge_vat_deductible_account": rc_ded,
			}
		)
		settings.flags.ignore_permissions = True
		settings.insert()

	return company


def _ensure_account(account_name: str, account_type: str, root_type: str, company: str) -> str:
	abbr = frappe.db.get_value("Company", company, "abbr")
	full_name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", full_name):
		return full_name

	# Place under the company's standard root for that root_type.
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type, "parent_account": ""},
		"name",
	) or frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type},
		"name",
	)
	if not parent:
		# Fallback: create a stub group under any group of that root_type.
		parent = frappe.db.get_value(
			"Account",
			{"company": company, "root_type": root_type},
			"name",
		)

	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"account_type": account_type,
			"root_type": root_type,
			"parent_account": parent,
			"company": company,
			"is_group": 0,
		}
	)
	account.flags.ignore_permissions = True
	account.insert()
	return account.name


def _ensure_company_address(company: str) -> None:
	"""INTERVAT XML needs Street/PostCode/City — give the test company a real
	primary address so build_xml doesn't fall back to placeholder values."""
	existing = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
		"parent",
	)
	if existing:
		return

	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": company,
			"address_type": "Billing",
			"address_line1": "Rue de la Loi 1",
			"city": "Bruxelles",
			"pincode": "1000",
			"country": "Belgium",
			"is_primary_address": 1,
			"links": [{"link_doctype": "Company", "link_name": company}],
		}
	)
	address.flags.ignore_permissions = True
	address.insert()

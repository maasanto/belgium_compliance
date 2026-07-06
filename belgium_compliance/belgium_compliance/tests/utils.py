# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Test fixtures shared across belgium_compliance test cases.

Bootstraps a minimal Belgian Company on the test site (along with VAT
account heads and Belgian VAT Settings) so the tests don't have to
duplicate setup. All operations are idempotent — safe to call from
any setUp / setUpClass.
"""

from datetime import date

import frappe

TEST_COMPANY = "Test BE Co"
TEST_ABBR = "TBC"
TEST_TAX_ID = "BE0123456789"

# Account heads we need for materialise + declaration tests.
OUTPUT_VAT_ACCOUNT_NAME = "BE Output VAT Test"
INPUT_VAT_ACCOUNT_NAME = "BE Input VAT Test"
RC_DUE_ACCOUNT_NAME = "BE RC VAT Due Test"
DOMESTIC_RC_DUE_ACCOUNT_NAME = "BE Domestic RC VAT Due Test"
RC_DEDUCTIBLE_ACCOUNT_NAME = "BE RC VAT Deductible Test"


def ensure_belgian_company() -> str:
	"""Create the test Belgian company once. Returns the company name."""
	_ensure_fixtures_loaded()

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


def _ensure_fixtures_loaded() -> None:
	"""Make sure the shipped catalogue fixtures are present.

	`bench install-app` is supposed to run our `after_install` hook (which
	loads them), but on some CI flows the hook may have been bypassed.
	Always force-load — `load_fixtures` itself skips records that already
	exist, so this is idempotent.
	"""
	from belgium_compliance.install import load_fixtures

	load_fixtures()
	count = frappe.db.count("Belgian VAT Tax Definition")
	if count == 0:
		raise RuntimeError(f"Fixtures failed to load — Belgian VAT Tax Definition count = {count}")


def _ensure_erpnext_prereqs() -> None:
	"""Pre-create ERPNext masters that Company / Address controllers' on_update
	steps depend on. On a freshly installed test site without the setup wizard,
	these aren't loaded automatically and the inserts fail with
	LinkValidationError or "No default Address Template found"."""
	for warehouse_type in ("Transit",):
		if not frappe.db.exists("Warehouse Type", warehouse_type):
			doc = frappe.get_doc({"doctype": "Warehouse Type", "name": warehouse_type})
			doc.flags.ignore_permissions = True
			doc.insert()

	if not frappe.db.exists("Address Template", {"is_default": 1}):
		doc = frappe.get_doc(
			{
				"doctype": "Address Template",
				"country": "Belgium",
				"is_default": 1,
				"template": "{{ address_line1 }}<br>{{ city }} {{ pincode }}<br>{{ country }}",
			}
		)
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
	domestic_rc_due = _ensure_account(DOMESTIC_RC_DUE_ACCOUNT_NAME, "Tax", "Liability", company)
	rc_ded = _ensure_account(RC_DEDUCTIBLE_ACCOUNT_NAME, "Tax", "Asset", company)

	if not frappe.db.exists("Belgian VAT Settings", company):
		settings = frappe.get_doc(
			{
				"doctype": "Belgian VAT Settings",
				"company": company,
				"output_vat_account": output_vat,
				"input_vat_deductible_account": input_vat,
				"reverse_charge_vat_due_account": rc_due,
				"domestic_reverse_charge_vat_due_account": domestic_rc_due,
				"reverse_charge_vat_deductible_account": rc_ded,
			}
		)
		settings.flags.ignore_permissions = True
		settings.insert()
	elif not frappe.db.get_value("Belgian VAT Settings", company, "domestic_reverse_charge_vat_due_account"):
		# Backfill for a test site that created the settings before this field existed.
		frappe.db.set_value(
			"Belgian VAT Settings", company, "domestic_reverse_charge_vat_due_account", domestic_rc_due
		)

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


# ---------------------------------------------------------------------------
# Posting-document scaffolding — masters, invoice builders and declaration
# helpers shared by the declaration test files. All idempotent; builders that
# submit documents leave cleanup registration to the caller because submits
# commit and survive the per-test transaction rollback.
# ---------------------------------------------------------------------------


def ensure_fiscal_year(year: int) -> None:
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


def ensure_uom() -> str:
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


def ensure_selling_price_list() -> str:
	return _ensure_price_list("selling", "BE Test Selling")


def ensure_buying_price_list() -> str:
	return _ensure_price_list("buying", "BE Test Buying")


def _ensure_price_list(direction_flag: str, fallback_name: str) -> str:
	"""Return an enabled Price List for the given direction, creating one if the
	site ships none (a bare install without the setup wizard has no default)."""
	existing = frappe.db.get_value("Price List", {direction_flag: 1, "enabled": 1}, "name")
	if existing:
		return existing
	if not frappe.db.exists("Price List", fallback_name):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": fallback_name,
				direction_flag: 1,
				"enabled": 1,
				"currency": "EUR",
			}
		).insert(ignore_permissions=True)
	return fallback_name


def ensure_leaf(doctype: str, name_field: str, parent_field: str) -> str:
	"""Return a leaf node of a nested-set master (Item Group / Customer Group /
	Territory / Supplier Group), creating one under the root if the site ships
	none — a bare CI install has the tree roots but not always the child leaves
	these tests need."""
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


def ensure_customer() -> str:
	name = "BE Test Customer"
	if frappe.db.exists("Customer", name):
		return name
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_group": ensure_leaf("Customer Group", "customer_group_name", "parent_customer_group"),
			"territory": ensure_leaf("Territory", "territory_name", "parent_territory"),
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert()
	return customer.name


def ensure_supplier() -> str:
	name = "BE Test Supplier"
	if frappe.db.exists("Supplier", name):
		return name
	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": name,
			"supplier_group": ensure_leaf("Supplier Group", "supplier_group_name", "parent_supplier_group"),
		}
	)
	supplier.flags.ignore_permissions = True
	supplier.insert()
	return supplier.name


def ensure_service_item(code: str = "BE Test Service") -> str:
	if frappe.db.exists("Item", code):
		return code
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_group": ensure_leaf("Item Group", "item_group_name", "parent_item_group"),
			"stock_uom": ensure_uom(),
			"is_stock_item": 0,
			"is_sales_item": 1,
			"is_purchase_item": 1,
		}
	)
	item.flags.ignore_permissions = True
	item.insert()
	return item.name


def ensure_bank_account() -> str:
	"""A non-VAT bank account, used to balance test Journal Entries without
	polluting the VAT boxes."""
	return _ensure_account("BE Test Bank", "Bank", "Asset", TEST_COMPANY)


def ensure_income_account() -> str:
	return _ensure_account("BE Test Revenue", "", "Income", TEST_COMPANY)


def ensure_expense_account() -> str:
	return _ensure_account("BE Test Expense", "", "Expense", TEST_COMPANY)


def template_for_definition(definition: str, template_doctype: str) -> str:
	"""The materialised tax template linked to a fixture Tax Definition."""
	template = frappe.db.get_value(
		"Belgian VAT Tax Template Link",
		{
			"company": TEST_COMPANY,
			"tax_definition": definition,
			"tax_template_doctype": template_doctype,
		},
		"tax_template",
	)
	assert template, f"no {template_doctype} materialised for {definition}"
	return template


def make_sales_invoice(items: list[dict], taxes_template: str, posting_date, is_return: bool = False):
	"""Insert + submit a Sales Invoice on the test company.

	`items` rows carry qty/rate. Currency, pricing and the template's tax rows
	are pinned explicitly so a bare site (no default price list / exchange
	rate) needs no master lookups, and no round-off account (disabled) is
	required. Caller registers cleanup (see destroy_submittable)."""
	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": TEST_COMPANY,
			"customer": ensure_customer(),
			"is_return": 1 if is_return else 0,
			"set_posting_time": 1,
			"posting_date": posting_date,
			"due_date": posting_date,
			"currency": "EUR",
			"conversion_rate": 1.0,
			"selling_price_list": ensure_selling_price_list(),
			"price_list_currency": "EUR",
			"plc_conversion_rate": 1.0,
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"update_stock": 0,
			"taxes_and_charges": taxes_template,
			"items": _invoice_item_rows(items, {"income_account": ensure_income_account()}),
		}
	)
	_append_template_taxes(invoice, "Sales Taxes and Charges Template", taxes_template)
	return _submit_invoice(invoice)


def make_purchase_invoice(items: list[dict], taxes_template: str, posting_date, is_return: bool = False):
	"""Insert + submit a Purchase Invoice on the test company. Mirrors
	make_sales_invoice — see there for the pinned-master rationale."""
	invoice = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": TEST_COMPANY,
			"supplier": ensure_supplier(),
			"is_return": 1 if is_return else 0,
			"set_posting_time": 1,
			"posting_date": posting_date,
			"due_date": posting_date,
			"currency": "EUR",
			"conversion_rate": 1.0,
			"buying_price_list": ensure_buying_price_list(),
			"price_list_currency": "EUR",
			"plc_conversion_rate": 1.0,
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"update_stock": 0,
			"taxes_and_charges": taxes_template,
			"items": _invoice_item_rows(items, {"expense_account": ensure_expense_account()}),
		}
	)
	_append_template_taxes(invoice, "Purchase Taxes and Charges Template", taxes_template)
	return _submit_invoice(invoice)


def _invoice_item_rows(items: list[dict], account_fields: dict) -> list[dict]:
	default_item = ensure_service_item()
	cost_center = frappe.db.get_value("Company", TEST_COMPANY, "cost_center")
	return [
		{
			"item_code": line.get("item_code") or default_item,
			"qty": line["qty"],
			"rate": line["rate"],
			"price_list_rate": line["rate"],
			"cost_center": cost_center,
			**account_fields,
		}
		for line in items
	]


def _append_template_taxes(invoice, template_doctype: str, template_name: str) -> None:
	"""Pull the template's tax rows explicitly so the VAT posts regardless of
	whether server-side auto-population runs in the test harness."""
	template = frappe.get_doc(template_doctype, template_name)
	for row in template.taxes:
		tax_row = {
			"charge_type": row.charge_type,
			"account_head": row.account_head,
			"rate": row.rate,
			"description": row.description,
			"category": row.get("category"),
		}
		if row.get("add_deduct_tax"):
			tax_row["add_deduct_tax"] = row.add_deduct_tax
		invoice.append("taxes", tax_row)


def _submit_invoice(invoice):
	invoice.flags.ignore_permissions = True
	invoice.insert()
	invoice.submit()
	return invoice


def make_declaration(period_year: int, period_quarter: int):
	"""Insert a fresh quarterly declaration for the period, deleting any
	leftover from a prior aborted run first. Caller registers cleanup (see
	destroy_declaration)."""
	abbr = frappe.db.get_value("Company", TEST_COMPANY, "abbr")
	name = f"TVA-BE-{abbr}-{period_year}-Q{period_quarter}"
	if frappe.db.exists("Belgian VAT Declaration", name):
		frappe.delete_doc("Belgian VAT Declaration", name, force=True, delete_permanently=True)
	declaration = frappe.get_doc(
		{
			"doctype": "Belgian VAT Declaration",
			"company": TEST_COMPANY,
			"period_type": "Quarterly",
			"period_year": period_year,
			"period_month_or_quarter": period_quarter,
			"status": "Draft",
		}
	)
	declaration.flags.ignore_permissions = True
	declaration.insert()
	return declaration


def destroy_submittable(doctype: str, name: str) -> None:
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


def destroy_declaration(name: str) -> None:
	if frappe.db.exists("Belgian VAT Declaration", name):
		frappe.delete_doc("Belgian VAT Declaration", name, force=True, delete_permanently=True)

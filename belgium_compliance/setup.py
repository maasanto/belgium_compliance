# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Materialise Belgian VAT Tax Definitions into Sales/Purchase Taxes and
Charges Templates for a given company, and remember the link so the
declaration's compute() step can recover the Tax Definition for each invoice
tax row at posting time.
"""

import frappe
from frappe import _
from frappe.utils import flt

REVERSE_CHARGE_TYPES = {
	"Reverse Charge Domestic",
	"Reverse Charge EU",
	"Intra-EU Acquisition",
	"Import",
}


@frappe.whitelist()
def materialise_for_company(company: str, dry_run: int = 0) -> dict:
	"""Create one Sales/Purchase Taxes and Charges Template per Belgian VAT
	Tax Definition for the given company and link them.

	Idempotent — re-running on a company that already has links will skip
	the existing ones and only create the missing templates.

	Returns a summary dict with created / skipped / errors.
	"""
	dry_run = bool(int(dry_run))
	settings = _get_settings(company)

	created = []
	skipped = []
	errors = []

	for definition_name in frappe.get_all("Belgian VAT Tax Definition", pluck="name"):
		definition = frappe.get_cached_doc("Belgian VAT Tax Definition", definition_name)
		try:
			result = _materialise_one(definition, company, settings, dry_run=dry_run)
			if result == "created":
				created.append(definition_name)
			elif result == "skipped":
				skipped.append(definition_name)
		except Exception as exc:
			errors.append({"definition": definition_name, "error": str(exc)})

	if not dry_run:
		frappe.db.commit()

	return {
		"company": company,
		"dry_run": dry_run,
		"created": len(created),
		"skipped": len(skipped),
		"errors": len(errors),
		"created_codes": created,
		"error_details": errors,
	}


def _get_settings(company: str):
	if not frappe.db.exists("Belgian VAT Settings", company):
		frappe.throw(
			_(
				"Belgian VAT Settings not configured for company {0}. "
				"Create the Belgian VAT Settings document first."
			).format(company)
		)
	return frappe.get_doc("Belgian VAT Settings", company)


def _materialise_one(definition, company, settings, dry_run):
	# Skip if a link already exists for (company, definition).
	if frappe.db.exists(
		"Belgian VAT Tax Template Link",
		{"company": company, "tax_definition": definition.name},
	):
		return "skipped"

	parent_doctype = (
		"Sales Taxes and Charges Template"
		if definition.direction == "Sales"
		else "Purchase Taxes and Charges Template"
	)

	if dry_run:
		return "created"

	# 1. Parent invoice template — used when the whole invoice is single-rate.
	parent_template = _create_parent_template(definition, company, settings, parent_doctype)
	_create_link(company, definition.name, parent_doctype, parent_template.name)

	# 2. Item Tax Template — used to override per line on mixed-rate invoices.
	item_template = _create_item_tax_template(definition, company, settings)
	_create_link(company, definition.name, "Item Tax Template", item_template.name)

	return "created"


def _create_parent_template(definition, company, settings, parent_doctype):
	template_title = f"BE — {definition.title}"
	template_name = _unique_template_name(parent_doctype, template_title, company)

	template = frappe.new_doc(parent_doctype)
	template.title = template_title
	template.company = company
	template.update({"name": template_name})

	for row in _build_parent_tax_rows(definition, settings):
		template.append("taxes", row)

	template.flags.ignore_permissions = True
	template.insert()
	return template


def _create_item_tax_template(definition, company, settings):
	template_title = f"BE — {definition.title}"
	template_name = _unique_template_name("Item Tax Template", template_title, company)

	template = frappe.new_doc("Item Tax Template")
	template.title = template_title
	template.company = company
	template.update({"name": template_name})

	for row in _build_item_tax_rows(definition, settings):
		template.append("taxes", row)

	template.flags.ignore_permissions = True
	template.insert()
	return template


def _create_link(company, tax_definition, tax_template_doctype, tax_template):
	link = frappe.new_doc("Belgian VAT Tax Template Link")
	link.company = company
	link.tax_definition = tax_definition
	link.tax_template_doctype = tax_template_doctype
	link.tax_template = tax_template
	link.flags.ignore_permissions = True
	link.insert()


def _unique_template_name(template_doctype: str, base_title: str, company: str) -> str:
	"""Match the autoname rules used by ERPNext for tax templates so we don't
	collide with existing templates from other localisations."""
	from frappe.utils import cstr

	abbr = frappe.db.get_value("Company", company, "abbr")
	candidate = f"{base_title} - {abbr}" if abbr else base_title
	if not frappe.db.exists(template_doctype, candidate):
		return candidate
	# Suffix until unique.
	for i in range(2, 100):
		alt = f"{candidate} ({i})"
		if not frappe.db.exists(template_doctype, alt):
			return alt
	frappe.throw(_("Cannot find a free name for template {0}").format(base_title))


def _accounts_for(definition, settings) -> list[tuple[str, str]]:
	"""Resolve which configured accounts this definition writes to.

	Returns a list of (role, account_name) tuples where role is either
	"output", "input", "rc_due", or "rc_deductible" — used by both the
	parent template builder (which needs Add/Deduct semantics) and the
	Item Tax Template builder (which needs flat (account, rate) pairs).
	"""
	if definition.direction == "Sales":
		return [("output", settings.output_vat_account)]

	if definition.tax_type in REVERSE_CHARGE_TYPES:
		# Belgian PCMN splits reverse-charge VAT due: domestic cocontractant
		# (art. 20 AR n°1) posts to 4510 (boxes 56/87) while intra-EU
		# acquisitions/services and imports post to 4513 (boxes 55/86).
		due_account = settings.reverse_charge_vat_due_account
		if (
			definition.tax_type == "Reverse Charge Domestic"
			and settings.domestic_reverse_charge_vat_due_account
		):
			due_account = settings.domestic_reverse_charge_vat_due_account
		return [
			("rc_due", due_account),
			("rc_deductible", settings.reverse_charge_vat_deductible_account),
		]

	is_investment = "investment" in (definition.code or "").lower()
	account = (
		settings.input_vat_investment_account
		if is_investment and settings.input_vat_investment_account
		else settings.input_vat_deductible_account
	)
	return [("input", account)]


def _build_parent_tax_rows(definition, settings) -> list[dict]:
	"""Parent Sales/Purchase Taxes and Charges Template rows.

	Standard direction: one row at the definition's rate.
	Reverse-charge purchases: two rows — Add on the due account and Deduct
	on the deductible account at the same rate, so the supplier net stays
	intact while both autoliquidation legs hit the GL.
	"""
	rate = flt(definition.rate)
	rows = []
	for role, account in _accounts_for(definition, settings):
		if role == "rc_deductible":
			rows.append(
				_parent_row(
					account=account,
					rate=rate,
					description=f"{definition.title} (TVA déductible)",
					add_deduct_tax="Deduct",
				)
			)
		elif role == "rc_due":
			rows.append(
				_parent_row(
					account=account,
					rate=rate,
					description=f"{definition.title} (TVA due)",
					add_deduct_tax="Add",
				)
			)
		else:
			rows.append(
				_parent_row(
					account=account,
					rate=rate,
					description=definition.title,
				)
			)
	return rows


def _build_item_tax_rows(definition, settings) -> list[dict]:
	"""Item Tax Template rows — one positive rate per account that the parent
	template uses. ERPNext re-applies the parent's Add/Deduct semantics, so
	we always emit positive rates here regardless of role.
	"""
	rate = flt(definition.rate)
	return [{"tax_type": account, "tax_rate": rate} for _role, account in _accounts_for(definition, settings)]


def _parent_row(
	account: str,
	rate: float,
	description: str,
	add_deduct_tax: str | None = None,
) -> dict:
	row = {
		"charge_type": "On Net Total",
		"account_head": account,
		"rate": rate,
		"description": description,
		"category": "Total",
	}
	if add_deduct_tax:
		row["add_deduct_tax"] = add_deduct_tax
	return row


@frappe.whitelist()
def get_tax_definition_for_template(template_doctype: str, template_name: str) -> str | None:
	"""Resolve the Belgian VAT Tax Definition behind a given Sales/Purchase
	Taxes and Charges Template, used by the declaration compute step.
	"""
	return frappe.db.get_value(
		"Belgian VAT Tax Template Link",
		{"tax_template_doctype": template_doctype, "tax_template": template_name},
		"tax_definition",
	)

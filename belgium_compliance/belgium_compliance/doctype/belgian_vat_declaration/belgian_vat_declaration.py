# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

from calendar import monthrange
from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from belgium_compliance import intervat_xml

# All grid codes managed by the declaration. Keep in sync with the Belgian VAT Grid fixtures.
GRID_CODES = [
	"00",
	"01",
	"02",
	"03",
	"44",
	"45",
	"46L",
	"46T",
	"47",
	"48",
	"49",
	"81",
	"82",
	"83",
	"84",
	"85",
	"86",
	"87",
	"88",
	"54",
	"55",
	"56",
	"57",
	"61",
	"63",
	"59",
	"62",
	"64",
	"71",
	"72",
]

# Codes whose value is computed from other grids (cadre VI). Excluded from
# direct extraction; their value is derived in _apply_total_formulas.
TOTAL_GRID_CODES = {"71", "72"}


def grid_to_fieldname(code: str) -> str:
	"""Map an INTERVAT grid code (e.g. '46L') to its declaration fieldname (g_46l)."""
	return f"g_{code.lower()}"


class BelgianVATDeclaration(Document):
	def autoname(self):
		# TVA-BE-{abbr}-{year}-{Q|M}{idx} — company abbr keeps names unique
		# across companies; without it, a quarterly declaration of the same
		# period on a different company would collide on the primary key.
		prefix = "Q" if self.period_type == "Quarterly" else "M"
		idx = int(self.period_month_or_quarter)
		idx_part = f"{prefix}{idx}" if self.period_type == "Quarterly" else f"{prefix}{idx:02d}"
		abbr = frappe.db.get_value("Company", self.company, "abbr") or self.company
		self.name = f"TVA-BE-{abbr}-{int(self.period_year)}-{idx_part}"

	def validate(self):
		self._validate_period()
		self._compute_period_dates()
		if self.status not in {"Draft", "Ready"} and self.docstatus == 0:
			frappe.throw(_("Status {0} is only allowed after submission.").format(self.status))

	def before_save(self):
		self._recompute_adjustment_totals()

	# ------------------------------------------------------------------
	# Period bookkeeping
	# ------------------------------------------------------------------

	def _validate_period(self):
		if self.period_type == "Monthly":
			if not 1 <= int(self.period_month_or_quarter) <= 12:
				frappe.throw(_("Month must be between 1 and 12 for monthly declarations."))
		elif self.period_type == "Quarterly":
			if not 1 <= int(self.period_month_or_quarter) <= 4:
				frappe.throw(_("Quarter must be between 1 and 4 for quarterly declarations."))
		else:
			frappe.throw(_("Period type must be Monthly or Quarterly."))

	def _compute_period_dates(self):
		year = int(self.period_year)
		idx = int(self.period_month_or_quarter)
		if self.period_type == "Monthly":
			self.start_date = date(year, idx, 1)
			self.end_date = date(year, idx, monthrange(year, idx)[1])
		else:
			start_month = (idx - 1) * 3 + 1
			end_month = start_month + 2
			self.start_date = date(year, start_month, 1)
			self.end_date = date(year, end_month, monthrange(year, end_month)[1])

	def _recompute_adjustment_totals(self):
		# Adjustments are summed into the grid fields when computing; nothing to do
		# here yet, but the hook is in place for future per-row sanity checks.
		for adj in self.adjustments or []:
			if adj.grid in TOTAL_GRID_CODES:
				frappe.throw(
					_("Manual adjustment on grid {0} is not allowed — totals are derived.").format(adj.grid)
				)

	# ------------------------------------------------------------------
	# Compute — public entry point
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def compute(self):
		"""Recompute the 31 grid totals from GL Entries posted in the period.

		Strategy (Phase 2 skeleton):
		  1. Reset grid fields and computed_lines.
		  2. Pull GL Entries for the company/period bound to taxes that the
		     materialised Sales/Purchase Taxes and Charges Templates produced
		     from a Belgian VAT Tax Definition.
		  3. For each entry, fan out into N grid contributions according to
		     the Tax Definition's grid_tags (matched on amount_type ×
		     document_type).
		  4. Apply manual adjustments.
		  5. Apply total formulas (g_71, g_72).

		For now steps 2-3 are stubbed: we leave the grid fields at zero unless
		manual adjustments are present. The materialisation hook that links a
		template to a Tax Definition is Phase 1 follow-up (after the Odoo
		bulk port lands).
		"""
		self._reset_grids()
		self._extract_from_gl_entries()
		self._apply_adjustments()
		self._apply_total_formulas()
		self.status = "Ready"

	# ------------------------------------------------------------------
	# Compute — internals
	# ------------------------------------------------------------------

	def _reset_grids(self):
		for code in GRID_CODES:
			self.set(grid_to_fieldname(code), 0)
		self.computed_lines = []

	def _extract_from_gl_entries(self):
		"""Walk Sales/Purchase Invoice item lines posted in the period.

		Each item resolves to a Belgian VAT Tax Definition, in priority order:
		  1. Its `item_tax_template` field, looked up in the Belgian VAT Tax
		     Template Link table — supports mixed-rate invoices.
		  2. Otherwise, fall back to the parent invoice's `taxes_and_charges`
		     template.

		Items that resolve to no definition are skipped silently — they
		belong to non-Belgian-VAT flows (e.g. another country's templates
		on a multi-company site).
		"""
		for invoice_doctype, item_doctype, parent_template_doctype in (
			("Sales Invoice", "Sales Invoice Item", "Sales Taxes and Charges Template"),
			("Purchase Invoice", "Purchase Invoice Item", "Purchase Taxes and Charges Template"),
		):
			self._extract_from_invoice_set(
				invoice_doctype=invoice_doctype,
				item_doctype=item_doctype,
				parent_template_doctype=parent_template_doctype,
			)

	def _extract_from_invoice_set(self, invoice_doctype, item_doctype, parent_template_doctype):
		# Map each linked template (Item or parent) to its Tax Definition.
		links = frappe.get_all(
			"Belgian VAT Tax Template Link",
			filters={
				"company": self.company,
				"tax_template_doctype": ["in", [parent_template_doctype, "Item Tax Template"]],
			},
			fields=["tax_template", "tax_definition", "tax_template_doctype"],
		)
		if not links:
			return

		item_template_map = {
			l.tax_template: l.tax_definition for l in links if l.tax_template_doctype == "Item Tax Template"
		}
		parent_template_map = {
			l.tax_template: l.tax_definition
			for l in links
			if l.tax_template_doctype == parent_template_doctype
		}

		# Pull invoices in period that point at one of our parent templates,
		# OR whose items reference one of our Item Tax Templates. To keep the
		# query simple we fetch all submitted invoices in period and filter
		# in Python — typical period sizes (a quarter) are small.
		invoices = frappe.get_all(
			invoice_doctype,
			filters={
				"company": self.company,
				"docstatus": 1,
				"posting_date": ["between", [self.start_date, self.end_date]],
			},
			fields=["name", "taxes_and_charges", "is_return", "posting_date"],
		)
		if not invoices:
			return

		for inv in invoices:
			parent_definition = parent_template_map.get(inv.taxes_and_charges)
			document_type = "Refund" if inv.is_return else "Invoice"

			items = frappe.get_all(
				item_doctype,
				filters={"parent": inv.name},
				fields=["name", "item_tax_template", "base_net_amount"],
			)
			for item in items:
				definition_name = (
					item_template_map.get(item.item_tax_template)
					if item.item_tax_template
					else parent_definition
				)
				if not definition_name:
					continue

				definition = frappe.get_cached_doc("Belgian VAT Tax Definition", definition_name)
				base = abs(flt(item.base_net_amount))
				tax = base * flt(definition.rate) / 100.0

				for tag in definition.grid_tags:
					if tag.document_type != document_type:
						continue
					raw = base if tag.amount_type == "Base" else tax
					contribution = raw * flt(tag.sign)
					if not contribution:
						continue

					self.append(
						"computed_lines",
						{
							"grid": tag.grid,
							"amount_type": tag.amount_type,
							"tax_definition": definition_name,
							"voucher_type": invoice_doctype,
							"voucher_no": inv.name,
							"posting_date": inv.posting_date,
							"amount": raw,
							"sign": tag.sign,
							"contributing_amount": contribution,
						},
					)
					field = grid_to_fieldname(tag.grid)
					self.set(field, flt(self.get(field)) + contribution)

	def _apply_adjustments(self):
		for adj in self.adjustments or []:
			fieldname = grid_to_fieldname(adj.grid)
			current = flt(self.get(fieldname))
			self.set(fieldname, current + flt(adj.amount))

	def _apply_total_formulas(self):
		# Cadre VI — net balance.
		due = sum(flt(self.get(grid_to_fieldname(code))) for code in ("54", "55", "56", "57", "61", "63"))
		deductible = sum(flt(self.get(grid_to_fieldname(code))) for code in ("59", "62", "64"))
		net = due - deductible
		self.g_71 = max(net, 0)
		self.g_72 = max(-net, 0)

	# ------------------------------------------------------------------
	# INTERVAT XML generation
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def generate_intervat_xml(self) -> dict:
		"""Build, validate and attach the INTERVAT XML for this declaration.

		Returns a summary dict {"status", "validation", "file_url"}. The XML
		is attached as a File and the `generated_xml` field is updated so
		the user can download it from the form.
		"""
		if self.docstatus == 2:
			frappe.throw(_("Cannot generate XML on a cancelled declaration."))
		if self.status == "Draft":
			frappe.throw(_("Run Compute first — XML generation requires a Ready declaration."))

		xml_bytes = intervat_xml.build_xml(self)
		validation = intervat_xml.validate_xml(xml_bytes)

		filename = f"INTERVAT-{self.name}.xml"
		_replace_attached_xml(self, filename, xml_bytes)

		# Surface validation issues but don't block — the user may want to
		# inspect a not-yet-conformant XML during development.
		if validation["status"] == "invalid":
			frappe.msgprint(
				_("INTERVAT XML attached but failed XSD validation:")
				+ "<br>"
				+ "<br>".join(validation["errors"][:5]),
				title=_("XSD validation"),
				indicator="orange",
			)

		return {
			"status": "ok",
			"validation": validation,
			"file_url": self.generated_xml,
		}

	# ------------------------------------------------------------------
	# Submission lifecycle
	# ------------------------------------------------------------------

	def on_submit(self):
		if self.status not in {"Ready", "Filed", "Paid"}:
			frappe.throw(_("Compute and review the declaration before submitting."))
		# Closing journal entry creation is Phase 4. For now we just lock the doc.

	def on_cancel(self):
		# Allow cancellation only when not yet filed.
		if self.status in {"Filed", "Paid"}:
			frappe.throw(_("Cancel the closing journal entry first."))
		self.status = "Draft"

	def on_update_after_submit(self):
		# Allow status transitions Ready → Filed → Paid post-submit without
		# cancel/amend cycle. Frappe lets us write to a submitted doc only on
		# fields explicitly marked allow_on_submit; we keep `status`,
		# `intervat_reference`, `payment_entry` editable that way in Phase 3.
		return

	# ------------------------------------------------------------------
	# Helpers exposed for tests / scripts
	# ------------------------------------------------------------------

	def get_grid_value(self, grid_code: str) -> float:
		return flt(self.get(grid_to_fieldname(grid_code)))


def _replace_attached_xml(declaration, filename: str, content: bytes) -> None:
	"""Remove any prior INTERVAT XML attached to this declaration, then save
	the new one and update the `generated_xml` field with its file URL."""
	prior = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": declaration.doctype,
			"attached_to_name": declaration.name,
			"file_name": ["like", "INTERVAT-%.xml"],
		},
		pluck="name",
	)
	for name in prior:
		frappe.delete_doc("File", name, force=True, delete_permanently=True)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"attached_to_doctype": declaration.doctype,
			"attached_to_name": declaration.name,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert()

	declaration.db_set("generated_xml", file_doc.file_url, update_modified=False)

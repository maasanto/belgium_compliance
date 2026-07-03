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

# Tax boxes sourced from the *booked* VAT read straight off the ledger
# (GL Entries on the accounts configured in Belgian VAT Settings), routed by
# account + debit/credit sign — never recomputed as base × rate. This is the
# fix for the per-line rounding drift and makes JE-booked VAT visible.
#
# Only boxes whose account→box mapping is genuinely 1:1 belong here: every
# rate and sub-regime that lands in 54/64 posts to the single output VAT
# account, and everything deductible lands in 59/63 via the input/reverse-
# charge deductible accounts. The reverse-charge *due* boxes (55/56/57) are
# deliberately excluded — the intra-EU due account also carries import VAT
# (box 57), so account routing can't separate them; those stay definition-
# driven off the invoice lines until per-box accounts exist.
GL_TAX_GRIDS = frozenset({"54", "59", "63", "64"})


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
		"""Recompute the grid totals as a faithful read of the ledger.

		Each box family is filled from the source that carries the truth:

		  1. Reset grid fields and computed_lines.
		  2. Base boxes (00-03/44-49/81-88) and the reverse-charge *due* boxes
		     come from the Sales/Purchase Invoice lines, routed through the
		     Belgian VAT Tax Definition grid tags (they carry the goods /
		     services / investment classification that the GL can't).
		  3. Tax boxes 54/59/63/64 come from the *booked* VAT on the ledger —
		     GL Entries on the accounts configured in Belgian VAT Settings,
		     routed by account + debit/credit sign. No base × rate rounding,
		     and JE-booked VAT is included for free.
		  4. Journal-Entry bases flow in through the account→grid mapping on
		     Belgian VAT Settings.
		  5. Apply manual adjustments, then the total formulas (g_71, g_72).

		A cent-level cross-check between the recomputed base × rate and the
		booked VAT is surfaced as a warning so a mis-booked VAT account
		doesn't pass silently.
		"""
		self._reset_grids()
		self._extract_from_invoices()
		self._extract_tax_from_gl()
		self._extract_bases_from_journal_entries()
		# Cross-check the booked VAT against base × rate before manual
		# adjustments fold in — otherwise an adjustment on 54/59/63/64 would
		# masquerade as a booking discrepancy.
		self._warn_on_booked_vat_mismatch()
		self._apply_adjustments()
		self._apply_total_formulas()
		self.status = "Ready"
		# Persist the freshly-computed grids so that downstream consumers
		# (XML generation, reports) see them after a doc reload. Skip when
		# the doc is already submitted/cancelled — saving would error.
		if self.docstatus == 0 and not self.is_new():
			self.save(ignore_permissions=True)

	# ------------------------------------------------------------------
	# Compute — internals
	# ------------------------------------------------------------------

	def _reset_grids(self):
		for code in GRID_CODES:
			self.set(grid_to_fieldname(code), 0)
		self.computed_lines = []
		# base × rate recomputed per line, kept only to cross-check the booked
		# VAT read from the GL (see _warn_on_booked_vat_mismatch).
		self._recomputed_tax = {code: 0.0 for code in GL_TAX_GRIDS}

	def _extract_from_invoices(self):
		"""Walk Sales/Purchase Invoice item lines posted in the period.

		Each item resolves to a Belgian VAT Tax Definition, in priority order:
		  1. Its `item_tax_template` field, looked up in the Belgian VAT Tax
		     Template Link table — supports mixed-rate invoices.
		  2. Otherwise, fall back to the parent invoice's `taxes_and_charges`
		     template.

		Items that resolve to no definition are skipped silently — they
		belong to non-Belgian-VAT flows (e.g. another country's templates
		on a multi-company site).

		Only the base boxes and the reverse-charge *due* boxes are filled from
		here; the booked-VAT boxes (GL_TAX_GRIDS) are read from the ledger in
		_extract_tax_from_gl instead.
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

		# Fetch every item line for the period in one query, then group by
		# invoice — avoids a per-invoice round-trip over a full quarter.
		items_by_invoice: dict[str, list] = {}
		for item in frappe.get_all(
			item_doctype,
			filters={"parent": ["in", [inv.name for inv in invoices]]},
			fields=["parent", "name", "item_tax_template", "base_net_amount"],
		):
			items_by_invoice.setdefault(item.parent, []).append(item)

		for inv in invoices:
			parent_definition = parent_template_map.get(inv.taxes_and_charges)
			document_type = "Refund" if inv.is_return else "Invoice"

			for item in items_by_invoice.get(inv.name, []):
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

					# Booked-VAT boxes are read from the ledger, never from the
					# invoice line — guard on the grid so a stray Base tag on a
					# tax box can't double-count on top of the GL read. Keep the
					# recomputed tax only to cross-check that GL read.
					if tag.grid in GL_TAX_GRIDS:
						if tag.amount_type == "Tax":
							self._recomputed_tax[tag.grid] += contribution
						continue

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

	# ------------------------------------------------------------------
	# Booked VAT — read straight off the ledger
	# ------------------------------------------------------------------

	def _extract_tax_from_gl(self):
		"""Fill the booked-VAT boxes (GL_TAX_GRIDS) from GL Entries.

		Reads every non-cancelled GL Entry in the period on the VAT accounts
		configured in Belgian VAT Settings and routes it by account +
		debit/credit sign. This captures invoice- and JE-booked VAT alike and
		reflects the exact amount posted, so multi-rate invoices come out with
		no per-line rounding drift.
		"""
		settings = _get_vat_settings(self.company)
		if not settings:
			return

		routing = _gl_tax_account_routing(settings)
		if not routing:
			return

		for entry in self._fetch_gl_entries(list(routing)):
			credit_grid, debit_grid = routing[entry.account]
			for amount, grid in ((flt(entry.credit), credit_grid), (flt(entry.debit), debit_grid)):
				if not amount or not grid:
					continue
				self._add_ledger_line(grid, "Tax", entry, amount)

	def _extract_bases_from_journal_entries(self):
		"""Include VAT-relevant Journal Entry bases via the account→grid map.

		Belgian VAT Settings can map a P&L / base account to a base grid so
		that bases booked directly through a Journal Entry — periodic takings,
		corrections, accrued reverse-charge bases — reach the declaration
		without being restructured into invoices.

		Only Journal-Entry postings are read here: invoice-sourced postings on
		the same account already flow through the invoice lines, and the VAT
		itself already flows through _extract_tax_from_gl, so nothing is
		double-counted.
		"""
		settings = _get_vat_settings(self.company)
		if not settings:
			return

		mappings = {m.account: m for m in (settings.get("journal_mappings") or []) if m.account and m.grid}
		if not mappings:
			return

		for entry in self._fetch_gl_entries(list(mappings), {"voucher_type": "Journal Entry"}):
			mapping = mappings[entry.account]
			# Bases live on the natural balance of the account (credit for
			# income, debit for expense). The mapping's sign lets the user
			# pick which side is positive; default +1 suits income accounts.
			# Reversals net through the debit/credit sign — there is no separate
			# Invoice/Refund grid routing here (unlike the invoice line path).
			amount = (flt(entry.credit) - flt(entry.debit)) * (mapping.sign or 1)
			if not amount:
				continue
			self._add_ledger_line(mapping.grid, "Base", entry, amount)

	def _fetch_gl_entries(self, accounts: list[str], extra_filters: dict | None = None) -> list:
		"""Non-cancelled GL Entries for this period on the given accounts.
		Shared by the booked-VAT and JE-base readers so the field list and
		period/cancellation filters live in one place."""
		filters = {
			"company": self.company,
			"account": ["in", accounts],
			"posting_date": ["between", [self.start_date, self.end_date]],
			"is_cancelled": 0,
		}
		if extra_filters:
			filters.update(extra_filters)
		return frappe.get_all(
			"GL Entry",
			filters=filters,
			fields=["name", "account", "debit", "credit", "voucher_type", "voucher_no", "posting_date"],
		)

	def _add_ledger_line(self, grid: str, amount_type: str, entry, contribution: float):
		"""Append an audit line for a ledger-sourced contribution and fold it
		into the grid total. Used by both the booked-VAT and JE-base readers."""
		self.append(
			"computed_lines",
			{
				"grid": grid,
				"amount_type": amount_type,
				"voucher_type": entry.voucher_type,
				"voucher_no": entry.voucher_no,
				"posting_date": entry.posting_date,
				"account": entry.account,
				"amount": contribution,
				"sign": 1,
				"contributing_amount": contribution,
				"gl_entry": entry.name,
			},
		)
		field = grid_to_fieldname(grid)
		self.set(field, flt(self.get(field)) + contribution)

	def _warn_on_booked_vat_mismatch(self):
		"""Cross-check the booked VAT read from the GL against the base × rate
		recomputation. A gap beyond a cent per box usually means VAT was
		posted to an account outside Belgian VAT Settings (or to the wrong
		one) — surface it rather than let the declaration drift silently."""
		diffs = []
		for grid in sorted(GL_TAX_GRIDS):
			booked = flt(self.get(grid_to_fieldname(grid)))
			recomputed = flt(self._recomputed_tax.get(grid, 0.0))
			if abs(booked - recomputed) > 0.01:
				diffs.append((grid, booked, recomputed))
		if not diffs:
			return

		rows = "<br>".join(
			_("Box {0}: booked {1:.2f} vs base × rate {2:.2f} (Δ {3:.2f})").format(
				grid, booked, recomputed, booked - recomputed
			)
			for grid, booked, recomputed in diffs
		)
		frappe.msgprint(
			_("Booked VAT differs from the base × rate recomputation:")
			+ "<br>"
			+ rows
			+ "<br>"
			+ _("Check that all VAT is posted to the accounts in Belgian VAT Settings."),
			title=_("VAT consistency check"),
			indicator="orange",
		)

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


def _get_vat_settings(company: str):
	"""Return the company's Belgian VAT Settings, or None when the company has
	no Belgian VAT set-up (compute then simply skips the ledger readers)."""
	if not frappe.db.exists("Belgian VAT Settings", company):
		return None
	return frappe.get_cached_doc("Belgian VAT Settings", company)


def _gl_tax_account_routing(settings) -> dict[str, tuple[str, str]]:
	"""Map each configured VAT account to the (credit_grid, debit_grid) pair
	its booked movements land in.

	Output VAT: collected on a sale credits 54; reversed on a sales credit
	note it debits, landing in the regularisation box 64. Deductible VAT
	(regular input, investment and the reverse-charge deductible leg) is
	debited on a purchase into 59; reversed on a purchase credit note it
	credits, landing in the regularisation box 63.

	Accounts left empty in the settings are skipped. When two roles share an
	account they route to the same boxes, so collapsing them is harmless.
	"""
	routing: dict[str, tuple[str, str]] = {}

	def route(account, credit_grid, debit_grid):
		if account:
			routing.setdefault(account, (credit_grid, debit_grid))

	route(settings.output_vat_account, "54", "64")  # credit→54 due, debit→64 recover
	for deductible_account in (
		settings.input_vat_deductible_account,
		settings.input_vat_investment_account,
		settings.reverse_charge_vat_deductible_account,
	):
		route(deductible_account, "63", "59")  # credit→63 reversal, debit→59 normal

	return routing


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

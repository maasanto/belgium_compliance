# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

VAT_ACCOUNT_FIELDS = (
	"output_vat_account",
	"input_vat_deductible_account",
	"input_vat_investment_account",
	"reverse_charge_vat_due_account",
	"domestic_reverse_charge_vat_due_account",
	"reverse_charge_vat_deductible_account",
)


class BelgianVATSettings(Document):
	def validate(self):
		self._reject_vat_accounts_in_journal_mappings()

	def _reject_vat_accounts_in_journal_mappings(self):
		"""The declaration reads VAT off the accounts above directly from the
		ledger. Mapping one of them as a Journal-Entry *base* account would
		double-count it (once as base, once as tax), so refuse it up front."""
		vat_accounts = {self.get(field) for field in VAT_ACCOUNT_FIELDS if self.get(field)}
		for mapping in self.journal_mappings or []:
			if mapping.account in vat_accounts:
				frappe.throw(
					_(
						"Journal Entry Base Mapping row {0}: account {1} is a VAT account. "
						"VAT is already read from the ledger — map only base / P&L accounts here."
					).format(mapping.idx, mapping.account)
				)

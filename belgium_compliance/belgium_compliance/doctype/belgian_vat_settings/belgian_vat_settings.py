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
		self._reject_vat_account_as_settlement_account()

	def _reject_vat_account_as_settlement_account(self):
		"""The settlement account marks vouchers to *exclude* from the ledger
		read. Pointing it at one of the VAT accounts would make every VAT
		posting self-exclude and the declaration read nothing."""
		if not self.vat_settlement_account:
			return
		for field in VAT_ACCOUNT_FIELDS:
			if self.get(field) == self.vat_settlement_account:
				frappe.throw(
					_(
						"VAT Settlement Account {0} is already configured as {1}. "
						"Use the dedicated current account (PCMN 4519), not a VAT account."
					).format(self.vat_settlement_account, _(self.meta.get_label(field)))
				)

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

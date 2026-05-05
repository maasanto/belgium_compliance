# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Bootstrap step run after `bench install-app belgium_compliance`.

Loads the shipped fixtures (Belgian VAT Grid catalogue + Tax Definitions)
into the site so the app is usable out of the box. Idempotent — re-running
on a site that already has the records is a no-op (the import skips
existing names).
"""

import json
import os

import frappe

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURES_TO_LOAD = (
	"belgian_vat_grid.json",
	"belgian_vat_tax_definition.json",
)


def after_install():
	"""Load the catalogue fixtures shipped with the app."""
	load_fixtures()


def load_fixtures():
	"""Read each shipped fixture JSON and insert its records.

	Skips records that already exist (matched on name) so this is safe to
	call repeatedly — useful from CI / setup-from-scratch scripts.
	"""
	for filename in FIXTURES_TO_LOAD:
		path = os.path.join(FIXTURE_DIR, filename)
		if not os.path.exists(path):
			continue
		with open(path, encoding="utf-8") as fh:
			records = json.load(fh)
		for record in records:
			doctype = record["doctype"]
			name = record.get("name") or record.get("code")
			if name and frappe.db.exists(doctype, name):
				continue
			doc = frappe.get_doc(record)
			doc.flags.ignore_permissions = True
			doc.insert(ignore_if_duplicate=True)
	frappe.db.commit()

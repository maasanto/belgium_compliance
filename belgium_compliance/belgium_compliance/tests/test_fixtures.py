# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Integrity tests for the shipped fixtures.

These run without touching the DB — they only parse the JSON files and
check structural invariants (no orphan grid references, valid directions,
valid amount types, etc.). A failure here means a fixture file has drifted
into an inconsistent state.
"""

import json
import os
import unittest

FIXTURES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "fixtures"))

VALID_DIRECTIONS = {"Sales", "Purchase"}
VALID_AMOUNT_TYPES = {"Base", "Tax"}
VALID_DOCUMENT_TYPES = {"Invoice", "Refund"}
VALID_SIGNS = {-1, 0, 1}


def _load(name: str):
	with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as fh:
		return json.load(fh)


class TestGridFixture(unittest.TestCase):
	def setUp(self):
		self.grids = _load("belgian_vat_grid.json")

	def test_loads_as_list(self):
		self.assertIsInstance(self.grids, list)
		self.assertGreater(len(self.grids), 0)

	def test_every_grid_has_required_fields(self):
		for grid in self.grids:
			self.assertEqual(grid["doctype"], "Belgian VAT Grid")
			for field in ("code", "label", "cadre"):
				self.assertIn(field, grid, f"missing {field} in grid {grid.get('code')}")

	def test_codes_are_unique(self):
		codes = [g["code"] for g in self.grids]
		self.assertEqual(len(codes), len(set(codes)), "duplicate grid codes")

	def test_known_grids_present(self):
		# Sanity: a few grids that absolutely must ship.
		codes = {g["code"] for g in self.grids}
		for required in ("00", "01", "02", "03", "44", "45", "46L", "46T", "54", "55", "59", "71", "72"):
			self.assertIn(required, codes, f"grid {required} missing from fixture")


class TestTaxDefinitionFixture(unittest.TestCase):
	def setUp(self):
		self.definitions = _load("belgian_vat_tax_definition.json")
		self.grid_codes = {g["code"] for g in _load("belgian_vat_grid.json")}

	def test_loads_as_list(self):
		self.assertIsInstance(self.definitions, list)
		self.assertGreater(len(self.definitions), 0)

	def test_every_definition_has_required_fields(self):
		for d in self.definitions:
			self.assertEqual(d["doctype"], "Belgian VAT Tax Definition")
			for field in ("code", "title", "direction", "rate", "tax_type"):
				self.assertIn(field, d, f"missing {field} in {d.get('code')}")

	def test_directions_are_valid(self):
		for d in self.definitions:
			self.assertIn(d["direction"], VALID_DIRECTIONS, f"bad direction in {d['code']}")

	def test_grid_tag_references_exist(self):
		"""Every grid_tag.grid must point to a grid present in the catalogue."""
		for d in self.definitions:
			for tag in d.get("grid_tags", []):
				self.assertIn(
					tag["grid"],
					self.grid_codes,
					f"tax definition {d['code']} references unknown grid {tag['grid']}",
				)

	def test_grid_tag_amount_types_valid(self):
		for d in self.definitions:
			for tag in d.get("grid_tags", []):
				self.assertIn(tag["amount_type"], VALID_AMOUNT_TYPES)

	def test_grid_tag_document_types_valid(self):
		for d in self.definitions:
			for tag in d.get("grid_tags", []):
				self.assertIn(tag["document_type"], VALID_DOCUMENT_TYPES)

	def test_grid_tag_signs_valid(self):
		for d in self.definitions:
			for tag in d.get("grid_tags", []):
				self.assertIn(tag["sign"], VALID_SIGNS)

	def test_codes_are_unique(self):
		codes = [d["code"] for d in self.definitions]
		self.assertEqual(len(codes), len(set(codes)), "duplicate tax definition codes")

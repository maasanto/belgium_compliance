# Copyright (c) 2026, Antoine Maas and contributors
# For license information, please see license.txt

"""Generate the SPF Finances INTERVAT XML payload for a Belgian VAT
Declaration, conforming to NewTVA-in_v0_9.xsd.

The XML carries:
- VATConsignment root with one VATDeclaration child (single-declaration envoi)
- Declarant block with the Belgian VAT number, name and address from Company
- Period block (Quarter | Month + Year)
- Data block with one Amount per non-zero grid
- ClientListingNihil = NO (the annual customer listing is filed separately)
- Ask block with Restitution and Payment defaults to NO

Optional XSD validation runs at generation time when the schema dependencies
are resolvable (the SPF schemas import a few EU TAXUD common types we don't
ship; if those aren't available, validation degrades gracefully to "skipped").
"""

import os
import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import flt
from lxml import etree

NS_VAT = "http://www.minfin.fgov.be/VATConsignment"
NS_COMMON = "http://www.minfin.fgov.be/InputCommon"
NSMAP = {None: NS_VAT, "common": NS_COMMON}

# Map our internal grid codes to the integer GridNumber that goes into the
# XML. INTERVAT collapses 46L/46T into a single grid 46 (the form has two
# input cells but the XML schema only exposes one — the form-rendering
# layer disaggregates them visually).
GRID_TO_INTERVAT = {
	"00": "0",
	"01": "1",
	"02": "2",
	"03": "3",
	"44": "44",
	"45": "45",
	"46L": "46",
	"46T": "46",  # collapse to 46
	"47": "47",
	"48": "48",
	"49": "49",
	"54": "54",
	"55": "55",
	"56": "56",
	"57": "57",
	"59": "59",
	"61": "61",
	"62": "62",
	"63": "63",
	"64": "64",
	"71": "71",
	"72": "72",
	"81": "81",
	"82": "82",
	"83": "83",
	"84": "84",
	"85": "85",
	"86": "86",
	"87": "87",
	"88": "88",
}

# All declaration grid fields that the controller maintains.
GRID_FIELDS = list(GRID_TO_INTERVAT.keys())

XSD_DIR = os.path.join(os.path.dirname(__file__), "xsd")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_xml(declaration) -> bytes:
	"""Serialise a Belgian VAT Declaration to an INTERVAT XML payload."""
	root = etree.Element(
		f"{{{NS_VAT}}}VATConsignment",
		nsmap=NSMAP,
		VATDeclarationsNbr="1",
	)

	vat_decl = etree.SubElement(
		root,
		f"{{{NS_VAT}}}VATDeclaration",
		SequenceNumber="1",
		DeclarantReference=_declarant_reference(declaration),
	)

	# Declarant block (children in common namespace)
	_append_declarant(vat_decl, declaration)

	# Period block (children in VAT namespace per NewTVA-in.xsd)
	_append_period(vat_decl, declaration)

	# Data block — one Amount element per non-zero grid
	_append_data(vat_decl, declaration)

	# Required tail elements
	etree.SubElement(vat_decl, f"{{{NS_VAT}}}ClientListingNihil").text = "NO"
	etree.SubElement(
		vat_decl,
		f"{{{NS_VAT}}}Ask",
		Restitution="NO",
		Payment="NO",
	)

	return etree.tostring(
		root,
		xml_declaration=True,
		encoding="ISO-8859-1",
		pretty_print=True,
	)


def validate_xml(xml_bytes: bytes) -> dict:
	"""Validate XML against the NewTVA-in XSD when dependencies are available.

	Returns {"status": "valid" | "skipped" | "invalid", "errors": [...]}.
	Skipped means the XSD couldn't be loaded (missing transitive dependency
	like the EU TAXUD commontypes_v1.xsd) — not a structural failure.
	"""
	xsd_path = os.path.join(XSD_DIR, "NewTVA-in.xsd")
	if not os.path.exists(xsd_path):
		return {"status": "skipped", "errors": ["XSD not found at " + xsd_path]}

	try:
		schema_doc = etree.parse(xsd_path)
		schema = etree.XMLSchema(schema_doc)
	except etree.XMLSchemaParseError as exc:
		return {"status": "skipped", "errors": [f"Schema load failed: {exc}"]}

	try:
		doc = etree.fromstring(xml_bytes)
	except etree.XMLSyntaxError as exc:
		return {"status": "invalid", "errors": [f"XML syntax error: {exc}"]}

	if schema.validate(doc):
		return {"status": "valid", "errors": []}

	return {
		"status": "invalid",
		"errors": [str(e) for e in schema.error_log],
	}


# ---------------------------------------------------------------------------
# Internals — block builders
# ---------------------------------------------------------------------------


def _append_declarant(vat_decl, declaration):
	declarant = etree.SubElement(vat_decl, f"{{{NS_VAT}}}Declarant")

	company = frappe.get_cached_doc("Company", declaration.company)
	vat_number = _normalise_be_vat_number(company.tax_id, company.name)
	address = _company_address(company.name)

	etree.SubElement(declarant, f"{{{NS_COMMON}}}VATNumber").text = vat_number
	etree.SubElement(declarant, f"{{{NS_COMMON}}}Name").text = company.company_name
	etree.SubElement(declarant, f"{{{NS_COMMON}}}Street").text = address["street"]
	etree.SubElement(declarant, f"{{{NS_COMMON}}}PostCode").text = address["pincode"]
	etree.SubElement(declarant, f"{{{NS_COMMON}}}City").text = address["city"]
	etree.SubElement(declarant, f"{{{NS_COMMON}}}CountryCode").text = "BE"
	if address["email"]:
		etree.SubElement(declarant, f"{{{NS_COMMON}}}EmailAddress").text = address["email"]
	if address["phone"]:
		etree.SubElement(declarant, f"{{{NS_COMMON}}}Phone").text = address["phone"]


def _append_period(vat_decl, declaration):
	period = etree.SubElement(vat_decl, f"{{{NS_VAT}}}Period")
	idx = int(declaration.period_month_or_quarter)
	if declaration.period_type == "Quarterly":
		etree.SubElement(period, f"{{{NS_VAT}}}Quarter").text = str(idx)
	else:
		etree.SubElement(period, f"{{{NS_VAT}}}Month").text = str(idx)
	etree.SubElement(period, f"{{{NS_VAT}}}Year").text = str(int(declaration.period_year))


def _append_data(vat_decl, declaration):
	data = etree.SubElement(vat_decl, f"{{{NS_VAT}}}Data")
	# Aggregate by INTERVAT grid number (collapses 46L+46T → 46)
	by_intervat: dict[str, Decimal] = {}
	for grid_code in GRID_FIELDS:
		amount = flt(declaration.get(f"g_{grid_code.lower()}"))
		if not amount:
			continue
		intervat_grid = GRID_TO_INTERVAT[grid_code]
		by_intervat[intervat_grid] = by_intervat.get(intervat_grid, Decimal("0")) + Decimal(str(amount))

	# Emit in the conventional grid order
	for intervat_grid in sorted(by_intervat.keys(), key=lambda g: int(g)):
		amount = by_intervat[intervat_grid]
		# PositiveAmount_Type — XSD expects positive values (sign is implicit
		# in the grid's role; e.g. 71 vs 72 carry the direction).
		formatted = f"{amount:.2f}"
		etree.SubElement(
			data,
			f"{{{NS_VAT}}}Amount",
			GridNumber=intervat_grid,
		).text = formatted


# ---------------------------------------------------------------------------
# Internals — data sources
# ---------------------------------------------------------------------------


def _declarant_reference(declaration) -> str:
	# DeclarantReference_Type — xs:token with maxLength="14".
	# Format: "Y2026Q1" / "Y2026M07" — keeps it short and readable.
	idx = int(declaration.period_month_or_quarter)
	if declaration.period_type == "Quarterly":
		return f"Y{int(declaration.period_year)}Q{idx}"
	return f"Y{int(declaration.period_year)}M{idx:02d}"


def _normalise_be_vat_number(raw_tax_id: str | None, company_name: str) -> str:
	"""Strip any "BE", spaces, dots and dashes; return a 10-digit string.

	Belgian VAT numbers are 10 digits (with a leading 0 when the legacy 9-digit
	form is encountered, e.g. KBO/BCE numbers). The XSD type BEVATNumber
	enforces exactly 10 digits.
	"""
	if not raw_tax_id:
		frappe.throw(
			_("Company {0} has no Tax ID set — required to file the INTERVAT declaration.").format(
				company_name
			)
		)
	digits = re.sub(r"[^0-9]", "", raw_tax_id)
	if len(digits) == 9:
		digits = "0" + digits
	if len(digits) != 10:
		frappe.throw(_("Company Tax ID {0!r} is not a valid 10-digit Belgian VAT number.").format(raw_tax_id))
	return digits


def _company_address(company_name: str) -> dict:
	"""Return the company's primary address as a dict.

	Falls back to empty strings (and lets XSD validation surface the missing
	field rather than throwing here) — INTERVAT requires Street/PostCode/City
	but having a partial XML for inspection is more useful than an opaque
	throw at build time.
	"""
	# Dynamic Link from Address to Company is the standard way addresses are
	# attached in ERPNext. Pick the primary, or the first.
	address_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Company", "link_name": company_name, "parenttype": "Address"},
		"parent",
	)
	street = pincode = city = email = phone = ""
	if address_name:
		addr = frappe.get_cached_doc("Address", address_name)
		street_parts = [addr.address_line1, addr.address_line2]
		street = " ".join(p for p in street_parts if p) or ""
		pincode = addr.pincode or ""
		city = addr.city or ""
		email = addr.email_id or ""
		phone = addr.phone or ""

	# Fall back to Company-level fields where they exist.
	if not email:
		email = frappe.db.get_value("Company", company_name, "email") or ""
	if not phone:
		phone = frappe.db.get_value("Company", company_name, "phone_no") or ""

	return {
		"street": street or "—",
		"pincode": pincode or "0000",
		"city": city or "—",
		"email": email,
		"phone": phone,
	}

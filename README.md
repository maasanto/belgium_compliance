# Belgium Compliance

Belgian regional compliance app for [ERPNext](https://github.com/frappe/erpnext).

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

## Scope

- Belgian PCMN chart of accounts (FR/NL) — already merged upstream in ERPNext
- Belgian VAT declaration (INTERVAT, 30-grid official catalogue)
- INTERVAT XML generation against official SPF Finances XSD schemas
- Annual customer listing (LK) — planned
- Intracommunity sales statement (ICO) — planned

## Status

- Phase 1 — Catalogue + grid mapping + materialisation : **shipped**.
- Phase 2 — Belgian VAT Declaration + multi-rate GL extraction : **shipped**.
- Phase 3 — INTERVAT XML generation + XSD validation : **shipped**.
- Phase 4+ — Closing JE on submit, listing LK, ICO, SOAP submission : roadmap.

See [docs/USAGE.md](docs/USAGE.md) for setup and usage,
[docs/ROADMAP.md](docs/ROADMAP.md) for the remaining phases.

## Installation

Requires Frappe v15+ and ERPNext on the bench.

```bash
bench get-app https://github.com/maasanto/belgium_compliance
bench --site <site> install-app belgium_compliance
```

## Out of scope (V1)

The current catalogue covers ASBL and standard SME purchase / sales scenarios
under the regular VAT regime. The following regimes are intentionally not
shipped and require additional templates from the user:

- **Vehicle deduction limitation** (art. 45 §2 Code TVA — D35 / D50 / D85
  templates for company cars). Belgian regulation accepts both an
  at-posting split and a periodic régularisation in box 61; neither is
  modelled. Planned for V2 by adding a `factor_percent` field to
  `Belgian VAT Grid Tag` and seven derived templates.
- **OSS / IOSS** (B2C cross-border sales > 10 000 €/year, low-value imports
  ≤ 150 €). These are filed in a separate INTERVAT declaration and do not
  map to the periodic 30-grid form.
- **Margin scheme** (second-hand goods, art. 58 §4 Code TVA — antique
  dealers, used vehicles).
- **Forfait agricole** (art. 57 Code TVA — small farming operations).

Grille 91 (December advance payment) was abrogated by the AR of 29/03/2021
and is not shipped.

## Attributions

The Belgian VAT tax catalogue shipped in
`belgium_compliance/fixtures/belgian_vat_tax_definition.json` (rate, direction,
INTERVAT grid mapping, and reverse-charge fan-out) is ported from Odoo
17.0's `l10n_be` module — specifically `data/template/account.tax-be.csv`
and `data/account_tax_report_data.xml`. The original Odoo localisation was
authored and contributed by Odoo S.A. and Noviat. The upstream data is
licensed LGPL-3; this app is distributed under GPL-3, which is compatible
per LGPL-3 §3 (re-distribution under GPL).

XSD schemas under `belgium_compliance/xsd/` are sourced from the Belgian
SPF Finances INTERVAT portal and are public.

## License

GPL-3.0

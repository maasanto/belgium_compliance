# Belgium Compliance — Mise en place et utilisation

Cette app gère la conformité TVA belge sur Frappe / ERPNext : catalogue
INTERVAT, mappage des taux aux grilles, génération de la déclaration TVA
périodique. Elle s'appuie sur le PCMN (déjà mergé upstream dans ERPNext).

---

## 1. Prérequis

- Frappe v15+ et ERPNext installés sur le bench.
- Une société belge dans la base : `Country = Belgium`, devise `EUR`.
- Un plan comptable PCMN configuré pour cette société (fourni par ERPNext
  depuis le merge `feat/be-pcmn-from-official-source`).
- Les comptes TVA standards créés sous le PCMN — typiquement :
  - `4511` — TVA collectée
  - `4116` — TVA à récupérer (déductible)
  - `4117` — TVA à récupérer sur biens d'investissement *(optionnel)*
  - `4514` — TVA due en autoliquidation *(ou compte d'attente 4951)*
  - `4956` — TVA déductible en autoliquidation *(ou réutiliser 4116)*

---

## 2. Installation

Depuis le bench :

```bash
bench get-app https://github.com/maasanto/belgium_compliance  # ou path local
bench --site <site> install-app belgium_compliance
```

L'install charge automatiquement les fixtures :

- 30 enregistrements `Belgian VAT Grid` (catalogue INTERVAT)
- 65 enregistrements `Belgian VAT Tax Definition` (templates de taxes)
- ~380 lignes `Belgian VAT Grid Tag` (mapping taxe → grilles)

Vérification :

```bash
bench --site <site> mariadb -e "SELECT COUNT(*) FROM \`tabBelgian VAT Tax Definition\`"
# Attendu : 65
```

---

## 3. Configuration par société

### 3.1 Créer le `Belgian VAT Settings`

Dans le desk : **Belgian VAT Settings → New**, ou via la console :

```python
settings = frappe.new_doc("Belgian VAT Settings")
settings.company = "Ma Société Belge"
settings.output_vat_account            = "4511 - TVA collectée - MSB"
settings.input_vat_deductible_account  = "4116 - TVA à récupérer - MSB"
settings.input_vat_investment_account  = "4117 - TVA inv. - MSB"  # optionnel
settings.reverse_charge_vat_due_account        = "4513 - TVA due s/acquis. intracom. - MSB"
settings.domestic_reverse_charge_vat_due_account = "4510 - TVA due cocontractant - MSB"  # optionnel
settings.reverse_charge_vat_deductible_account = "4116 - TVA à récupérer - MSB"
settings.insert()
```

Le compte `domestic_reverse_charge_vat_due_account` est optionnel : il reçoit
la TVA due autoliquidée des achats cocontractant (art. 20 AR n°1, grilles
56/87), tandis que `reverse_charge_vat_due_account` reste utilisé pour les
acquisitions intracommunautaires, services intracommunautaires et imports
(grilles 55/86). S'il est vide, tout part sur `reverse_charge_vat_due_account`
(comportement historique).

Un seul `Belgian VAT Settings` par société (la company sert de clé primaire).
Les comptes peuvent pointer sur les mêmes ou des comptes distincts selon le
niveau de détail souhaité dans les balances âgées.

#### Compte courant TVA (centralisation) — optionnel

```python
settings.vat_settlement_account = "4519 - Compte courant TVA - MSB"
```

L'OD périodique de **centralisation** (solder 4511 / 4116 vers le compte
courant 4519 en fin de période) est un transfert, pas un événement TVA. Sans
ce paramètre, ses jambes seraient lues comme régularisations (grilles 63 / 64).
Avec le compte renseigné, toute pièce qui touche le compte courant est exclue
de la lecture des comptes TVA. Ne pas y mettre un des comptes TVA ci-dessus
(la validation le refuse).

#### Mapping des bases passées en écriture (OD) — optionnel

La table **Journal Entry Base Mapping** route les bases comptabilisées
directement en `Journal Entry` (recettes journalières, corrections, bases
d'autoliquidation régularisées) vers une grille de déclaration :

```python
settings.append("journal_mappings", {
    "account": "700000 - Ventes de services - MSB",  # compte de produit / charge
    "grid": "03",                                      # grille de base cible
    "sign": 1,                                          # +1 pour un compte de produit
})
```

Contribution = `(crédit − débit) × sign`. Ne mappez **que des comptes de base**
(produits / charges) : la TVA comptabilisée sur les comptes ci-dessus est déjà
lue depuis le grand livre, et seules les écritures de type `Journal Entry` sont
prises ici (les factures passent par leurs lignes, aucun double comptage).

### 3.2 Matérialiser les templates de taxes

```python
from belgium_compliance.setup import materialise_for_company

# Dry-run — affiche ce qui serait créé sans rien écrire
result = materialise_for_company("Ma Société Belge", dry_run=1)

# Live — crée 65 templates parents + 65 Item Tax Templates + 65 liens
result = materialise_for_company("Ma Société Belge")
print(result["created"], "templates créés,", result["errors"], "erreurs")
```

L'opération est **idempotente** : la rejouer skippe les définitions déjà
matérialisées. Utile quand on ajoute de nouvelles `Belgian VAT Tax Definition`
ou qu'on remappe les comptes.

Après matérialisation, on dispose, dans la société :

- 13 `Sales Taxes and Charges Template` préfixés `BE — `
- 52 `Purchase Taxes and Charges Template` préfixés `BE — `
- 65 `Item Tax Template` préfixés `BE — ` (un par scénario, pour les factures
  multi-taux)

---

## 4. Utilisation quotidienne

### 4.1 Émettre une facture mono-taux (cas standard)

Sur la `Sales Invoice` (ou `Purchase Invoice`) :

1. **Customer / Supplier**, **Posting Date** : standards.
2. **Sales Taxes and Charges Template** : sélectionner le template `BE —`
   correspondant au scénario fiscal (ex. `BE — TVA 21% — Ventes de biens`).
3. Ajouter les lignes d'articles. ERPNext applique automatiquement le taux
   du template parent.

### 4.2 Émettre une facture multi-taux

Quand la facture mélange plusieurs taux (ex. un produit à 21 % et un livre
à 6 %) :

1. Sur la facture parent, choisir un template `BE —` quelconque (par exemple
   le 21 %, qui sert de "rail") — il fournit les comptes de référence.
2. Sur **chaque ligne d'article**, renseigner **Item Tax Template** = le
   `BE —` correspondant à ce taux (`BE — TVA 6% — Ventes` pour le livre).
3. ERPNext utilise le taux de l'Item Tax Template pour cette ligne et le
   taux du parent comme défaut pour les lignes sans override.

Ne mélangez **pas** des opérations soumises à autoliquidation et des opérations
classiques sur la même facture — splitter en deux factures (limitation V1).

### 4.3 Émettre une note de crédit

Utiliser le mécanisme natif d'ERPNext : `Return / Credit Note` depuis la facture
originale, ou créer une `Sales Invoice` avec `Is Return = 1`. L'extraction
détecte `is_return` et applique automatiquement le mapping refund (cases 48 / 49
côté ventes ; 84 / 85 / 62 / 63 côté achats).

### 4.4 Reverse-charge (autoliquidation)

Aucune action particulière — le template `BE —` choisi (ex. `BE — Acquisition
intracommunautaire de biens (autoliquidation 21%)`) contient automatiquement
deux lignes de taxe (Add + Deduct) qui :

- Créditent le compte "TVA due autoliquidation" (cadre IV)
- Débitent le compte "TVA déductible autoliquidation" (cadre V)
- Maintiennent le total de la facture égal à la base hors taxe

L'extraction fan-out vers les grilles concernées (ex. 86 + 81 + 55 + 59 pour
une acquisition intracommunautaire de biens à 21 %).

---

## 5. Générer une déclaration TVA périodique

### 5.1 Créer le document

Dans le desk : **Belgian VAT Declaration → New**.

- **Company** : la société
- **Period Type** : `Monthly` ou `Quarterly`
- **Year** : ex. `2026`
- **Month or Quarter** : `1`–`12` mensuel ou `1`–`4` trimestriel

À la sauvegarde, les dates de début et fin de période sont calculées
automatiquement.

### 5.2 Calculer

Bouton **Compute** (ou via API : `doc.compute()`).

La déclaration est une **lecture fidèle du grand livre** : chaque famille de
cases vient de la source qui porte la vérité.

1. Reset des 28 champs grilles.
2. **Cases de base** (00-03 / 44-49 / 81-88) et cases *TVA due* en
   autoliquidation (55 / 56 / 57) : walk des `Sales Invoice` et `Purchase
   Invoice` *submitted* de la période dont le template est lié à une
   `Belgian VAT Tax Definition`, fan-out vers les grilles selon les `grid_tags`
   (elles portent la classification biens / services / investissement).
3. **Cases TVA 54 / 59 / 63 / 64** : lues sur la **TVA réellement comptabilisée**
   — les `GL Entry` de la période sur les comptes TVA du `Belgian VAT Settings`,
   routées par compte + sens débit/crédit. Pas de recalcul base × taux, donc
   pas de dérive d'arrondi ligne par ligne, et la TVA passée en écriture (OD)
   est prise en compte automatiquement.
4. **Bases passées en écriture (OD)** : intégrées via le mapping compte→grille
   du `Belgian VAT Settings` (§ 3.1).
5. Application des `adjustments` manuels.
6. Calcul des totaux 71 / 72 (taxe due / sommes dues par l'État).
7. Un contrôle de cohérence au centime (TVA comptabilisée vs base × taux)
   signale, sans bloquer, une TVA comptabilisée sur un compte hors
   `Belgian VAT Settings`.
8. Statut → `Ready`.

### 5.3 Ajouts manuels (régularisations)

Si une régularisation manuelle est nécessaire (ex. correction TVA d'une période
antérieure, grille 61 ou 62) :

- Ouvrir la déclaration (encore en `Ready`).
- Ajouter une ligne dans **Adjustments** : grille, base / tax, montant signé,
  raison (obligatoire pour l'audit).
- Re-cliquer **Compute** — les ajustements sont sommés aux totaux issus
  des factures.

### 5.4 Soumettre

**Submit** verrouille la déclaration. À ce stade, en V1 :

- Le statut peut passer de `Ready` à `Filed` (manuel — après dépôt sur le
  portail INTERVAT) puis à `Paid`.
- L'`INTERVAT Reference` (numéro de dépôt SPF) peut être renseigné après filing.
- L'export XML INTERVAT et le Journal Entry de clôture **ne sont pas encore
  automatisés** (Phases 3 et 4 — voir [ROADMAP.md](ROADMAP.md)).

---

## 6. Limitations connues V1

| Cas | Statut | Workaround |
|---|---|---|
| Multi-taux sur une facture | ✅ Supporté | Item Tax Template par ligne |
| Mixed-regime (RC + non-RC sur même facture) | ❌ Non supporté | Splitter en deux factures |
| Déduction limitée véhicules (D35/D50/D85) | ❌ Non modélisée | Régularisation manuelle en grille 61, ou attendre V2 |
| OSS / IOSS (B2C transfrontalier) | ❌ Hors scope | Déclaration INTERVAT séparée, manuelle |
| Régime de la marge (occasion) | ❌ Hors scope | Templates dédiés à créer manuellement |
| Forfait agricole | ❌ Hors scope | Niche, pas prévu |
| Génération XML INTERVAT | ❌ Phase 3 | Déposer manuellement sur le portail |
| Journal Entry de clôture | ❌ Phase 4 | Saisir manuellement |
| Listing clients annuel (LK) | ❌ Phase 5 | Hors app pour l'instant |
| Relevé intra-UE trimestriel (ICO) | ❌ Phase 5 | Hors app pour l'instant |

---

## 7. Dépannage

### Compute ne remonte aucune facture

1. Vérifier que les factures sont en `docstatus = 1` (Submitted).
2. Vérifier que la `posting_date` tombe bien dans la période.
3. Vérifier que le template parent OU au moins un Item Tax Template de la
   facture est bien lié à une Tax Definition :
   ```python
   frappe.db.exists("Belgian VAT Tax Template Link",
       {"company": "...", "tax_template": "BE — ..."})
   ```
4. Si la matérialisation a été refaite après les factures, les anciens templates
   ne sont plus liés. Re-matérialiser ne suffit pas — il faut soit re-soumettre
   les factures avec les nouveaux templates, soit ajouter manuellement des
   `Belgian VAT Tax Template Link` pour les anciens templates.

### Erreur "Round Off Account missing"

Configurer le compte `Round Off` sur la société (Company → Default Accounts).
Indépendant de belgium_compliance, prérequis ERPNext général.

### Doublons après deuxième matérialisation

Normalement impossible (`materialise_for_company` est idempotent et skippe
les définitions déjà liées). Si un état incohérent existe :

```python
# Tout effacer pour la société et repartir de zéro
for n in frappe.get_all("Belgian VAT Tax Template Link",
                         filters={"company": "..."}, pluck="name"):
    frappe.delete_doc("Belgian VAT Tax Template Link", n,
                       force=True, delete_permanently=True)
for dt in ("Sales Taxes and Charges Template",
            "Purchase Taxes and Charges Template",
            "Item Tax Template"):
    for n in frappe.get_all(dt, filters={"company": "...",
                                          "title": ("like", "BE %")},
                              pluck="name"):
        frappe.delete_doc(dt, n, force=True, delete_permanently=True)
```

Puis re-matérialiser.

---

## 8. Modèle de données — vue d'ensemble

```
Belgian VAT Grid               ── Catalogue 30 grilles INTERVAT (fixture)
       ▲
       │ référencée via grid_tags
       │
Belgian VAT Tax Definition     ── 65 templates abstraits company-agnostic
       │                          (ports Odoo l10n_be + recherche SPF)
       │ matérialisée via setup.materialise_for_company(company)
       ▼
Belgian VAT Tax Template Link  ── Pont (company × Tax Definition × Template)
       │                          tax_template_doctype ∈ {Sales / Purchase /
       │                          Item Tax Template}
       │
Sales / Purchase / Item        ── Templates ERPNext standard, créés et câblés
Tax Templates                     aux comptes 4XX par la matérialisation
       │
       │ référencés par les factures
       ▼
Sales Invoice / Purchase Invoice   ── Factures réelles
       │
       │ extraction par compute()
       ▼
Belgian VAT Declaration        ── Document de déclaration submittable
       └─ computed_lines        ── Audit trail per-grid contribution
       └─ adjustments           ── Régularisations manuelles
       └─ g_00, g_01, …, g_72  ── 28 champs grilles calculés
```

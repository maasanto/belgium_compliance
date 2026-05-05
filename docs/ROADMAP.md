# Belgium Compliance — Roadmap

État au 2026-05-05. Phases 1, 2 et 3 livrées. Phases 4+ à construire.

---

## Phase 3 — Génération XML INTERVAT — ✅ livré

**Objectif** : produire un fichier XML conforme au schéma SPF Finances
`NewTVA-in_v0_9.xsd`, attaché à la `Belgian VAT Declaration` lors du
passage en statut `Ready`, prêt à être uploadé manuellement sur le
portail INTERVAT.

**Livrables** :
- Module `belgium_compliance/intervat_xml.py` qui sérialise une déclaration en
  XML (lxml, namespaces SPF). ✅
- Vendor du XSD officiel sous `belgium_compliance/xsd/NewTVA-in.xsd`. ✅
- Validation XSD côté serveur au moment de la génération — pas de XML
  invalide attaché. ✅ (gracefully degrades to "skipped" if EU TAXUD
  transitive schemas are missing)
- Champ `generated_xml` (Attach) peuplé automatiquement. ✅

---

## Phase 4 — Journal Entry de clôture sur submit

**Objectif** : à la soumission de la déclaration, générer automatiquement
l'écriture comptable de clôture TVA classique.

**Mécanique attendue** :
- Débit : compte `output_vat_account` (4511) du montant collecté
- Crédit : compte `input_vat_deductible_account` (4116) du montant déductible
- Solde net :
  - Si TVA due → crédit `4510` (TVA à payer) ou `451` selon plan
  - Si TVA récupérable → débit `4111` ou `4119`
- Date de l'écriture : dernier jour de la période
- Référence : numéro de la déclaration

**Livrables** :
- Méthode `_create_closing_entry()` dans `belgian_vat_declaration.py`.
- Champ supplémentaire dans `Belgian VAT Settings` : `vat_payable_account` /
  `vat_receivable_account` pour le solde.
- Cancellation propre — `on_cancel()` doit annuler le Journal Entry.
- Gestion des reverse-charge dans la clôture (les comptes 4514/4956 doivent
  aussi être soldés).

**Coût estimé** : 1-2 jours. Mécanique connue mais sensible aux conventions
de plan comptable de chaque société.

---

## Phase 5 — Listing clients annuel (LK) et Relevé intra-UE (ICO)

Deux DocTypes additionnels, indépendants de la déclaration TVA périodique
mais qui partagent la même source de données (factures de la période).

### 5.1 Listing clients annuel (LK)

**Obligation** : déclaration annuelle des clients belges assujettis à la TVA
auxquels on a vendu pour > 250 € HT dans l'année. À déposer avant le 31 mars
de l'année suivante.

**Livrables** :
- DocType `Belgian VAT Customer Listing` (annuel, par société et année).
- Extraction des Sales Invoices de l'année avec un client BE assujetti
  (TVA n° BE0xxx).
- Génération XML conforme au XSD `NewLK-in_v0_9.xsd`.

### 5.2 Relevé intra-UE (ICO)

**Obligation** : déclaration trimestrielle (mensuelle si seuil dépassé) des
livraisons et services intra-UE B2B avec autoliquidation par le preneur.
À déposer pour le 20 du mois suivant la période.

**Livrables** :
- DocType `Belgian VAT Intracom Statement` (trimestriel/mensuel).
- Extraction des factures avec template `BE — ` lié à une Tax Definition de
  type `Zero` direction `Sales` (livraisons intra-UE 46L/46T) ou la grille 44
  (services intra-UE).
- Génération XML conforme au XSD `NewICO-in_v0_9.xsd`.

**Coût estimé global** : 3-4 jours. La logique d'extraction est analogue à
la Phase 2, le travail principal est sur les XSD et leurs particularités.

---

## Phase 6 — Soumission directe à INTERVAT (SOAP)

**Objectif** : déposer le XML directement sur le portail SPF sans passer
par l'upload manuel.

**Mécanique** :
- Signature XAdES du XML avec un certificat eID belge ou un certificat
  serveur émis par CSAM / Itsme.
- Appel SOAP au endpoint INTERVAT (auth via certificat client SSL).
- Récupération de l'accusé de réception (numéro de référence SPF).
- Mise à jour automatique du champ `intervat_reference` et passage du
  statut à `Filed`.

**Livrables** :
- Module `belgium_compliance/intervat_client.py` (SOAP via `zeep`).
- DocType `Belgian VAT Filing Credentials` (Single, par société) pour
  stocker le chemin du certificat et la phrase de passe (chiffrée).
- Gestion des erreurs SPF (accusé refusé, format invalide, etc.).
- Bouton "File on INTERVAT" sur le formulaire de déclaration.

**Coût estimé** : 4-6 jours. Le gros du travail est sur la signature XAdES
qui est notoirement délicate. Plusieurs librairies Python existent (`signxml`,
`xmlsec`) mais aucune ne couvre 100 % des cas SPF — souvent du tuning.

**Optionnel** : peut ne jamais être livré pour les clients qui acceptent
le dépôt manuel (qui ne prend que 30 secondes).

---

## V2 — Déduction limitée véhicules (D35 / D50 / D85)

**Quand** : après Phase 4, sur demande utilisateur (typique pour toute société
avec véhicule de société, hors ASBL).

**Mécanique** :
- Ajouter un champ `factor_percent` (0–100) à `Belgian VAT Grid Tag`.
- Porter les 7 templates Odoo dropés au Phase 1 (D35/D50/D85 × 21/12/6 + 4
  variantes import).
- Adapter `_extract_from_invoices` pour appliquer `factor_percent` sur la
  contribution `contributing_amount`.

**Coût estimé** : 1 jour. Petit, isolé, faisable dès qu'un client demande.

---

## Hors roadmap (out of scope définitif)

- **OSS / IOSS** — déclaration INTERVAT séparée, modèle métier différent.
  Pourrait faire l'objet d'une autre app (`belgium_compliance_oss`).
- **Régime de la marge** — niche (brocanteurs, antiquaires, voitures
  d'occasion). Templates à créer à la main.
- **Forfait agricole** — très niche.

---

## Priorisation suggérée

1. **Phase 4 (Closing JE)** — finalise la cohérence comptable.
2. **Phase 5 (LK + ICO)** — complète le périmètre INTERVAT au-delà de la
   périodique.
3. **V2 véhicules** — selon premier client non-ASBL.
4. **Phase 6 (SOAP submission)** — uniquement si la friction du dépôt manuel
   devient un problème.

Phases 4 et 5 forment avec les phases 1-3 livrées la "V1 publishable" pour la
communauté. Phase 6 est confort.

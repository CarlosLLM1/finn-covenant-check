"""The standard positions Finn knows, and where to find each one in the business plan.

Idea: GuV and Bilanz follow the HGB layout (§ 266 / § 275 HGB), so the line labels are
standardised. Instead of letting the LLM pick Excel cells, the LLM translates each contract
definition into these standard positions (e.g. "EBITDA = jahresueberschuss + ertragsteuern
+ ..."). Code then looks the positions up here. The table is the single place to adapt if a
plan uses different labels.

Fields:
  sheet   - worksheet name
  row     - text that must appear in the row label (case-insensitive, exactly one match)
  column  - text that must appear in the column header; None = the plan-year column
  flip    - True for P&L expenses, which the plan stores as negative numbers.
            We want the amount (positive), e.g. taxes 3,200 instead of -3,200.
  missing - the plan has no such line; value is 0 and the reply says so
"""

POSITIONS: dict[str, dict] = {
    # --- Erfolgsplan (GuV) ---
    "jahresueberschuss":    {"sheet": "GuV", "row": "Jahresüberschuss", "desc": "Jahresüberschuss/-fehlbetrag"},
    "ertragsteuern":        {"sheet": "GuV", "row": "Steuern vom Einkommen und vom Ertrag", "flip": True, "desc": "Steuern vom Einkommen und vom Ertrag"},
    "zinsaufwand_guv":      {"sheet": "GuV", "row": "Zinsen und ähnliche Aufwendungen", "flip": True, "desc": "Zinsen und ähnliche Aufwendungen laut GuV (inkl. Aufzinsung von Rückstellungen)"},
    "aufzinsung_rueckstellungen": {"sheet": "GuV", "row": "Aufzinsung von Pensionsrückstellungen", "flip": True, "desc": "davon Aufzinsung von (Pensions-)Rückstellungen, nicht zahlungswirksam"},
    "zinsertraege":         {"sheet": "GuV", "row": "Sonstige Zinsen und ähnliche Erträge", "desc": "Sonstige Zinsen und ähnliche Erträge"},
    "abschreibungen":       {"sheet": "GuV", "row": "Abschreibungen", "flip": True, "desc": "Abschreibungen auf immaterielle Vermögensgegenstände und Sachanlagen"},
    "ausserordentliche_posten": {"missing": True, "desc": "Außerordentliche Aufwendungen/Erträge (§ 285 Nr. 31 HGB), Saldo"},

    # --- Planbilanz ---
    "bilanzsumme":          {"sheet": "Bilanz", "row": "Summe Aktiva", "desc": "Bilanzsumme (Summe Aktiva)"},
    "eigenkapital":         {"sheet": "Bilanz", "row": "A. Eigenkapital", "desc": "Bilanzielles Eigenkapital (§ 266 Abs. 3 A HGB)"},
    "ausstehende_einlagen": {"missing": True, "desc": "Eingeforderte, noch nicht eingezahlte Einlagen"},
    "sonderposten":         {"sheet": "Bilanz", "row": "Sonderposten für Investitions", "desc": "Sonderposten für Investitions- und Baukostenzuschüsse"},
    "rueckstellungen":      {"sheet": "Bilanz", "row": "C. Rückstellungen", "desc": "Rückstellungen gesamt"},
    "verb_kreditinstitute": {"sheet": "Bilanz", "row": "Verbindlichkeiten gegenüber Kreditinstituten", "desc": "Verbindlichkeiten gegenüber Kreditinstituten (Stichtag)"},
    "gesellschafterdarlehen": {"sheet": "Bilanz", "row": "Verbindlichkeiten gegenüber Gesellschafter", "desc": "Gesellschafterdarlehen (nachrangig, qualifizierter Rangrücktritt) (Stichtag)"},
    "finanzierungsleasing": {"missing": True, "desc": "Verbindlichkeiten aus Finanzierungsleasing (laut Plan nur Operating Leasing)"},
    "liquide_mittel":       {"sheet": "Bilanz", "row": "Kassenbestand und Guthaben", "desc": "Kassenbestand und Guthaben bei Kreditinstituten (laut Plan frei verfügbar)"},

    # --- Finanzierungsplan / Darlehensspiegel (whole sheet = plan year) ---
    "zins_bankdarlehen":    {"sheet": "Finanzierung", "row": "Summe Kreditinstitute", "column": "Zinsaufwand", "desc": "Zinsaufwand Bankdarlehen (ohne Aufzinsung)"},
    "zins_gesellschafterdarlehen": {"sheet": "Finanzierung", "row": "Gesellschafterdarlehen", "column": "Zinsaufwand", "desc": "Zinsaufwand Gesellschafterdarlehen"},
    "tilgung_bankdarlehen": {"sheet": "Finanzierung", "row": "Summe Kreditinstitute", "column": "Planmäßige Tilgung", "desc": "Planmäßige Tilgung Bankdarlehen"},
    "tilgung_gesellschafterdarlehen": {"sheet": "Finanzierung", "row": "Gesellschafterdarlehen", "column": "Planmäßige Tilgung", "desc": "Planmäßige Tilgung Gesellschafterdarlehen"},
    "sondertilgung_bankdarlehen": {"sheet": "Finanzierung", "row": "Summe Kreditinstitute", "column": "Sondertilgung", "desc": "Freiwillige Sondertilgung Bankdarlehen"},
    "sondertilgung_gesellschafterdarlehen": {"sheet": "Finanzierung", "row": "Gesellschafterdarlehen", "column": "Sondertilgung", "desc": "Freiwillige Sondertilgung Gesellschafterdarlehen"},
}

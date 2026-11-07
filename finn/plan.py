"""Reading the business plan (Wirtschaftsplan.xlsx) into a catalog of cells.

The LLM later chooses which cells match a contract term. It only sees the catalog
(id, sheet, row label, column header), never the values, so it cannot change a number.
No LLM is involved in this file.

Built to cope with plans that look different from the sample:
- labels anywhere left of the numbers (e.g. a code column "A." before "Eigenkapital"),
- headers over several rows ("Plan" / 2027), as number, date or merged cell,
- different words for actuals and forecasts (Ist, Vorjahr, HR, Hochrechnung, Forecast, ...),
- units per sheet (EUR, T€/TEUR, Mio. €), everything converted to T€,
- sheet roles recognised by content, not by name (P&L, balance sheet, loan schedule).
If something essential is missing, read_plan raises PlanError with a message Finn sends back.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from openpyxl.formula import Tokenizer
from openpyxl.utils import get_column_letter, range_boundaries


@dataclass
class PlanCell:
    id: str                 # e.g. "Bilanz!D22"
    sheet: str
    label: str              # row label, e.g. "A. Eigenkapital"
    header: str             # column header, e.g. "31.12.2027 (Plan)"
    value: float            # amount in T€; P&L / loan-schedule outflows as positive amounts
    is_total: bool          # True if the cell is a formula (a subtotal or total)
    parts: set[str] = field(default_factory=set)  # all input cells a total is built from


@dataclass
class SheetInfo:
    name: str
    role: str               # "GuV", "Bilanz", "Darlehen" or "sonstige"
    unit: str               # e.g. "T€", "EUR (umgerechnet in T€)"
    cells: int


class PlanCells(dict):
    """dict[id, PlanCell] plus what was found, for messages and the reply."""
    sheets: list[SheetInfo]
    notes: list[str]


class PlanError(Exception):
    """The plan cannot be read reliably; Finn replies with this message instead of numbers."""


# ---------- words ----------

ACTUAL_WORDS = ("ist", "vorjahr", "actual", "abschluss", "jahresabschluss")
FORECAST_WORDS = ("hr", "hochrechnung", "forecast", "fc", "prognose", "erwartung", "vorschau")

ROLE_PATTERNS = {
    "GuV": ("jahresüberschuss", "jahresfehlbetrag", "jahresergebnis", "ergebnis vor", "umsatzerlöse"),
    "Bilanz": ("summe aktiva", "summe passiva", "bilanzsumme", "eigenkapital"),
    "Darlehen": ("tilgung", "darlehen", "zinsaufwand"),
}


def _text(v) -> str:
    """Any cell value as header text: numbers like 2027 and dates like 31.12.2027 included."""
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-zäöüß]+", s.lower()))


def _is_year_like(v) -> bool:
    return isinstance(v, int) and 1990 <= v <= 2100 or isinstance(v, (dt.datetime, dt.date))


# ---------- sheet layout ----------

def _unit_factor(ws) -> tuple[float, str]:
    """Factor to convert the sheet's numbers into T€, found in the title/first rows."""
    text = " ".join(_text(c.value) for row in ws.iter_rows(min_row=1, max_row=6) for c in row).lower()
    text += " " + ws.title.lower()
    if re.search(r"\bmio\b|millionen|mio\.", text):
        return 1000.0, "Mio. € (umgerechnet in T€)"
    if re.search(r"t€|teur|tsd|tausend|k€", text):
        return 1.0, "T€"
    if re.search(r"\beur\b|€|euro", text):
        return 0.001, "EUR (umgerechnet in T€)"
    return 1.0, "T€ (angenommen, keine Einheit angegeben)"


def _first_data_row(ws) -> int:
    """First row that has a text label and a real number (not a year or date in a header row)."""
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
        has_label = any(isinstance(c.value, str) and c.value.strip() and not c.value.startswith("=") for c in row)
        numbers = [c.value for c in row if isinstance(c.value, (int, float)) or
                   (isinstance(c.value, str) and c.value.startswith("="))]
        if has_label and numbers and not all(_is_year_like(v) for v in numbers):
            return row[0].row
    return ws.max_row + 1


def _headers(ws, data_row: int) -> dict[int, str]:
    """Column -> header text, combining all header rows above the data (merged cells spread out)."""
    values: dict[tuple[int, int], object] = {}
    for row in ws.iter_rows(min_row=1, max_row=data_row - 1):
        for c in row:
            if c.value is not None:
                values[(c.row, c.column)] = c.value
    for rng in ws.merged_cells.ranges:
        if rng.min_row < data_row and (rng.min_row, rng.min_col) in values:
            for col in range(rng.min_col, rng.max_col + 1):
                for r in range(rng.min_row, min(rng.max_row, data_row - 1) + 1):
                    values.setdefault((r, col), values[(rng.min_row, rng.min_col)])
    headers: dict[int, list[str]] = {}
    for (r, col), v in sorted(values.items()):
        t = _text(v)
        if t:
            headers.setdefault(col, []).append(t)
    return {col: " ".join(parts) for col, parts in headers.items()}


def _is_plan_column(header: str, year: int) -> bool:
    if str(year) not in header:
        return False
    w = _words(header)
    return not (w & set(ACTUAL_WORDS) or w & set(FORECAST_WORDS))


def _role(labels: list[str]) -> str:
    text = " ".join(labels).lower()
    scores = {role: sum(p in text for p in pats) for role, pats in ROLE_PATTERNS.items()}
    if scores["GuV"] >= 2 or ("jahresüberschuss" in text and scores["Bilanz"] < 2):
        return "GuV"
    if scores["Bilanz"] >= 2:
        return "Bilanz"
    if scores["Darlehen"] >= 1:
        return "Darlehen"
    return "sonstige"


def _refs(formula: str, sheet: str) -> set[str]:
    """All cell ids a formula refers to, e.g. '=SUM(D4:D6)+Bilanz!D28' ->
    {'GuV!D4', 'GuV!D5', 'GuV!D6', 'Bilanz!D28'}."""
    out = set()
    for tok in Tokenizer(formula).items:
        if tok.type != "OPERAND" or tok.subtype != "RANGE":
            continue
        if "!" in tok.value:
            ref_sheet, rng = tok.value.rsplit("!", 1)
        else:
            ref_sheet, rng = sheet, tok.value
        ref_sheet = ref_sheet.strip("'")
        try:
            min_col, min_row, max_col, max_row = range_boundaries(rng.replace("$", ""))
        except ValueError:
            continue  # named range or whole column: ignore
        if None in (min_col, min_row, max_col, max_row):
            continue
        for c in range(min_col, max_col + 1):
            for r in range(min_row, max_row + 1):
                out.add(f"{ref_sheet}!{get_column_letter(c)}{r}")
    return out


# ---------- main function ----------

def read_plan(path: Path, year: int = 2027) -> PlanCells:
    """All numeric plan-year cells, keyed by id, in T€. Raises PlanError if unusable."""
    try:
        wb_formulas = openpyxl.load_workbook(path, data_only=False)  # which cells are formulas
        wb_values = openpyxl.load_workbook(path, data_only=True)     # the values Excel calculated
    except Exception as e:
        raise PlanError(f"Die Datei {Path(path).name} kann nicht gelesen werden ({e.__class__.__name__}). "
                        "Bitte als .xlsx speichern und erneut senden.")

    cells = PlanCells()
    cells.sheets, cells.notes = [], []
    direct_refs: dict[str, set[str]] = {}
    years_seen: set[int] = set()
    broken: list[str] = []   # formulas without a usable value; reported after the "what is missing" check

    for ws in wb_formulas.worksheets:
        wv = wb_values[ws.title]
        data_row = _first_data_row(ws)
        headers = _headers(ws, data_row)
        factor, unit = _unit_factor(ws)
        for h in headers.values():
            years_seen |= {int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", h)}
        plan_cols = {col for col, h in headers.items() if _is_plan_column(h, year)}

        rows = []
        for row in ws.iter_rows(min_row=data_row):
            # label = all text left of the first number or formula, e.g. "A." + "Eigenkapital"
            parts, numeric = [], []
            for c in row:
                v = c.value
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.startswith("=")):
                    numeric.append(c)
                elif isinstance(v, str) and v.strip() and not numeric:
                    parts.append(v.strip())
            if parts:
                rows.append((" ".join(parts), [c for c in numeric if c.column in plan_cols]))

        role = _role([label for label, _ in rows])
        count = 0
        for label, plan_cells in rows:
            for c in plan_cells:
                formula = c.value if isinstance(c.value, str) and c.value.startswith("=") else None
                value = wv[c.coordinate].value
                if not isinstance(value, (int, float)):
                    if formula:  # e.g. #REF! (points to a missing sheet) or never calculated
                        broken.append(f"{ws.title}!{c.coordinate} ({value})" if value else f"{ws.title}!{c.coordinate}")
                    continue
                # Sign rule: P&L and loan schedule store expenses/outflows as negative inputs; contracts
                # mean the amount ("zuzüglich Zinsaufwand"). Balance sheet signs are kept, so a negative
                # Verlustvortrag stays negative. Totals (formulas) keep their sign, so a loss stays negative.
                if formula is None and value < 0 and role != "Bilanz":
                    value = -value
                cid = f"{ws.title}!{c.coordinate}"
                cells[cid] = PlanCell(cid, ws.title, label, headers.get(c.column, ""), float(value) * factor,
                                      formula is not None)
                if formula:
                    direct_refs[cid] = _refs(formula, ws.title)
                count += 1
        if count:
            cells.sheets.append(SheetInfo(ws.title, role, unit, count))
            if "angenommen" in unit:
                cells.notes.append(f"Blatt '{ws.title}': keine Einheit angegeben, T€ angenommen.")

    # A total's parts = everything it is built from, followed through all levels
    def parts_of(cid: str, seen: set[str]) -> set[str]:
        out = set()
        for ref in direct_refs.get(cid, ()):
            if ref not in seen:
                seen.add(ref)
                out |= {ref} | parts_of(ref, seen)
        return out

    for cid, cell in cells.items():
        cell.parts = parts_of(cid, set())

    _check_found(cells, year, years_seen)
    if broken:
        raise PlanError(f"Formeln ohne gültigen Wert: {', '.join(broken[:5])}"
                        f"{' ...' if len(broken) > 5 else ''}. Bitte die Datei in Excel öffnen, Fehler "
                        "beheben und speichern.")
    check_plan(cells)
    # Check rows are for us, not data: keep them out of what the LLM can choose from
    result = PlanCells({cid: c for cid, c in cells.items() if not _is_check_row(c)})
    result.sheets, result.notes = cells.sheets, cells.notes
    return result


def plan_years(path: Path) -> list[int]:
    """Years that appear in a plan column header ("Plan 2027", "31.12.2027 (Plan)", "Plan" / 2027).
    Used when neither the email nor the file name says which year to check."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception:
        return []
    years: set[int] = set()
    for ws in wb.worksheets:
        for h in _headers(ws, _first_data_row(ws)).values():
            w = _words(h)
            if "plan" in w and not (w & set(ACTUAL_WORDS) or w & set(FORECAST_WORDS)):
                years |= {int(y) for y in re.findall(r"\b(20\d\d)\b", h)}
    return sorted(years)


def _check_found(cells: PlanCells, year: int, years_seen: set[int]) -> None:
    """Is this a usable business plan for `year`? Otherwise say what is missing."""
    if not cells:
        other = sorted(y for y in years_seen if y != year)
        hint = f" Gefundene Jahre in den Spaltenüberschriften: {', '.join(map(str, other))}." if other else ""
        raise PlanError(f"Keine Planwerte für {year} gefunden. Die Datei scheint kein Wirtschaftsplan für {year} "
                        f"zu sein (erwartet: Erfolgsplan, Planbilanz und Finanzplan mit einer Plan-Spalte {year})."
                        + hint)
    roles = {s.role for s in cells.sheets}
    missing = [name for role, name in (("GuV", "Erfolgsplan (GuV)"), ("Bilanz", "Planbilanz")) if role not in roles]
    if missing:
        found = ", ".join(f"'{s.name}' ({s.role})" for s in cells.sheets)
        raise PlanError(f"Im Wirtschaftsplan fehlt: {', '.join(missing)}. Gefundene Blätter: {found}.")
    if "Darlehen" not in roles:
        cells.notes.append("Kein Darlehensspiegel gefunden: Zinsen und Tilgungen nur aus Erfolgsplan/Bilanz.")


def _is_check_row(cell: PlanCell) -> bool:
    label = cell.label.lower()
    return label.startswith("kontrolle") or label.startswith("abgleich")


def check_plan(cells: dict[str, PlanCell]) -> None:
    """The plan must be consistent before we calculate anything from it."""
    # 1. Rows the plan marks as checks ("Kontrolle: Aktiva – Passiva (muss 0 sein)") must be 0
    for cell in cells.values():
        if cell.label.lower().startswith("kontrolle") and abs(cell.value) > 0.5:
            raise PlanError(f"Plan in sich nicht stimmig: {cell.id} „{cell.label}“ = {cell.value:,.0f}")
    # 2. Balance sheet balances: total assets = total liabilities
    aktiva = [c for c in cells.values() if c.label.lower().startswith(("summe aktiva", "aktiva gesamt"))]
    passiva = [c for c in cells.values() if c.label.lower().startswith(("summe passiva", "passiva gesamt"))]
    if aktiva and passiva and abs(aktiva[0].value - passiva[0].value) > 0.5:
        raise PlanError(f"Bilanz geht nicht auf: Aktiva {aktiva[0].value:,.0f} ≠ Passiva {passiva[0].value:,.0f}")


def catalog(cells: dict[str, PlanCell]) -> str:
    """What the LLM sees: one line per cell, WITHOUT values."""
    lines = []
    for c in cells.values():
        mark = " [Summe]" if c.is_total else ""
        lines.append(f"{c.id} | {c.label} | {c.header}{mark}")
    return "\n".join(lines)


if __name__ == "__main__":
    #   python -m finn.plan data/plan/Wirtschaftsplan_2027.xlsx
    import sys

    cells = read_plan(Path(sys.argv[1]))
    for s in cells.sheets:
        print(f"Blatt '{s.name}': {s.role}, Einheit {s.unit}, {s.cells} Planwerte")
    for n in cells.notes:
        print("Hinweis:", n)
    print()
    for c in cells.values():
        print(f"{c.id:16} {c.value:>10,.0f}  {'Σ' if c.is_total else ' '}  {c.label[:60]}  [{c.header}]")
    print(f"\n{len(cells)} cells. Catalog for the LLM (first lines):\n")
    print("\n".join(catalog(cells).splitlines()[:5]))

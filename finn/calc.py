"""All arithmetic. No LLM in this file.

For each covenant: numerator and denominator from the mapped cells, the ratio, rounding as the
contract says, met yes/no, headroom, and a simple stress figure ("how far may it get worse?").
Every intermediate value keeps its source, so the reply can show the full calculation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from typing import TYPE_CHECKING

from finn.plan import PlanCell

if TYPE_CHECKING:  # only for type hints, so this file also runs without mapping.py
    from finn.contracts import Covenant
    from finn.mapping import ContractMapping

TIGHT = 0.10   # less than 10 % relative headroom counts as "tight"


@dataclass
class Line:
    """One component of a numerator/denominator with its value and sources."""
    sign: int
    weight: float
    description: str
    amount: float                 # value of the component before sign/weight
    cells: list[tuple[int, PlanCell]]
    reason: str


@dataclass
class CovenantResult:
    name: str
    numerator: float
    denominator: float
    value_raw: float              # unrounded, in the covenant's unit (percent or ratio)
    value: float                  # rounded as the contract says (or unrounded if it says nothing)
    limit: float
    operator: str
    unit: str
    decimals: int                 # decimals for display: the contract's rounding, else 2
    met: bool
    headroom: float               # distance to the limit in the covenant's unit (positive = room left)
    tight: bool
    stress_text: str              # "how far may it get worse"
    num_lines: list[Line] = field(default_factory=list)
    den_lines: list[Line] = field(default_factory=list)


def _de(x: float, decimals: int = 0) -> str:
    """German number format: 1234.5 -> '1.234,5'."""
    return f"{x:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def round_half_up(x: float, decimals: int) -> float:
    """Commercial rounding (kaufmännisch): 2.345 -> 2.35. Python's round() would give 2.34."""
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def _side(cov: Covenant, side: str, mapping: ContractMapping, cells: dict[str, PlanCell]) -> tuple[float, list[Line]]:
    total, lines = 0.0, []
    for i, comp in enumerate(getattr(cov, side)):
        item = next(m for m in mapping.items if m.covenant == cov.name and m.side == side and m.index == i)
        picked = [(c.sign, cells[c.id]) for c in item.cells]
        amount = sum(s * cell.value for s, cell in picked)
        total += comp.sign * comp.weight * amount
        lines.append(Line(comp.sign, comp.weight, comp.description, amount, picked, item.reason))
    return total, lines


def evaluate(cov: Covenant, mapping: ContractMapping, cells: dict[str, PlanCell]) -> CovenantResult:
    num, num_lines = _side(cov, "numerator", mapping, cells)
    den, den_lines = _side(cov, "denominator", mapping, cells)
    factor = 100.0 if cov.unit == "percent" else 1.0

    if den <= 0:
        # e.g. EBITDA <= 0 for a debt/EBITDA covenant: the ratio is meaningless, treat as breached
        raw = float("inf")
    else:
        raw = num / den * factor
    value = round_half_up(raw, cov.rounding_decimals) if cov.rounding_decimals is not None and den > 0 else raw

    if cov.operator == ">=":
        met = value >= cov.limit
        headroom = value - cov.limit
        # numerator may fall by this much (denominator unchanged) before the limit is reached
        room = num - cov.limit / factor * den
        stress = (f"Zähler darf bei unverändertem Nenner um {_de(room)} T€ ({_de(room / num * 100, 1)} %) sinken."
                  if num > 0 else "")
    else:
        met = value <= cov.limit
        headroom = cov.limit - value
        # denominator (typically EBITDA) may fall by this much before the limit is reached
        room = den - num / (cov.limit / factor) if cov.limit > 0 else 0.0
        stress = (f"Nenner darf bei unverändertem Zähler um {_de(room)} T€ ({_de(room / den * 100, 1)} %) sinken."
                  if den > 0 else "")

    tight = met and cov.limit != 0 and abs(headroom / cov.limit) < TIGHT
    decimals = cov.rounding_decimals if cov.rounding_decimals is not None else 2
    return CovenantResult(cov.name, num, den, raw, value, cov.limit, cov.operator, cov.unit, decimals,
                          met, headroom, tight, stress, num_lines, den_lines)


if __name__ == "__main__":
    # Demo without any LLM: the four covenants with a mapping written by hand.
    #   python -m finn.calc
    from pathlib import Path
    from types import SimpleNamespace as NS

    from finn.plan import read_plan

    cells = read_plan(Path("data/plan/Wirtschaftsplan_2027.xlsx"))

    def comp(desc, sign=1, weight=1.0):
        return NS(description=desc, sign=sign, weight=weight)

    ebitda = [comp("Jahresüberschuss"), comp("Ertragsteuern"), comp("Zinsaufwand"), comp("Zinserträge", -1),
              comp("Abschreibungen")]
    ebitda_cells = [["GuV!D18"], ["GuV!D17"], ["GuV!D13"], ["GuV!D12"], ["GuV!D9"]]
    demo = [
        # name, numerator, denominator, operator, limit, unit, rounding, cells per numerator part, per denominator part
        ("Wirtschaftliche Eigenkapitalquote (V2)", [comp("Eigenkapital"), comp("Sonderposten", 1, 0.5)],
         [comp("Bilanzsumme")], ">=", 27.5, "percent", None,
         [["Bilanz!D22"], ["Bilanz!D23"]], [["Bilanz!D15"]]),
        ("Schuldendienstdeckungsgrad (V3)", ebitda + [comp("Ertragsteuern", -1)],
         [comp("Zinsen Finanzverbindlichkeiten"), comp("planmäßige Tilgung")], ">=", 1.30, "ratio", 2,
         ebitda_cells + [["GuV!D17"]], [["Finanzierung!G15"], ["Finanzierung!D15"]]),
        ("Eigenkapitalquote (V4)", [comp("Eigenkapital")], [comp("Bilanzsumme")], ">=", 30.0, "percent", 1,
         [["Bilanz!D22"]], [["Bilanz!D15"]]),
        ("Dynamischer Verschuldungsgrad (V4)", [comp("Finanzverbindlichkeiten"), comp("Liquide Mittel", -1)],
         ebitda, "<=", 3.50, "ratio", 2, [["Bilanz!D28"], ["Bilanz!D12"]], ebitda_cells),
    ]
    for name, num, den, op, limit, unit, rnd, num_cells, den_cells in demo:
        cov = NS(name=name, numerator=num, denominator=den, operator=op, limit=limit, unit=unit,
                 rounding_decimals=rnd)
        items = [NS(covenant=name, side=side, index=i, cells=[NS(id=x, sign=1) for x in ids], reason="")
                 for side, lists in (("numerator", num_cells), ("denominator", den_cells))
                 for i, ids in enumerate(lists)]
        r = evaluate(cov, NS(items=items), cells)
        print(f"{r.name}: {_de(r.numerator)} / {_de(r.denominator)} = {r.value_raw:.4f} -> {r.value:g} "
              f"{r.operator} {r.limit:g}  {'eingehalten' if r.met else 'VERLETZT'}{' (knapp)' if r.tight else ''}")
        print(f"    {r.stress_text}")
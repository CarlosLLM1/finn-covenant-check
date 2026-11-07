"""Mapping contract definitions to plan cells: LLM chooses, code checks.

The LLM gets every component of every covenant (with its inclusion/exclusion rules) and the plan
catalog WITHOUT values. It answers with cell ids. Code then checks:
- every component is answered exactly once,
- every id exists,
- no total is chosen together with a cell it is built from (double counting),
- an empty answer has a reason (the component is then 0 and the reply says so).
On errors the LLM gets one second chance with the list of problems.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from finn import llm
from finn.contracts import CACHE_DIR, ContractResult
from finn.plan import PlanCell, catalog

PROMPT = Path("prompts/mapping.txt")


class CellRef(BaseModel):
    id: str           # e.g. "Finanzierung!G15"
    sign: int         # +1 add, -1 subtract


class ComponentCells(BaseModel):
    covenant: str
    side: Literal["numerator", "denominator"]
    index: int        # position of the component in that list (0, 1, 2, ...)
    cells: list[CellRef]
    reason: str       # why these cells (or why none)


class ContractMapping(BaseModel):
    items: list[ComponentCells]


def _components_text(result: ContractResult) -> str:
    lines = []
    for cov in result.covenants:
        for side in ("numerator", "denominator"):
            for i, comp in enumerate(getattr(cov, side)):
                lines.append(f"- covenant={cov.name!r} side={side} index={i}: {comp.description}")
    return "\n".join(lines)


def check_mapping(result: ContractResult, mapping: ContractMapping, cells: dict[str, PlanCell]) -> list[str]:
    """Problems that make the mapping unusable. Empty list = OK."""
    errors = []
    expected = {(c.name, side, i) for c in result.covenants
                for side in ("numerator", "denominator") for i in range(len(getattr(c, side)))}
    seen: dict[tuple, int] = {}
    for item in mapping.items:
        key = (item.covenant, item.side, item.index)
        seen[key] = seen.get(key, 0) + 1
        ids = [c.id for c in item.cells]
        for cid in ids:
            if cid not in cells:
                errors.append(f"{key}: Zelle {cid} gibt es nicht im Katalog.")
        for cid in ids:
            if cid in cells:
                inside = cells[cid].parts & set(ids)
                if inside:
                    errors.append(f"{key}: {cid} ist eine Summe und enthält schon {sorted(inside)} (Doppelzählung).")
        if not ids and not item.reason.strip():
            errors.append(f"{key}: keine Zelle und keine Begründung.")
    for key in expected - set(seen):
        errors.append(f"{key}: Bestandteil wurde nicht zugeordnet.")
    for key, n in seen.items():
        if key not in expected:
            errors.append(f"{key}: diesen Bestandteil gibt es nicht.")
        elif n > 1:
            errors.append(f"{key}: mehrfach zugeordnet.")
    return errors


def map_contract(name: str, result: ContractResult, cells: dict[str, PlanCell]) -> ContractMapping:
    """Ask the LLM (or the cache), check, retry once with the errors."""
    cat = catalog(cells)
    base_prompt = PROMPT.read_text(encoding="utf-8")
    prompt = base_prompt.replace("{components}", _components_text(result)).replace("{catalog}", cat)

    # Cache key: the definitions + the plan's STRUCTURE (labels, not values) + prompt + model.
    # A plan with new numbers but the same layout needs no new LLM call.
    key = hashlib.sha256((prompt + llm.MODEL_READING).encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"mapping_{Path(name).stem}_{key}.json"
    if cache_file.exists():
        mapping = ContractMapping.model_validate_json(cache_file.read_text(encoding="utf-8"))
        if not check_mapping(result, mapping, cells):
            return mapping

    mapping = llm.ask_json(prompt, ContractMapping, model=llm.MODEL_READING)
    errors = check_mapping(result, mapping, cells)
    if errors:  # second chance: tell the LLM what was wrong
        retry = prompt + "\n\nDeine letzte Antwort hatte diese Fehler, korrigiere sie:\n" + "\n".join(errors)
        mapping = llm.ask_json(retry, ContractMapping, model=llm.MODEL_READING)
        errors = check_mapping(result, mapping, cells)
        if errors:
            raise ValueError("Zuordnung zum Wirtschaftsplan fehlerhaft: " + "; ".join(errors))

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")
    return mapping


if __name__ == "__main__":
    # Contracts (from cache) -> LLM picks plan cells -> code checks -> calc.py calculates.
    #   python -m finn.mapping
    from finn.calc import evaluate
    from finn.contracts import read_all_contracts
    from finn.plan import read_plan

    cells = read_plan(Path("data/plan/Wirtschaftsplan_2027.xlsx"))
    for name, (result, warnings) in read_all_contracts().items():
        print(f"\n##### {name}")
        if result is None or not result.has_covenants:
            print("  keine Financial Covenants" if result else f"  {warnings}")
            continue
        mapping = map_contract(name, result, cells)
        for cov in result.covenants:
            r = evaluate(cov, mapping, cells)
            print(f"  {r.name}: {r.numerator:,.0f} / {r.denominator:,.0f} = {r.value_raw:.4f} -> {r.value:g} "
                  f"{r.operator} {r.limit:g}  {'eingehalten' if r.met else 'VERLETZT'}{' (knapp)' if r.tight else ''}")
            for side, lines in (("Zähler", r.num_lines), ("Nenner", r.den_lines)):
                for ln in lines:
                    picked = " ".join(f"{'-' if s < 0 else '+'}{c.id}({c.value:,.0f})" for s, c in ln.cells) or "—"
                    sign = "+" if ln.sign > 0 else "-"
                    w = "" if ln.weight == 1 else f"{ln.weight:g}×"
                    print(f"    {side} {sign}{w} {ln.description[:55]:55} -> {picked}")
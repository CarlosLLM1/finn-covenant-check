"""Reading the loan contracts: PDF -> LLM -> checked covenant definitions.

Steps per contract:
1. Extract the text page by page and mark each page ("=== Seite 3 ===") so the LLM can cite pages.
2. The LLM returns a ContractResult (schema below): covenants, their parts, limit, sources.
3. Code checks the answer: every quote must really be on the cited page, and the limit must
   appear in one of the quotes. Problems are collected as warnings and shown in the reply.
4. The result is cached as JSON, so each contract is read only once (contracts don't change).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Literal, Optional

import pdfplumber
from pydantic import BaseModel

from finn import llm

CONTRACTS_DIR = Path("data/contracts")
CACHE_DIR = Path("cache")
PROMPT = Path("prompts/extract.txt")


# ---------- What the LLM must return (the schema) ----------

class Component(BaseModel):
    description: str          # e.g. "Zinsen und ähnliche Aufwendungen (ohne Aufzinsung von Rückstellungen)"
    sign: int                 # +1 or -1
    weight: float             # e.g. 0.5 for "50 % der Sonderposten"


class Source(BaseModel):
    page: int
    section: str              # e.g. "§ 13 Abs. 1" or "Anlage 3 Abs. 4"
    quote: str                # verbatim from the contract


class Covenant(BaseModel):
    name: str                 # e.g. "Schuldendienstdeckungsgrad"
    numerator: list[Component]
    denominator: list[Component]
    operator: Literal[">=", "<="]
    limit: float
    unit: Literal["percent", "ratio"]
    rounding_decimals: Optional[int]
    plan_basis_note: str
    sources: list[Source]


class ContractResult(BaseModel):
    has_covenants: bool
    reason_if_none: str
    covenants: list[Covenant]


# ---------- Step 1: PDF text with page markers ----------

def pdf_pages(path: Path) -> list[str]:
    """Text per page, without the header/footer lines that repeat on every page
    (e.g. "Kreditvertrag ... Seite 5 von 13"). They would break quotes that run over a page."""
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]

    def shape(line: str) -> str:          # "Seite 5 von 13" and "Seite 6 von 13" look the same
        return "".join("#" if ch.isdigit() else ch for ch in line.strip())

    counts: dict[str, int] = {}
    for p in pages:
        for s in {shape(l) for l in p.splitlines()}:
            counts[s] = counts.get(s, 0) + 1
    repeated = {s for s, n in counts.items() if n > len(pages) / 2 and s}
    return ["\n".join(l for l in p.splitlines() if shape(l) not in repeated) for p in pages]


def marked_text(pages: list[str]) -> str:
    return "\n".join(f"=== Seite {i} ===\n{text}" for i, text in enumerate(pages, start=1))


# ---------- Step 3: checks ----------

def _norm(s: str) -> str:
    """Make text comparable: same quotes and dashes, no line-break hyphens, single spaces."""
    s = unicodedata.normalize("NFKC", s)
    for a, b in [("„", '"'), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")]:
        s = s.replace(a, b)
    s = s.replace("-\n", "")          # word split over two lines: "Finanz-\nverbindlichkeiten"
    return " ".join(s.split()).lower()


def find_quote(quote: str, pages: list[str]) -> list[int]:
    """All page numbers on which the quote appears (also across a page break)."""
    q = _norm(quote)
    hits = [i for i, p in enumerate(pages, start=1) if q in _norm(p)]
    if not hits:  # quote may run over a page break: try each page together with the next
        hits = [i for i in range(1, len(pages)) if q in _norm(pages[i - 1] + "\n" + pages[i])]
    return hits


def _limit_texts(limit: float) -> set[str]:
    """How a limit can be written in German: 1.3 -> {'1,3', '1,30'}; 30 -> {'30', '30,0'}."""
    out = {f"{limit:.2f}".replace(".", ","), f"{limit:.1f}".replace(".", ",")}
    if limit == int(limit):
        out.add(str(int(limit)))
    return out


def check_result(result: ContractResult, pages: list[str]) -> list[str]:
    """Verify the LLM's answer against the PDF. Returns warnings (empty list = all fine).
    Wrong page numbers are corrected in place."""
    warnings = []
    for cov in result.covenants:
        for src in cov.sources:
            hits = find_quote(src.quote, pages)
            if not hits:
                warnings.append(f"{cov.name}: Zitat nicht im Vertrag gefunden: „{src.quote[:80]}…“")
            elif src.page not in hits:
                warnings.append(f"{cov.name}: Zitat steht auf Seite {hits[0]}, nicht {src.page} (korrigiert).")
                src.page = hits[0]
        if not any(t in src.quote for src in cov.sources for t in _limit_texts(cov.limit)):
            warnings.append(f"{cov.name}: Grenzwert {cov.limit} steht in keinem der Zitate.")
        if not cov.numerator or not cov.denominator:
            warnings.append(f"{cov.name}: Zähler oder Nenner ist leer.")
    return warnings


# ---------- Steps 2 + 4: ask the LLM, with cache ----------

def read_contract(path: Path) -> tuple[ContractResult, list[str]]:
    pages = pdf_pages(path)
    prompt = PROMPT.read_text(encoding="utf-8")

    # Cache key: contract content + prompt + model. Change any of them and it is read again.
    key = hashlib.sha256(path.read_bytes() + prompt.encode() + llm.MODEL_READING.encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"{path.stem}_{key}.json"

    if cache_file.exists():
        result = ContractResult.model_validate_json(cache_file.read_text(encoding="utf-8"))
    else:
        filled = prompt.replace("{filename}", path.name).replace("{text}", marked_text(pages))
        result = llm.ask_json(filled, ContractResult, model=llm.MODEL_READING)
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    warnings = check_result(result, pages)
    return result, warnings


def read_all_contracts() -> dict[str, tuple[ContractResult, list[str]]]:
    return {p.name: read_contract(p) for p in sorted(CONTRACTS_DIR.glob("*.pdf"))}


if __name__ == "__main__":
    #   python -m finn.contracts
    for name, (res, warnings) in read_all_contracts().items():
        print(f"\n##### {name}")
        if not res.has_covenants:
            print("  keine Financial Covenants:", res.reason_if_none)
        for cov in res.covenants:
            print(f"  {cov.name}: {cov.operator} {cov.limit} ({cov.unit}, Rundung: {cov.rounding_decimals})")
            for label, parts in (("Zähler", cov.numerator), ("Nenner", cov.denominator)):
                for c in parts:
                    sign = "+" if c.sign > 0 else "-"
                    w = "" if c.weight == 1 else f"{c.weight:g} × "
                    print(f"    {label} {sign} {w}{c.description}")
            for s in cov.sources:
                print(f"    Quelle: S. {s.page}, {s.section}")
        for w in warnings:
            print("  WARNUNG:", w)

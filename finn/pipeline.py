"""The fixed order of the steps: plan -> contracts -> mapping -> calculation.

Code decides the order; the LLM is only called inside contracts.py and mapping.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from finn.calc import CovenantResult, evaluate
from finn.contracts import ContractResult, read_all_contracts
from finn.mapping import map_contract
from finn.plan import read_plan


@dataclass
class ContractReport:
    name: str                                   # file name of the contract
    result: ContractResult | None               # what was read (None = could not be read)
    covenants: list[CovenantResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CheckReport:
    year: int
    plan_file: str
    contracts: list[ContractReport]


def run_check(plan_path: Path, year: int = 2027, verbose: bool = False) -> CheckReport:
    cells = read_plan(plan_path, year)              # raises PlanError if the plan is unusable
    if verbose:
        print("  Wirtschaftsplan gelesen, lese Verträge (Cache) ...", flush=True)
    reports = []
    for name, (result, warnings) in read_all_contracts().items():
        rep = ContractReport(name, result, warnings=list(warnings))
        if result is not None and result.has_covenants:
            try:
                if verbose:
                    print(f"  ordne {name} dem Plan zu ...", flush=True)
                mapping = map_contract(name, result, cells)
                rep.covenants = [evaluate(cov, mapping, cells) for cov in result.covenants]
            except Exception as e:  # one contract failing must not hide the others
                rep.warnings.append(f"Berechnung nicht möglich: {e}")
        reports.append(rep)
    return CheckReport(year, plan_path.name, reports)
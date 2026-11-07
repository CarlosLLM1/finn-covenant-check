"""The reply email (HTML + plain text). Every number comes from calc.py.

The LLM only writes the 2-3 sentence summary, and code checks it: every number in it must
appear in the results. If not (or if the LLM is unavailable), a fixed template is used.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from pydantic import BaseModel

from finn import llm
from finn.calc import CovenantResult
from finn.pipeline import CheckReport, ContractReport

SUMMARY_PROMPT = Path("prompts/summary.txt")


# ---------- German number formatting ----------

def de(x: float, decimals: int = 0) -> str:
    """1234.5 -> '1.234,5' (German style)."""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_value(r: CovenantResult, x: float, decimals: int | None = None) -> str:
    """Value or limit as the contract writes it: 30,7 % / 30,0 %, 1,56 / 1,30."""
    d = decimals if decimals is not None else r.decimals
    return f"{de(x, d)} %" if r.unit == "percent" else de(x, d)


def fmt_headroom(r: CovenantResult) -> str:
    return f"{de(r.headroom, r.decimals)} %-Pkt." if r.unit == "percent" else de(r.headroom, r.decimals)


def op(r: CovenantResult) -> str:
    return "≥" if r.operator == ">=" else "≤"


def status(r: CovenantResult) -> str:
    if not r.met:
        return "NICHT eingehalten"
    return "eingehalten (knapp)" if r.tight else "eingehalten"


def short_name(contract_file: str) -> str:
    """'Vertrag_3_Landesbank_Investitionsdarlehen.pdf' -> 'Vertrag 3 Landesbank Investitionsdarlehen'"""
    return Path(contract_file).stem.replace("_", " ")


# ---------- Summary (LLM, checked) ----------

class Summary(BaseModel):
    text: str


def _all_results(report: CheckReport) -> list[tuple[ContractReport, CovenantResult]]:
    return [(c, r) for c in report.contracts for r in c.covenants]


def verdict(report: CheckReport) -> str:
    """First sentence of the reply, written by code."""
    res = _all_results(report)
    failed = [r for _, r in res if not r.met]
    tight = [r for _, r in res if r.met and r.tight]
    if failed:
        s = f"{len(failed)} von {len(res)} Covenants werden auf Planbasis {report.year} NICHT eingehalten."
    else:
        s = f"Alle {len(res)} Covenants werden auf Planbasis {report.year} eingehalten."
    if tight:
        s += " Knapp: " + ", ".join(r.name for r in tight) + "."
    return s


def template_summary(report: CheckReport) -> str:
    parts = []
    for c, r in _all_results(report):
        parts.append(f"{r.name} ({short_name(c.name)}): {fmt_value(r, r.value)} "
                     f"bei Grenzwert {op(r)} {fmt_value(r, r.limit)}, {status(r)}")
    return "; ".join(parts) + "."


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".,") for n in re.findall(r"\d[\d.,]*", text)}


def llm_summary(report: CheckReport) -> str:
    """LLM writes, code checks every number. Falls back to the template."""
    results = template_summary(report)
    allowed = _numbers(results) | _numbers(verdict(report)) | {str(report.year), str(len(_all_results(report)))}
    for c, r in _all_results(report):
        # accept each number with 0-4 decimals (e.g. 30,7 / 30,70 / 30,6833)
        for x in (r.value, r.value_raw, r.limit, r.headroom):
            allowed |= {de(x, d) for d in range(5)}
    try:
        prompt = SUMMARY_PROMPT.read_text(encoding="utf-8").replace("{results}", results)
        text = llm.ask_json(prompt, Summary, model=llm.MODEL_FAST).text.strip()
        unknown = _numbers(text) - allowed
        if text and not unknown:
            return text
    except Exception:
        pass
    return results


# ---------- Plan source text for one component ----------

def _cells_text(line) -> str:
    if not line.cells:
        return f"nicht im Plan, mit 0 angesetzt ({line.reason})" if line.reason else "nicht im Plan, mit 0 angesetzt"
    parts = []
    for sign, cell in line.cells:
        parts.append(f"{'−' if sign < 0 else ''}{cell.id} „{cell.label[:60]}“ = {de(cell.value)}")
    return " ; ".join(parts)


def _formula(r: CovenantResult) -> str:
    unit = " × 100" if r.unit == "percent" else ""
    rounded = f" → gerundet {fmt_value(r, r.value)}" if r.value != r.value_raw else ""
    return (f"{de(r.numerator)} / {de(r.denominator)}{unit} = {fmt_value(r, r.value_raw, 4)}{rounded} "
            f"{op(r)} {fmt_value(r, r.limit)}")


# ---------- HTML ----------

COLORS = {"eingehalten": "#1a7f37", "eingehalten (knapp)": "#b26b00", "NICHT eingehalten": "#c62828"}


def build_html(report: CheckReport, summary: str) -> str:
    e = html.escape
    out = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:820px">',
        "<p>Hallo,</p>",
        f"<p><b>{e(verdict(report))}</b></p>",
        f"<p>{e(summary)}</p>",
        '<table style="border-collapse:collapse;width:100%;margin:12px 0">',
        "<tr>" + "".join(f'<th style="text-align:left;border-bottom:2px solid #444;padding:6px">{h}</th>'
                         for h in ["Vertrag", "Covenant", "Wert", "Grenzwert", "Headroom", "Status"]) + "</tr>",
    ]
    for c in report.contracts:
        if c.result is not None and not c.result.has_covenants:
            out.append(f'<tr><td style="padding:6px;border-bottom:1px solid #ddd">{e(short_name(c.name))}</td>'
                       f'<td colspan="5" style="padding:6px;border-bottom:1px solid #ddd;color:#666">'
                       f"keine Financial Covenants</td></tr>")
        for r in c.covenants:
            st = status(r)
            out.append(
                "<tr>" + "".join(f'<td style="padding:6px;border-bottom:1px solid #ddd;white-space:{ws}">{v}</td>'
                                for ws, v in zip(["normal", "normal", "nowrap", "nowrap", "nowrap", "nowrap"], [
                    e(short_name(c.name)), e(r.name), e(fmt_value(r, r.value)),
                    e(f"{op(r)} {fmt_value(r, r.limit)}"), e(fmt_headroom(r)),
                    f'<b style="color:{COLORS[st]}">{e(st)}</b>'])) + "</tr>")
        if c.result is None or (c.result.has_covenants and not c.covenants):
            out.append(f'<tr><td style="padding:6px;border-bottom:1px solid #ddd">{e(short_name(c.name))}</td>'
                       f'<td colspan="5" style="padding:6px;border-bottom:1px solid #ddd;color:#c62828">'
                       f"nicht geprüft, siehe Hinweise</td></tr>")
    out.append("</table>")

    out.append("<h3 style='margin-top:24px'>Details und Fundstellen</h3>")
    for c in report.contracts:
        out.append(f"<h4 style='margin-bottom:4px'>{e(short_name(c.name))}</h4>")
        if c.result is not None and not c.result.has_covenants:
            out.append(f"<p>Keine Financial Covenants. {e(c.result.reason_if_none)}</p>")
        for r, cov in zip(c.covenants, c.result.covenants if c.result else []):
            out.append(f"<p style='margin:6px 0'><b>{e(r.name)}</b>: {e(_formula(r))}</p>")
            out.append("<ul style='margin:4px 0'>")
            for side, lines in (("Zähler", r.num_lines), ("Nenner", r.den_lines)):
                for ln in lines:
                    sign = "+" if ln.sign > 0 else "−"
                    w = "" if ln.weight == 1 else f"{de(ln.weight * 100)} % × "
                    out.append(f"<li>{side} {sign} {w}{e(ln.description[:110])}<br>"
                               f"<span style='color:#555'>Plan: {e(_cells_text(ln))}</span></li>")
            out.append("</ul>")
            if r.stress_text:
                out.append(f"<p style='margin:4px 0'>Puffer: {e(r.stress_text)}</p>")
            for s in cov.sources:
                out.append(f"<p style='margin:4px 0;color:#444'>Vertrag S. {s.page}, {e(s.section)}: "
                           f"<i>„{e(s.quote.strip(' „“\"'))}“</i></p>")
            if cov.plan_basis_note:
                out.append(f"<p style='margin:4px 0;color:#444'>Planbasis: {e(cov.plan_basis_note)}</p>")
        for w in c.warnings:
            out.append(f"<p style='margin:4px 0;color:#b26b00'>Hinweis: {e(w)}</p>")

    out.append(
        f"<p style='margin-top:24px;color:#666;font-size:12px'>Grundlage: {e(report.plan_file)}, Planjahr "
        f"{report.year}, Werte in T€. Alle Kennzahlen wurden per Code berechnet; die KI hat nur die Verträge "
        "gelesen, Planzeilen zugeordnet und die Zusammenfassung formuliert. Jedes Vertragszitat wurde gegen "
        "die angegebene PDF-Seite geprüft.</p>")
    out.append("<p>Viele Grüße<br>Finn<br><span style='color:#666'>Digitaler Referent Finanzierung</span></p></div>")
    return "\n".join(out)


def build_text(report: CheckReport, summary: str) -> str:
    lines = ["Hallo,", "", verdict(report), "", summary, ""]
    for c in report.contracts:
        lines.append(short_name(c.name))
        if c.result is not None and not c.result.has_covenants:
            lines.append("  keine Financial Covenants: " + c.result.reason_if_none)
        for r in c.covenants:
            lines.append(f"  {r.name}: {fmt_value(r, r.value)} (Grenzwert {op(r)} {fmt_value(r, r.limit)}, "
                         f"Headroom {fmt_headroom(r)}) - {status(r)}")
            lines.append(f"    Rechnung: {_formula(r)}")
        for w in c.warnings:
            lines.append("  Hinweis: " + w)
        lines.append("")
    lines += ["Details und Fundstellen siehe HTML-Version dieser Mail.", "", "Viele Grüße", "Finn"]
    return "\n".join(lines)


def build_reply(report: CheckReport, use_llm: bool = True) -> tuple[str, str]:
    summary = llm_summary(report) if use_llm else template_summary(report)
    return build_text(report, summary), build_html(report, summary)
"""Understanding an incoming email: the LLM classifies, code decides.

Layers, so that reading the email can never break Finn:
1. Code cleans the text: quoted history and signatures removed, length limited.
2. The LLM (quick mode, 30 s) only fills a fixed schema (kind, plan year, language). The email is passed
   as marked DATA; instructions inside it are ignored and flagged (prompt injection).
3. Code checks the answer (valid year? consistent?) and decides what Finn does. Replies are fixed texts
   written by code, never by the LLM.
4. If the LLM fails, simple rules take over, so Finn always keeps working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from finn import llm

PROMPT = Path("prompts/intake.txt")
MAX_BODY_CHARS = 3000


class EmailIntent(BaseModel):
    """What the LLM must return."""
    kind: Literal["order", "thanks", "question", "other"]
    plan_year: Optional[int]
    language: Literal["de", "en"]
    injection_suspected: bool
    reason: str


@dataclass
class Decision:
    """What Finn does with the email. Decided by code."""
    action: Literal["check", "ask_for_plan", "explain", "ignore"]
    year: Optional[int]          # explicitly requested plan year, if any
    language: str
    source: str                  # "KI" or "Regeln" (fallback), for the log
    note: str                    # short explanation for the log


# ---------- 1. Clean the text ----------

_QUOTE_START = re.compile(
    r"^\s*(am .{0,80} schrieb .{0,80}:|on .{0,80} wrote:|-{2,}\s*(ursprüngliche|original) (nachricht|message)"
    r"|von:\s.+|from:\s.+|gesendet:\s.+|sent:\s.+)", re.IGNORECASE)
_SIGNATURE = re.compile(r"^\s*(--\s*$|viele grüße|mit freundlichen grüßen|beste grüße|best regards|kind regards)",
                        re.IGNORECASE)


def clean_body(body: str) -> str:
    """Only the new part of the email: no quoted history, no signature, limited length."""
    lines = []
    for line in (body or "").replace("\r", "").splitlines():
        if line.lstrip().startswith(">") or _QUOTE_START.match(line):
            break           # everything below is the quoted earlier conversation
        if _SIGNATURE.match(line):
            break           # greeting/signature: nothing important follows
        lines.append(line)
    text = "\n".join(lines).strip()
    return text[:MAX_BODY_CHARS]


# ---------- 2. Rules (fallback when the LLM is not available) ----------

ORDER_WORDS = ("covenant", "prüf", "wirtschaftsplan", "kennzahl", "check", "plan 20")
THANKS_WORDS = ("danke", "vielen dank", "thanks", "thank you", "super", "perfekt", "passt", "top", "great")


def rule_kind(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    b = body.lower()
    # A short "Danke!" in reply to Finn's result has "Re: Covenant-Prüfung ..." in the subject,
    # so thanks are checked first, on the body only
    if len(body) < 300 and any(w in b for w in THANKS_WORDS) and not any(w in b for w in ORDER_WORDS):
        return "thanks"
    if any(w in text for w in ORDER_WORDS):
        return "order"
    if "?" in body:
        return "question"
    return "other"


def explicit_year(text: str) -> Optional[int]:
    """A plan year written in the text, e.g. 'Wirtschaftsplan 2028' or 'Plan_2028.xlsx'."""
    years = re.findall(r"(?<!\d)(20\d\d)(?!\d)", text.replace("_", " "))
    return int(years[0]) if years else None


def valid_year(y: Optional[int]) -> Optional[int]:
    """Only plausible plan years: from 5 years ago to 10 years ahead."""
    if y is None:
        return None
    this = date.today().year
    return y if this - 5 <= y <= this + 10 else None


# ---------- 3. LLM + decision ----------

def classify(subject: str, body: str, attachments: list[str]) -> tuple[EmailIntent | None, str]:
    """Ask the LLM; returns (intent, error). Never raises."""
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{subject}", subject[:300])
              .replace("{attachments}", ", ".join(attachments) or "keine")
              .replace("{body}", body or "(leer)"))
    try:
        intent = llm.ask_json(prompt, EmailIntent, model=llm.MODEL_FAST, quick=True)
        return intent, ""
    except Exception as e:
        return None, str(e)[:120]


def decide(subject: str, body: str, attachments: list[str], use_llm: bool = True) -> Decision:
    """The whole intake: clean, classify (LLM or rules), check, decide."""
    text = clean_body(body)
    has_plan = any(a.lower().endswith((".xlsx", ".xlsm")) for a in attachments)

    intent, error = classify(subject, text, attachments) if use_llm else (None, "LLM aus")
    if intent is not None:
        kind, source = intent.kind, "KI"
        year = valid_year(intent.plan_year)
        language = intent.language
        note = intent.reason
        if intent.injection_suspected:
            note = "Achtung: Mail enthält Anweisungen an die KI (ignoriert). " + note
        # The LLM may only report a year that really appears in subject or text; otherwise it guessed
        if year is not None and str(year) not in f"{subject} {text}":
            note += f" (Jahr {year} nicht im Text, verworfen)"
            year = None
    else:
        kind, source = rule_kind(subject, text), "Regeln"
        year = valid_year(explicit_year(subject) or explicit_year(text))
        language = "en" if re.search(r"\b(please|thanks|check|attached)\b", f"{subject} {text}".lower()) else "de"
        note = f"KI nicht verfügbar ({error})" if use_llm else "Regeln"

    # Code decides. An attached Excel is always checked: it is the strongest signal of an order.
    if has_plan:
        action = "check"
    elif kind == "order":
        action = "ask_for_plan"
    elif kind == "question":
        action = "explain"
    else:  # thanks, other
        action = "ignore"
    return Decision(action, year, language, source, note)


# ---------- 4. Fixed reply texts (written by code, never by the LLM) ----------

TEXTS = {
    "ask_for_plan": {
        "de": ("Hallo,\n\ndanke für die Nachricht. Für die Covenant-Prüfung brauche ich den Wirtschaftsplan als "
               "Excel-Datei (.xlsx) im Anhang. Schick ihn mir gern noch einmal mit Anhang.\n\nViele Grüße\nFinn"),
        "en": ("Hello,\n\nthanks for your message. To check the covenants I need the business plan as an Excel file "
               "(.xlsx) attached. Please send it again with the attachment.\n\nBest regards\nFinn"),
    },
    "explain": {
        "de": ("Hallo,\n\nich bin Finn, digitaler Referent Finanzierung. Ich prüfe die Financial Covenants aus unseren "
               "Kreditverträgen auf Basis eines Wirtschaftsplans. Schick mir dafür einfach den Wirtschaftsplan als "
               "Excel-Datei (.xlsx) im Anhang, z. B. mit dem Betreff „Covenant-Prüfung Wirtschaftsplan 2027“. "
               "Ich antworte mit Ergebnis, Rechenweg und Fundstellen.\n\nAndere Fragen kann ich leider nicht "
               "beantworten.\n\nViele Grüße\nFinn"),
        "en": ("Hello,\n\nI'm Finn, a digital financing analyst. I check the financial covenants in our loan "
               "agreements against a business plan. Just send me the business plan as an Excel file (.xlsx), e.g. "
               "with the subject \"Covenant-Prüfung Wirtschaftsplan 2027\". I reply with the result, the "
               "calculation and the sources.\n\nI can't answer other questions, sorry.\n\nBest regards\nFinn"),
    },
}


def reply_text(action: str, language: str) -> str:
    return TEXTS[action].get(language, TEXTS[action]["de"])

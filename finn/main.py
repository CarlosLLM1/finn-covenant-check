"""Finn's entry point.

  python -m finn.main run data/plan/Wirtschaftsplan_2027.xlsx   check a plan file, write runs/reply.html
  python -m finn.main watch                                     answer every new email (Ctrl+C to stop)
  python -m finn.main once                                      check the inbox one time, then exit
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

from finn import intake, mailer
from finn.pipeline import run_check
from finn.plan import PlanError, plan_years
from finn.report import build_reply

RUNS_DIR = Path("runs")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
ACK_AFTER_SECONDS = int(os.environ.get("ACK_AFTER_SECONDS", "15"))
STATE_FILE = RUNS_DIR / "processed.json"   # which emails got an acknowledgement / a final answer

ACK_TEXT = ("Hallo,\n\nAuftrag erhalten. Ich prüfe die Covenants aus den Kreditverträgen auf Basis des "
            "Wirtschaftsplans und melde mich in wenigen Minuten mit dem Ergebnis.\n\nViele Grüße\nFinn")


# ---------- Remember what was answered (no double replies) ----------

_state_lock = threading.Lock()


def _key(order: mailer.Order) -> str:
    """Stable id of an email: its Message-ID, or sender + subject if a client sent none."""
    return order.message_id or f"{order.sender}|{order.subject}"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _mark(order: mailer.Order, what: str) -> None:
    """Record that `what` ("ack" or "answered") was sent for this email."""
    with _state_lock:
        state = _load_state()
        state.setdefault(_key(order), {})[what] = datetime.now().isoformat(timespec="seconds")
        RUNS_DIR.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _was(order: mailer.Order, what: str) -> bool:
    with _state_lock:
        return what in _load_state().get(_key(order), {})


def _send_ack(order: mailer.Order) -> None:
    """Runs in a timer thread: only if the result is not out yet and no ack was sent before."""
    if _was(order, "ack") or _was(order, "answered"):
        return
    try:
        mailer.send(mailer.build_reply(order, ACK_TEXT))
        _mark(order, "ack")
        print("  Zwischenbescheid gesendet (Prüfung dauert länger)", flush=True)
    except Exception as e:
        print(f"  Zwischenbescheid nicht gesendet: {e}", flush=True)


def year_from(text: str, default: int = 2027) -> int:
    """Plan year from a text, e.g. 'Covenant-Prüfung Wirtschaftsplan 2028' -> 2028."""
    return intake.explicit_year(text) or default


def choose_year(requested: int | None, plan_file: Path) -> tuple[int, str]:
    """Which plan year to check: asked for in the email > in the file name > found in the plan > 2027."""
    if requested:
        return requested, "aus der Mail"
    from_name = intake.valid_year(intake.explicit_year(plan_file.name))
    if from_name:
        return from_name, "aus dem Dateinamen"
    found = plan_years(plan_file)
    if found:
        return found[0], "aus den Planspalten der Datei"
    return 2027, "Standard"


def save(text: str, html: str) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (RUNS_DIR / f"{stamp}_reply.txt").write_text(text, encoding="utf-8")
    path = RUNS_DIR / f"{stamp}_reply.html"
    path.write_text(html, encoding="utf-8")
    return path


def _answer(order: mailer.Order, text: str, html: str | None = None) -> None:
    """Send the final answer and remember it, so this email is never answered twice."""
    mailer.send(mailer.build_reply(order, text, html))
    _mark(order, "answered")


def handle(order: mailer.Order) -> None:
    """One email: decide, check, reply. Every email gets exactly one final answer."""
    print(f"[{datetime.now():%H:%M:%S}] Mail von {order.sender}: {order.subject!r}", flush=True)
    if _was(order, "answered"):
        print("  wurde schon beantwortet: übersprungen", flush=True)
        return

    # 1. Understand the email (LLM classifies, code decides, rules as fallback)
    decision = intake.decide(order.subject, order.body, [a.name for a in order.attachments])
    print(f"  Einordnung ({decision.source}): {decision.action}, Jahr {decision.year or '-'}, "
          f"{decision.language} – {decision.note}", flush=True)

    if decision.action == "ignore":
        _mark(order, "answered")   # nothing to answer (e.g. "Danke!"), but don't look at it again
        print("  keine Antwort nötig", flush=True)
        return
    if decision.action in ("ask_for_plan", "explain"):
        _answer(order, intake.reply_text(decision.action, decision.language))
        print(f"  Antwort gesendet: {decision.action}", flush=True)
        return

    # 2. Check the plan
    year, why = choose_year(decision.year, order.attachments[0])
    print(f"  Planjahr {year} ({why})", flush=True)
    # If the check takes long (the AI has to work), send a short acknowledgement first
    ack_timer = threading.Timer(ACK_AFTER_SECONDS, _send_ack, args=(order,))
    ack_timer.daemon = True
    ack_timer.start()
    try:
        report = run_check(order.attachments[0], year, verbose=True)
        text, html = build_reply(report)
        ack_timer.cancel()
        save(text, html)
        _answer(order, text, html)
        print("  Ergebnis gesendet", flush=True)
    except PlanError as e:
        ack_timer.cancel()
        text = (f"Hallo,\n\nich konnte den Wirtschaftsplan nicht auswerten:\n{e}\n\n"
                "Bitte prüfe die Datei und schick sie erneut.\n\nViele Grüße\nFinn")
        _answer(order, text)
        print(f"  Plan nicht auswertbar: {e}", flush=True)
    finally:
        ack_timer.cancel()


def watch(once: bool = False) -> None:
    print(f"Finn läuft. Postfach: {mailer.FINN_EMAIL}. Prüfe alle {POLL_SECONDS} s. Stop mit Strg+C.", flush=True)
    while True:
        try:
            for order in mailer.fetch_unread():
                try:
                    handle(order)
                    mailer.mark_as_read(order.imap_id)   # only after the reply was sent
                except Exception:
                    # Leave the mail unread: it is tried again on the next poll
                    traceback.print_exc()
                    print("  Fehler, die Mail bleibt ungelesen und wird erneut versucht.", flush=True)
        except Exception as e:  # e.g. no internet for a moment
            print(f"Postfach nicht erreichbar: {e}", flush=True)
        if once:
            return
        time.sleep(POLL_SECONDS)


def run_file(path: Path) -> None:
    year, why = choose_year(None, path)
    print(f"Planjahr {year} ({why})")
    report = run_check(path, year, verbose=True)
    text, html = build_reply(report)
    out = save(text, html)
    print("\n" + text)
    print(f"\nHTML-Antwort: {out}")
    webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "run":
        run_file(Path(sys.argv[2]))
    elif cmd == "once":
        watch(once=True)
    else:
        watch()

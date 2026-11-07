# Finn – covenant check by email

Finn is an email agent for a municipal utility (Stadtwerk). The head of finance sends Finn the business plan
(`Wirtschaftsplan_2027.xlsx`). Finn checks the financial covenants of four loan agreements against the plan
and replies in the same thread with the result, the calculation and the source of every number.

> **Core rule: the AI reads, maps and writes. Code calculates, checks and decides.**
> Every ratio is computed in `finn/calc.py`. The AI never sees a plan value.

## Result for Plan 2027 (test date 31.12.2027)

| Contract | Covenant | Value | Limit | Headroom | Status |
|---|---|---|---|---|---|
| 1 Sparkasse | no financial covenants (the preamble ratios are historical only) | – | – | – | – |
| 2 Förderbank | Wirtschaftliche Eigenkapitalquote | 34.32 % | ≥ 27.5 % | 6.82 pp | met |
| 3 Landesbank | Schuldendienstdeckungsgrad (DSCR) | 1.56 | ≥ 1.30 | 0.26 | met |
| 4 Syndicated loan | Eigenkapitalquote | 30.7 % | ≥ 30.0 % | 0.7 pp | met, tight |
| 4 Syndicated loan | Dynamischer Verschuldungsgrad | 3.37 | ≤ 3.50 | 0.13 | met, tight |

## Setup (about 10 minutes)

Requirements: Python 3.11+, a Gemini API key (aistudio.google.com), a Gmail account for Finn with an app password.

```powershell
git clone <REPO-URL>
cd my-finn
python -m venv .venv
.venv\Scripts\Activate.ps1            # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env                # then fill in .env
```

`.env`:

| Setting | Meaning |
|---|---|
| `FINN_EMAIL`, `FINN_APP_PASSWORD` | Finn's mailbox. App password: Google account → 2-Step Verification on → myaccount.google.com/apppasswords |
| `GEMINI_API_KEY` | key from aistudio.google.com |
| `GEMINI_MODEL_READING` | model for reading contracts and mapping (e.g. `gemini-3.7-flash`) |
| `GEMINI_MODEL_FAST` | model for the email intake and the summary |
| `GEMINI_MODEL_FALLBACK` | used automatically when the models above are overloaded |
| `POLL_SECONDS` | how often the inbox is checked (default 20) |

Gmail: add a filter "To: Finn's address → never send to spam" (new accounts often file mail as spam).

## Running

| Command | What happens |
|---|---|
| `python -m finn.main watch` | **Live mode.** Checks the inbox every 20 s and answers every new email. Stop with Ctrl+C. |
| `python -m finn.main once` | Checks the inbox once, then exits. |
| `python -m finn.main run data/plan/Wirtschaftsplan_2027.xlsx` | One check without email; the reply opens in the browser. |
| `python -m finn.contracts` | Shows how the AI read the four contracts. |
| `python -m finn.mapping` | Shows which plan cells the AI chose for each part of each covenant. |

The folder `cache/` contains the checked contract readings and mappings, so a fresh clone answers in seconds.
Delete it to have everything read fresh (takes a few minutes).

## How it works

```
email ──► mailer.py    read unread mail (IMAP), save the .xlsx, ignore own/auto/bulk mail
      ──► intake.py    AI classifies the email (order / thanks / question / other); code decides
      ──► plan.py      Excel → catalog of plan cells (no AI); plan must be complete and consistent
      ──► contracts.py AI reads each contract → covenant definitions as JSON (cached)
                       code: every quote must be on its cited PDF page, the limit must be in a quote
      ──► mapping.py   AI picks plan cells for each part of a definition, WITHOUT seeing values (cached)
                       code: cells exist, no double counting, every part mapped
      ──► calc.py      code: ratio, rounding as the contract says, headroom, buffer
      ──► report.py    code builds verdict + table + sources; AI writes 2–3 summary sentences,
                       code checks every number in them (otherwise a fixed template)
      ──► mailer.py    reply in the same thread (SMTP)
```

| File | Job | AI? |
|---|---|---|
| `finn/mailer.py` | IMAP/SMTP, threading, loop protection | – |
| `finn/intake.py` + `prompts/intake.txt` | understand the email, decide what to do | classifies only |
| `finn/plan.py` | read the business plan, check it | – |
| `finn/contracts.py` + `prompts/extract.txt` | read the contracts, verify quotes | yes |
| `finn/mapping.py` + `prompts/mapping.txt` | contract terms → plan cells, check | yes |
| `finn/calc.py` | all arithmetic | **never** |
| `finn/report.py` + `prompts/summary.txt` | reply email | summary sentences only |
| `finn/pipeline.py` | the fixed order of the steps | – |
| `finn/main.py` | `watch` / `once` / `run`, acknowledgement, no double replies | – |
| `finn/llm.py` | the only place that talks to Gemini: JSON schema, retries, fallback model | – |

## Why you can trust the numbers

| Risk | Guard in code |
|---|---|
| The AI invents or changes a number | The AI never sees plan values; `calc.py` computes everything. |
| A quote is invented or on another page | Every quote is searched on its cited PDF page; a wrong page is corrected and noted. |
| The limit comes from the wrong clause | The limit must appear in one of the quotes, otherwise a note in the reply. |
| A plan line is counted twice | A total may not be chosen together with a cell it is built from (formula references). |
| Rounding | Each contract's own rule, commercial rounding with `Decimal` (Python's `round` would round 2.345 to 2.34). |
| The plan is inconsistent | Balance sheet must balance, "Kontrolle" rows must be 0; otherwise Finn explains and does not calculate. |
| The summary contradicts the table | Every number in the AI's sentences must appear in the results; otherwise a fixed template. |
| Instructions hidden in an email ("ignore your rules …") | The AI only fills a fixed schema; code decides; all replies are fixed texts. |

## Robustness

- **Every email gets one final answer** (or none for "Danke!", newsletters, auto-replies). Answered emails are
  recorded in `runs/processed.json`, so Finn never replies twice, even after a restart.
- **An email is marked as read only after the reply was sent.** If anything crashes, it is tried again.
- **Slow checks get an acknowledgement**: if a check takes longer than 15 s (the AI has to work), Finn first
  replies "Auftrag erhalten …".
- **Gemini overloaded (503/504)**: retries after 5, 15 and 30 s, then the fallback model. The email intake has
  a 30 s limit and falls back to simple rules, so Finn always keeps working.
- **Different Excel layouts**: labels in any column, headers over several rows, the year as a number or date,
  units in EUR / T€ / Mio. € (converted), sheets recognised by their content, not by their name.
- **Bad files are refused with a clear reason**: not a business plan, wrong year, balance sheet missing,
  inconsistent, corrupt.
- **Plan year**: from the email, else from the file name, else from the plan columns in the file.

## Tests (no internet, no API key)

```powershell
python -m tests.test_calc            # the 4 covenants vs. the hand calculation, trap values must not appear
python -m tests.test_pipeline        # whole pipeline with a fake AI, incl. the reply email
python -m tests.test_plan_variants   # 6 differently built Excel files: same results or clear refusal
python -m tests.test_intake          # email understanding, prompt injection, fallback without AI
```

Test files in `tests/data/`: a plan with a different layout, one with a **breached covenant**
(`Wirtschaftsplan_2027_Covenant_verletzt.xlsx`), and five broken files. They can also be sent to Finn by email.

## Known limitations

- Only German-language plans and contracts (the sheet-role words are German).
- Years as columns, not rows; contracts as text PDFs (no OCR for scans).
- Excel files must have been saved by Excel (formulas need their saved values).
- The buffer ("how far may it get worse") changes one side of the ratio and keeps the other fixed.
- Before a breach is sent to a bank, a human should approve it; a new plan layout's mapping should be checked once.

More detail: `docs/HANDBOOK.md`. Assumptions and decisions: `NOTE.md`. Time spent: `TIMELOG.md`.

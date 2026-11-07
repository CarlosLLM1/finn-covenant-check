# Note – Finn covenant check

## Assumptions
- Check on Plan 2027, test date 31.12.2027, values in T€. If an email asks for another year, Finn checks that
  year; otherwise it takes the year from the file name or the plan columns.
- On plan figures, a breach is an early warning, not a default (contracts 2–4 say so); the reply quotes this.
- Each contract is applied literally: contract 3 counts the subordinated shareholder loan in debt service
  ("regardless of Rangrücktritt") and excludes the Sondertilgung and the Aufzinsung; contract 4 excludes the
  shareholder loan from debt and Sonderposten from equity. Rounding only where the contract has a rule
  (contract 2 has none).
- Items the plan has no line for (extraordinary items, finance leases, unpaid capital) count as 0, and the
  reply says so.
- Contract 1's ratios in the preamble describe 2019 and are not obligations, so it has no covenants.

## What I changed about the specification, and why
- **Fixed pipeline instead of a free agent.** The steps are always the same, so code sets the order and the
  AI is used at four points only (email, contracts, mapping, summary). That makes runs reproducible and testable.
- **The AI never sees plan values.** It only chooses which cells match a contract term; `calc.py` calculates.
  Every AI answer is checked by code: quotes must be on their cited PDF page, no double counting, every number
  in the summary must be in the results.
- **The AI understands emails but never decides.** It classifies (order, thanks, question, other); code
  decides, and all reply texts are fixed. A "Danke!" gets no reply, and instructions hidden in an email can't
  make Finn do anything else.
- **Refuse instead of guess.** A file that isn't a usable plan (wrong year, missing balance sheet,
  inconsistent, corrupt) gets a clear explanation instead of numbers.
- **Additions:** a buffer per covenant ("EBITDA may fall by 1,457 T€"), an acknowledgement when a check takes
  longer than 15 s, protection against double replies, and a cache so the live demo doesn't depend on the AI.

## How I used AI
- **For building:** I discussed the approach with a friend who had solved a similar task, then built my own, simpler version step by step with Claude as a coding assistant. I ran every step myself, checked the results against my own calculation of all four covenants, and made the design
  decisions (e.g. letting the AI do the mapping so the solution works for other Excel layouts).
- **Inside Finn:** Google Gemini reads the contracts, maps definitions to plan cells, classifies emails and
  writes the 2–3 summary sentences. Real problems during testing shaped the code: Gemini was often overloaded
  (503/504), so Finn retries and switches to a fallback model; a wrong Excel file was at first not recognised,
  so the plan reader now refuses it.
- **Result:** the AI read all four contracts correctly (all quotes verified) and the mapping gave the same
  values as my hand calculation, partly via different but equivalent cells.

## What I would build next
- Cross-checks between the sheets (e.g. bank debt in the balance sheet = loan schedule) and plausibility
  ranges for results; mapping twice and comparing for new layouts.
- English-language plans, all plan years at once, OCR for scanned contracts.
- Running Finn on a server instead of a laptop, with a log per email.

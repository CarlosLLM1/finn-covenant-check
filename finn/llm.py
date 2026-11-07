"""The only place that talks to the language model (Google Gemini).

Every call asks for JSON that matches a Pydantic schema, so the answer is structured data
that code can check, never free text that code has to guess from.

Robustness: models are sometimes overloaded (503) or rate-limited (429). We then wait with
growing pauses, and after that switch to a fallback model, so a busy model doesn't stop Finn.
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(override=True)  # .env wins over old values left in the terminal


def _model(var: str) -> str:
    """Model name from .env, cleaned of quotes, spaces and a 'models/' prefix."""
    name = os.environ.get(var, "").strip().strip('"').strip("'").strip()
    return name.removeprefix("models/")


API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL_READING = _model("GEMINI_MODEL_READING")    # strong model for the contracts
MODEL_FAST = _model("GEMINI_MODEL_FAST")          # cheap model for small tasks
MODEL_FALLBACK = _model("GEMINI_MODEL_FALLBACK")  # used when the others are overloaded

WAITS = [5, 15, 30]          # seconds between attempts on the same model
TIMEOUT_MS = 240_000         # give up on a single call after 4 minutes
QUICK_TIMEOUT_MS = 30_000    # quick mode (e.g. reading an email): 30 s per call, no waiting between attempts


def _is_temporary(error: Exception) -> bool:
    """Overloaded, rate limit, timeout or network: worth trying again."""
    text = str(error)
    return any(code in text for code in ("503", "429", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                                         "timed out", "Timeout", "Connection"))


def ask_json(prompt: str, schema: type[BaseModel], model: str, quick: bool = False) -> BaseModel:
    """Send `prompt`, get back an instance of `schema`.
    quick=True: one short attempt per model, no waiting. For steps that have a rule-based fallback."""
    from google import genai
    from google.genai import types

    if not API_KEY or not model:
        raise RuntimeError("GEMINI_API_KEY and the model names must be set in .env")

    timeout = QUICK_TIMEOUT_MS if quick else TIMEOUT_MS
    client = genai.Client(api_key=API_KEY, http_options=types.HttpOptions(timeout=timeout))
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,   # Gemini must answer in exactly this structure
        temperature=0,            # as deterministic as possible
    )

    models = [model] + ([MODEL_FALLBACK] if MODEL_FALLBACK and MODEL_FALLBACK != model else [])
    last_error: Exception | None = None
    for m in models:
        for attempt, wait in enumerate([0] + ([] if quick else WAITS)):
            if wait:
                print(f"    {m}: {last_error.__class__.__name__}, neuer Versuch in {wait} s ...", flush=True)
                time.sleep(wait)
            try:
                print(f"    frage {m} ...", flush=True)
                response = client.models.generate_content(model=m, contents=prompt, config=config)
                if response.parsed is not None:
                    return response.parsed
                return schema.model_validate_json(response.text)
            except Exception as e:
                last_error = e
                if not _is_temporary(e):
                    break  # e.g. wrong model name: retrying the same won't help
        print(f"    {m} nicht verfügbar: {str(last_error)[:120]}", flush=True)
    raise RuntimeError(f"LLM call failed on {models}: {last_error}")

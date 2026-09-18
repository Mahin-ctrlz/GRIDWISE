"""
interpreter.py - Pluggable LLM interpretation.

Two paths:
  * LLM_PROVIDER=openai  + OPENAI_API_KEY set     -> OpenAI Structured Outputs.
  * otherwise                                       -> deterministic regex/rule parser
                                                       that handles every canonical
                                                       example in the spec.

Both paths emit the same raw JSON envelope:
    {"interpretations": [ {note_index, applies, directive_type,
                           structured_adjustment, explanation}, ... ] }

Downstream guardrail (`app.directives.validator`) hardens each entry before the
solver sees it.
"""
from __future__ import annotations
import json
import logging
import os
import re
from typing import List, Dict, Any

from app.schemas import Battery
from app.llm.prompts import SYSTEM_PROMPT, JSON_SCHEMA
from app.directives.normalizer import (
    parse_hour_window,
    parse_factor,
    parse_percentage_of_capacity,
    parse_max_grid_kwh,
)

log = logging.getLogger(__name__)

# Keywords that suggest an energy-system directive. Anything else -> no_op.
_ENERGY_KEYWORDS = (
    "solar", "pv", "panel", "battery", "reserve", "grid",
    "charge", "discharge", "tariff", "kw", "kwh", "noon", "midnight",
    "am", "pm", "hour",
)


def _looks_relevant(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _ENERGY_KEYWORDS)


def _deterministic_parse(
    notes: List[str],
    battery: Battery,
) -> Dict[str, Any]:
    """
    Rule-based fallback. Always returns one interpretation per note.
    Recognizes:
      solar_reduction       -> factor / hours
      minimum_battery_reserve -> hours + minimum_energy_kwh (handles 'X% of capacity')
      no_charge_window
      no_discharge_window
      max_grid_window       -> hours + max_grid_kwh
    """
    out: List[Dict[str, Any]] = []
    for idx, raw in enumerate(notes):
        note = (raw or "").strip()
        if not note or not _looks_relevant(note):
            out.append({
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "note unrelated to energy system",
            })
            continue

        t = note.lower()
        hours = parse_hour_window(note)

        # ---------- no_charge / no_discharge ----------
        if re.search(r"\bno\s*charge|don'?t\s*charge|cannot\s*charge|forbid\s*charge", t):
            if not hours:
                hours = list(range(24))
            out.append({
                "note_index": idx,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": f"charging disabled for hours {hours}",
            })
            continue

        if re.search(r"\bno\s*discharge|don'?t\s*discharge|cannot\s*discharge|forbid\s*discharge", t):
            if not hours:
                hours = list(range(24))
            out.append({
                "note_index": idx,
                "applies": True,
                "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": f"discharging disabled for hours {hours}",
            })
            continue

        # ---------- max_grid ----------
        if re.search(r"\b(max|cap|limit)\s*grid\b", t) or "grid cap" in t:
            cap = parse_max_grid_kwh(note)
            if cap is not None:
                if not hours:
                    hours = list(range(24))
                out.append({
                    "note_index": idx,
                    "applies": True,
                    "directive_type": "max_grid_window",
                    "structured_adjustment": {"hours": hours, "max_grid_kwh": cap},
                    "explanation": f"grid capped at {cap} kWh for hours {hours}",
                })
                continue

        # ---------- minimum_battery_reserve ----------
        if re.search(r"\b(reserve|minimum|min)\b.*\b(battery|storage|soc)\b", t) \
                or "reserve" in t or "minimum" in t:
            if "solar" in t or "pv" in t or "panel" in t:
                pass                # let it fall through to solar_reduction handling
            else:
                pct_kwh = parse_percentage_of_capacity(note, battery.capacity_kwh)
                if pct_kwh is not None or "reserve" in t or "minimum" in t:
                    if not hours:
                        hours = list(range(24))
                    min_kwh = pct_kwh if pct_kwh is not None else battery.minimum_energy_kwh
                    out.append({
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "minimum_battery_reserve",
                        "structured_adjustment": {
                            "hours": hours,
                            "minimum_energy_kwh": float(min_kwh),
                        },
                        "explanation": f"battery reserve >= {min_kwh} kWh for hours {hours}",
                    })
                    continue

        # ---------- solar_reduction ----------
        if "solar" in t or "pv" in t or "panel" in t:
            factor = parse_factor(note)
            if factor is not None and not hours:
                hours = list(range(24))
            if factor is not None:
                out.append({
                    "note_index": idx,
                    "applies": True,
                    "directive_type": "solar_reduction",
                    "structured_adjustment": {
                        "hours": hours,
                        "factor": float(factor),
                    },
                    "explanation": f"solar usable fraction = {factor:.2f} for hours {hours}",
                })
                continue

        # Default: no_op if nothing matched
        out.append({
            "note_index": idx,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "no matching directive pattern",
        })

    return {"interpretations": out}


def _openai_parse(notes: List[str], battery: Battery, model: str) -> Dict[str, Any]:
    """Call OpenAI with Structured Outputs."""
    from openai import OpenAI

    client = OpenAI()
    user_payload = {
        "notes": notes,
        "battery": {
            "capacity_kwh": battery.capacity_kwh,
            "minimum_energy_kwh": battery.minimum_energy_kwh,
            "initial_energy_kwh": battery.initial_energy_kwh,
            "max_charge_kwh_per_hour": battery.max_charge_kwh_per_hour,
            "max_discharge_kwh_per_hour": battery.max_discharge_kwh_per_hour,
        },
        "instructions": (
            "Return exactly len(notes) interpretations, in the same order. "
            "Use 'note_index' starting at 0. Convert relative phrases (e.g. '50% "
            "of capacity') into absolute kWh using battery.capacity_kwh."
        ),
    }

    completion = client.chat.completions.create(
        model=model,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "directive_interpretation",
                "schema": JSON_SCHEMA,
                "strict": True,
            },
        },
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(user_payload)},
        ],
        temperature=0,
    )
    text = completion.choices[0].message.content or "{}"
    return json.loads(text)


def interpret(notes: List[str], battery: Battery) -> Dict[str, Any]:
    """
    Returns the raw envelope {"interpretations": [...]} from whichever path is active.
    Never raises - failures degrade to deterministic parser or no_op envelopes.
    """
    provider = os.getenv("LLM_PROVIDER", "auto").lower()
    has_key = bool(os.getenv("OPENAI_API_KEY"))

    use_openai = provider == "openai" or (provider == "auto" and has_key)

    if use_openai:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        try:
            return _openai_parse(notes, battery, model)
        except Exception as exc:
            log.warning("LLM provider failed (%s); falling back to deterministic parser", exc)

    return _deterministic_parse(notes, battery)

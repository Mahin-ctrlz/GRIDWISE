"""
prompts.py - System prompt + JSON schema for OpenAI Structured Outputs.
"""
from __future__ import annotations

SYSTEM_PROMPT = """You translate energy system operator notes into structured directives.
You operate on a 24-hour microgrid with battery, solar PV, and the utility grid.

TIME CONVENTIONS
- Times are 24h. "1 PM" = 13, "12 AM" = 0, "12 PM" = 12.
- Time windows are START-INCLUSIVE and END-EXCLUSIVE.
  "1 PM to 3 PM"      -> hours [13, 14]
  "6 PM until 9 PM"   -> hours [18, 19, 20]
  "from 10pm to 1am"  -> hours [22, 23, 0]
- "noon" = 12, "midnight" = 0.

DIRECTIVE TYPES
- solar_reduction:       { hours: [int], factor: float in (0,1] }
                          factor is the USABLE fraction of original solar.
                          "reduce solar by 80%"            -> factor = 0.2
                          "usable solar should be 25%"     -> factor = 0.25
                          "cut solar in half"              -> factor = 0.5
- minimum_battery_reserve:
                        { hours: [int], minimum_energy_kwh: number }
                          Convert relative phrases using battery capacity:
                          "reserve 50% of capacity"   -> hours[] + 0.5 * capacity_kwh
- no_charge_window:     { hours: [int] }     - battery charging disabled in these hours
- no_discharge_window:  { hours: [int] }     - battery discharging disabled in these hours
- max_grid_window:      { hours: [int], max_grid_kwh: number >= 0 }
- no_op:                applies=false, structured_adjustment=null

GENERAL RULES
- One directive per operator note (one entry of "interpretations" per note).
- Hours must be unique integers in [0, 23] and sorted ascending.
- If a note is unrelated to solar/battery/grid scheduling (e.g. "remind the team
  lunch is at noon"), return applies=false with directive_type=no_op and
  structured_adjustment=null.
- Output JSON ONLY. No commentary. No backticks.
"""

# Schema for OpenAI Structured Outputs (response_format json_schema).
# Mirrors DirectiveInterpretation with one entry per note.
JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "note_index":      {"type": "integer", "minimum": 0, "maximum": 2},
                    "applies":         {"type": "boolean"},
                    "directive_type":  {
                        "type": "string",
                        "enum": [
                            "solar_reduction",
                            "minimum_battery_reserve",
                            "no_charge_window",
                            "no_discharge_window",
                            "max_grid_window",
                            "no_op",
                        ],
                    },
                    "structured_adjustment": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "hours": {
                                        "type": "array",
                                        "items": {"type": "integer", "minimum": 0, "maximum": 23},
                                        "minItems": 1,
                                        "uniqueItems": True,
                                    },
                                    "factor":   {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                                    "minimum_energy_kwh": {"type": "number", "minimum": 0},
                                    "max_grid_kwh":       {"type": "number", "minimum": 0},
                                },
                            },
                        ],
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["note_index", "applies", "directive_type", "explanation"],
            },
        },
    },
    "required": ["interpretations"],
}

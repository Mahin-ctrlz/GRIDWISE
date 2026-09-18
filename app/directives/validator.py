"""
validator.py - Hard guardrails. Anything out-of-bounds collapses to no_op.

The whole point: the LLM output is untrusted. After parsing we sweep through
every directive and either (a) certify it for downstream use, or (b) override
it with a no_op so the solver stays safe.
"""
from __future__ import annotations
from typing import Dict, Any, List

from app.schemas import (
    Battery,
    DirectiveType,
    DirectiveInterpretation,
)


TOL = 1e-6


def _hours_ok(hours: List[int]) -> bool:
    """
    PRD §7 guardrail: hours must be:
      - non-empty, <= 24 entries
      - each an integer in [0, 23]
      - unique (no duplicates)
      - ascending (sorted) — LLM may emit unsorted, we still certify only when sorted
        (the solver re-sorts defensively anyway)
    """
    if not hours or len(hours) > 24:
        return False
    if any((not isinstance(h, int)) or h < 0 or h > 23 for h in hours):
        return False
    if len(set(hours)) != len(hours):           # unique
        return False
    if hours != sorted(hours):                  # ascending
        return False
    return True


# Anything >= this is treated as "infinite" by the guardrail (PRD §7).
_GRID_CAP_MAX = 1e15


def _no_op(note_index: int, reason: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type=DirectiveType.NO_OP,
        structured_adjustment=None,
        explanation=f"[guardrail] directive ignored -> no_op: {reason}",
    )


def validate(
    raw: Dict[str, Any],
    note_index: int,
    battery: Battery,
) -> DirectiveInterpretation:
    """
    `raw` is the parsed JSON object representing one directive.
    Returns a guaranteed-valid DirectiveInterpretation (no_op on any failure).
    """
    try:
        dtype = raw.get("directive_type")
        applies = bool(raw.get("applies", True))
        adj = raw.get("structured_adjustment")
        explanation = raw.get("explanation") or ""

        if not isinstance(dtype, str):
            return _no_op(note_index, "missing directive_type")

        # Unknown / not-an-enum values fall to no_op
        try:
            dtype_e = DirectiveType(dtype)
        except ValueError:
            return _no_op(note_index, f"unknown directive_type '{dtype}'")

        # Distractor: not applicable or no_op from the model
        if (not applies) or dtype_e == DirectiveType.NO_OP:
            return DirectiveInterpretation(
                note_index=note_index,
                applies=False,
                directive_type=DirectiveType.NO_OP,
                structured_adjustment=None,
                explanation=explanation or "no_op",
            )

        # -------- Type-specific checks --------
        if dtype_e == DirectiveType.SOLAR_REDUCTION:
            if not isinstance(adj, dict):
                return _no_op(note_index, "solar_reduction missing adjustment")
            hours = adj.get("hours") or []
            factor = adj.get("factor")
            if not _hours_ok(hours):
                return _no_op(note_index, "solar_reduction bad hours")
            # PRD §7: factor is "between 0 and 1" -> inclusive both ends.
            # factor == 0 is allowed (zero usable) per §5 ("usable fraction remaining").
            if (not isinstance(factor, (int, float))
                    or factor < 0.0 or factor > 1.0):
                return _no_op(note_index, "solar_reduction factor out of [0,1]")
            return DirectiveInterpretation(
                note_index=note_index,
                applies=True,
                directive_type=dtype_e,
                structured_adjustment={"hours": sorted(set(hours)), "factor": float(factor)},
                explanation=explanation or f"solar reduced to {factor*100:.0f}%",
            )

        if dtype_e == DirectiveType.MINIMUM_BATTERY_RESERVE:
            if not isinstance(adj, dict):
                return _no_op(note_index, "minimum_battery_reserve missing adjustment")
            hours = adj.get("hours") or []
            min_kwh = adj.get("minimum_energy_kwh")
            if not _hours_ok(hours):
                return _no_op(note_index, "minimum_battery_reserve bad hours")
            # PRD §7: reserve is finite and non-negative, does not exceed capacity.
            if (not isinstance(min_kwh, (int, float))
                    or min_kwh != min_kwh           # NaN
                    or min_kwh < 0
                    or min_kwh >= _GRID_CAP_MAX    # non-finite
                    or min_kwh > battery.capacity_kwh + TOL):
                return _no_op(note_index, "minimum_battery_reserve kwh not finite or out of bounds")
            return DirectiveInterpretation(
                note_index=note_index,
                applies=True,
                directive_type=dtype_e,
                structured_adjustment={
                    "hours": sorted(set(hours)),
                    "minimum_energy_kwh": float(min_kwh),
                },
                explanation=explanation or f"reserve >= {min_kwh} kWh",
            )

        if dtype_e in (DirectiveType.NO_CHARGE_WINDOW, DirectiveType.NO_DISCHARGE_WINDOW):
            if not isinstance(adj, dict):
                return _no_op(note_index, f"{dtype_e.value} missing adjustment")
            hours = adj.get("hours") or []
            if not _hours_ok(hours):
                return _no_op(note_index, f"{dtype_e.value} bad hours")
            return DirectiveInterpretation(
                note_index=note_index,
                applies=True,
                directive_type=dtype_e,
                structured_adjustment={"hours": sorted(set(hours))},
                explanation=explanation or f"{dtype_e.value} hours applied",
            )

        if dtype_e == DirectiveType.MAX_GRID_WINDOW:
            if not isinstance(adj, dict):
                return _no_op(note_index, "max_grid_window missing adjustment")
            hours = adj.get("hours") or []
            cap = adj.get("max_grid_kwh")
            if not _hours_ok(hours):
                return _no_op(note_index, "max_grid_window bad hours")
            # PRD §7: "grid cap is finite and non-negative".
            # finite  -> reject NaN, +inf, values >= _GRID_CAP_MAX
            # nneg    -> reject < 0
            if (not isinstance(cap, (int, float))
                    or cap != cap                  # NaN
                    or cap < 0
                    or cap >= _GRID_CAP_MAX):     # non-finite (or absurdly large)
                return _no_op(note_index, "max_grid_window cap not finite or negative")
            return DirectiveInterpretation(
                note_index=note_index,
                applies=True,
                directive_type=dtype_e,
                structured_adjustment={"hours": sorted(set(hours)), "max_grid_kwh": float(cap)},
                explanation=explanation or f"grid capped {cap} kWh",
            )

        return _no_op(note_index, "unhandled directive_type")

    except Exception as exc:                # never let a bad LLM crash the pipeline
        return _no_op(note_index, f"exception during validation: {type(exc).__name__}")

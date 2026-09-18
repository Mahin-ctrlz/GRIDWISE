"""
replay.py - Independent verification of the solver output.

Re-derives every constraint from the raw hourly arrays. If anything is off,
raises ValueError so the API layer can return a sanitized 500.

Also recomputes the three aggregate totals from the arrays themselves so we
guarantee the 0.01 absolute tolerance contract.
"""
from __future__ import annotations
from typing import List, Dict, Any
from app.schemas import (
    Hour,
    Battery,
    DirectiveType,
    DirectiveInterpretation,
    HourlyEntry,
    BatteryAction,
)

TOL = 0.01           # canonical tolerance
SOFT = 1e-3          # slack for floating-point in replay checks


def _adj_get(adj, key, default=None):
    """Read a key from either a Pydantic model or a dict."""
    if adj is None:
        return default
    if isinstance(adj, dict):
        return adj.get(key, default)
    return getattr(adj, key, default)


def _adj_per_hour(directives: List[DirectiveInterpretation]) -> Dict[int, Dict[str, Any]]:
    out = {h: {"factor": 1.0,
               "minimum_energy_kwh": 0.0,
               "max_grid_kwh": 1e18,
               "no_charge": False,
               "no_discharge": False}
           for h in range(24)}
    for d in directives:
        if not d.applies or d.directive_type == DirectiveType.NO_OP:
            continue
        a = d.structured_adjustment
        for h in _adj_get(a, "hours", []) or []:
            slot = out[h]
            if d.directive_type == DirectiveType.SOLAR_REDUCTION:
                slot["factor"] = min(
                    slot["factor"],
                    float(_adj_get(a, "factor", 1.0)),
                )
            elif d.directive_type == DirectiveType.MINIMUM_BATTERY_RESERVE:
                slot["minimum_energy_kwh"] = max(
                    slot["minimum_energy_kwh"],
                    float(_adj_get(a, "minimum_energy_kwh", 0.0)),
                )
            elif d.directive_type == DirectiveType.NO_CHARGE_WINDOW:
                slot["no_charge"] = True
            elif d.directive_type == DirectiveType.NO_DISCHARGE_WINDOW:
                slot["no_discharge"] = True
            elif d.directive_type == DirectiveType.MAX_GRID_WINDOW:
                slot["max_grid_kwh"] = min(
                    slot["max_grid_kwh"],
                    float(_adj_get(a, "max_grid_kwh", 1e18)),
                )
    return out


def replay(
    hours: List[Hour],
    battery: Battery,
    directives: List[DirectiveInterpretation],
    plan: List[HourlyEntry],
) -> Dict[str, float]:
    """
    Returns recomputed totals: {total_grid_kwh, total_cost_bdt, peak_grid_kwh}.
    Raises ValueError if any constraint is violated.
    """
    if len(hours) != 24 or len(plan) != 24:
        raise ValueError("plan must contain 24 entries")
    if [e.hour for e in plan] != list(range(24)):
        raise ValueError("plan hours must be 0..23 in order")

    adj = _adj_per_hour(directives)
    E_prev = battery.initial_energy_kwh

    total_grid = 0.0
    total_cost = 0.0
    peak_grid  = 0.0

    for h, entry in enumerate(plan):
        hspec = hours[h]
        slot  = adj[h]
        eff_solar = hspec.solar_kwh * slot["factor"]

        # Non-negativity + basic shape
        if entry.grid_kwh < -SOFT or entry.solar_used_kwh < -SOFT or entry.battery_kwh < -SOFT:
            raise ValueError(f"hour {h}: negative energy")
        if entry.battery_energy_after_kwh < -SOFT:
            raise ValueError(f"hour {h}: negative SoC")

        # Action vs magnitude
        if entry.battery_action == BatteryAction.IDLE and entry.battery_kwh > SOFT:
            raise ValueError(f"hour {h}: idle but battery_kwh > 0")

        # Rate caps
        if entry.battery_action == BatteryAction.CHARGE:
            if entry.battery_kwh > battery.max_charge_kwh_per_hour + SOFT:
                raise ValueError(f"hour {h}: charge rate exceeded")
        if entry.battery_action == BatteryAction.DISCHARGE:
            if entry.battery_kwh > battery.max_discharge_kwh_per_hour + SOFT:
                raise ValueError(f"hour {h}: discharge rate exceeded")

        # Directive overrides
        if slot["no_charge"] and entry.battery_action == BatteryAction.CHARGE:
            raise ValueError(f"hour {h}: no_charge_window violated")
        if slot["no_discharge"] and entry.battery_action == BatteryAction.DISCHARGE:
            raise ValueError(f"hour {h}: no_discharge_window violated")
        if entry.grid_kwh > slot["max_grid_kwh"] + SOFT:
            raise ValueError(f"hour {h}: max_grid_window violated")

        # Solar cap
        if entry.solar_used_kwh > eff_solar + SOFT:
            raise ValueError(f"hour {h}: solar_used exceeds effective solar")

        # Battery dynamics
        c = entry.battery_kwh if entry.battery_action == BatteryAction.CHARGE    else 0.0
        d = entry.battery_kwh if entry.battery_action == BatteryAction.DISCHARGE else 0.0
        e_after_expected = E_prev + c - d
        if abs(entry.battery_energy_after_kwh - e_after_expected) > SOFT:
            raise ValueError(
                f"hour {h}: SoC mismatch (got {entry.battery_energy_after_kwh}, "
                f"expected {e_after_expected})"
            )

        # Storage bounds
        min_e = max(battery.minimum_energy_kwh, slot["minimum_energy_kwh"])
        if entry.battery_energy_after_kwh < min_e - SOFT:
            raise ValueError(f"hour {h}: below minimum reserve ({min_e})")
        if entry.battery_energy_after_kwh > battery.capacity_kwh + SOFT:
            raise ValueError(f"hour {h}: above capacity")

        # Energy balance
        balance_lhs = entry.grid_kwh + entry.solar_used_kwh + d
        balance_rhs = hspec.demand_kwh + c
        if abs(balance_lhs - balance_rhs) > SOFT:
            raise ValueError(
                f"hour {h}: energy balance off by {balance_lhs - balance_rhs}"
            )

        # Totals
        total_grid += entry.grid_kwh
        total_cost += entry.grid_kwh * hspec.tariff_bdt_per_kwh
        peak_grid   = max(peak_grid, entry.grid_kwh)

        E_prev = entry.battery_energy_after_kwh

    # Closure
    if abs(E_prev - battery.initial_energy_kwh) > SOFT:
        raise ValueError(
            f"end-of-day SoC mismatch (got {E_prev}, expected {battery.initial_energy_kwh})"
        )

    return {
        "total_grid_kwh": round(total_grid, 4),
        "total_cost_bdt": round(total_cost, 4),
        "peak_grid_kwh":  round(peak_grid, 4),
    }

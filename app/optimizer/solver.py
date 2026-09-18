"""
solver.py - Linear program that produces the 24-hour schedule.

Decision variables (h = 0..23):
    grid[h]        >= 0     energy drawn from the utility
    solar[h]       >= 0     solar actually used this hour
    charge[h]      >= 0     energy flowing INTO the battery
    discharge[h]   >= 0     energy flowing OUT of the battery
    E_after[h]              battery state of charge at end of hour

Objective:
    minimize SUM_h  grid[h] * tariff[h]

Constraints (every hour):
    energy balance:     grid + solar + discharge = demand + charge
    solar:              0 <= solar <= effective_solar[h]
    rates:              0 <= charge <= max_charge_per_hour
                        0 <= discharge <= max_discharge_per_hour
    storage:            min_energy_kwh <= E_after <= capacity_kwh
    dynamics:           E_after[h] = E_after[h-1] + charge - discharge
    hour 0:             E_after[0] = E_init + charge[0] - discharge[0]
    end-of-day closure: E_after[23] = E_init                (HARD equality)

Directive bound overrides (applied before solving):
    solar_reduction        -> effective_solar[h] = solar[h] * factor
    minimum_battery_reserve-> E_after[h] >= max(min_energy_kwh, directive)
    no_charge_window       -> charge[h] = 0
    no_discharge_window    -> discharge[h] = 0
    max_grid_window        -> grid[h] <= max_grid_kwh

Returns a dict consumable by the API layer.
"""
from __future__ import annotations
import logging
from typing import List, Dict, Any

import pulp

from app.schemas import (
    Hour,
    Battery,
    DirectiveType,
    DirectiveInterpretation,
    BatteryAction,
    HourlyEntry,
)

log = logging.getLogger(__name__)

EPS = 1e-6
ROUND = 4


class InfeasibleScenarioError(Exception):
    """Raised when the LP has no feasible solution under the given directives.

    Typically caused by a directive combination that contradicts physical
    constraints (e.g. minimum_battery_reserve > capacity, or no_discharge_window
    that overlaps peak-demand hours while SoC closure is required).
    """


def _round(x: float, n: int = ROUND) -> float:
    return float(round(x, n))


def _empty_adj() -> Dict[str, Any]:
    return {"hours": [], "factor": 1.0, "minimum_energy_kwh": 0.0, "max_grid_kwh": 1e18}


def _adj_get(adj, key, default=None):
    """Read a key from either a Pydantic model or a dict."""
    if adj is None:
        return default
    if isinstance(adj, dict):
        return adj.get(key, default)
    return getattr(adj, key, default)


def _merge(directives: List[DirectiveInterpretation]) -> Dict[str, Any]:
    """Combine all applied directives into per-hour bound overrides."""
    adj = {h: dict(_empty_adj()) for h in range(24)}
    for d in directives:
        if not d.applies or d.directive_type == DirectiveType.NO_OP:
            continue
        a = d.structured_adjustment
        for h in _adj_get(a, "hours", []) or []:
            slot = adj[h]
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
                slot["force_charge_zero"] = True
            elif d.directive_type == DirectiveType.NO_DISCHARGE_WINDOW:
                slot["force_discharge_zero"] = True
            elif d.directive_type == DirectiveType.MAX_GRID_WINDOW:
                slot["max_grid_kwh"] = min(
                    slot["max_grid_kwh"],
                    float(_adj_get(a, "max_grid_kwh", 1e18)),
                )
    return adj


def solve(
    hours: List[Hour],
    battery: Battery,
    directives: List[DirectiveInterpretation],
) -> Dict[str, Any]:
    """Run the LP and return a response-shaped dict (no Pydantic enforcement)."""

    if len(hours) != 24:
        raise ValueError("expected 24 hours")

    adj = _merge(directives)

    prob = pulp.LpProblem("gridwise", pulp.LpMinimize)

    grid  = [pulp.LpVariable(f"grid_{h}",  lowBound=0)            for h in range(24)]
    solar = [pulp.LpVariable(f"solar_{h}", lowBound=0)            for h in range(24)]
    chg   = [pulp.LpVariable(f"chg_{h}",   lowBound=0)            for h in range(24)]
    dis   = [pulp.LpVariable(f"dis_{h}",   lowBound=0)            for h in range(24)]
    Eaft  = [pulp.LpVariable(f"E_{h}",     lowBound=0)            for h in range(24)]

    tariff = [h.tariff_bdt_per_kwh for h in hours]
    demand = [h.demand_kwh          for h in hours]
    eff_solar = [h.solar_kwh * adj[h.hour]["factor"] for h in hours]

    # Objective
    prob += pulp.lpSum(grid[h] * tariff[h] for h in range(24))

    # Energy balance + bounds per hour
    for h in range(24):
        # Balance
        prob += (
            grid[h] + solar[h] + dis[h]
            == demand[h] + chg[h]
        ), f"balance_{h}"

        # Solar cap
        prob += solar[h] <= eff_solar[h],                  f"solar_cap_{h}"
        prob += solar[h] >= 0,                             f"solar_lb_{h}"

        # Battery rate caps
        prob += chg[h] <= battery.max_charge_kwh_per_hour,    f"chg_rate_{h}"
        prob += dis[h] <= battery.max_discharge_kwh_per_hour, f"dis_rate_{h}"

        # Storage bounds
        min_e = max(battery.minimum_energy_kwh, adj[h]["minimum_energy_kwh"])
        prob += Eaft[h] <= battery.capacity_kwh, f"cap_{h}"
        prob += Eaft[h] >= min_e,                f"min_{h}"

        # Directive overrides
        if adj[h].get("force_charge_zero"):
            prob += chg[h] == 0, f"no_chg_{h}"
        if adj[h].get("force_discharge_zero"):
            prob += dis[h] == 0, f"no_dis_{h}"
        if adj[h]["max_grid_kwh"] < 1e17:
            prob += grid[h] <= adj[h]["max_grid_kwh"], f"grid_cap_{h}"

        # Battery dynamics
        if h == 0:
            prob += (
                Eaft[0] == battery.initial_energy_kwh + chg[0] - dis[0]
            ), "E_init_0"
        else:
            prob += (
                Eaft[h] == Eaft[h-1] + chg[h] - dis[h]
            ), f"E_dyn_{h}"

    # End-of-day closure (hard equality)
    prob += Eaft[23] == battery.initial_energy_kwh, "E_close_23"

    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)
    if pulp.LpStatus[status] != "Optimal":
        msg = f"LP not optimal: {pulp.LpStatus[status]}"
        if pulp.LpStatus[status] in ("Infeasible", "Unbounded", "Not Solved"):
            raise InfeasibleScenarioError(msg)
        raise RuntimeError(msg)

    # Build hourly entries
    plan: List[HourlyEntry] = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid  = 0.0

    for h in range(24):
        g = max(0.0, pulp.value(grid[h]) or 0.0)
        s = max(0.0, pulp.value(solar[h]) or 0.0)
        c = max(0.0, pulp.value(chg[h]) or 0.0)
        d = max(0.0, pulp.value(dis[h]) or 0.0)
        e = pulp.value(Eaft[h]) or 0.0

        if   c > EPS and d <= EPS: action, bkwh = BatteryAction.CHARGE,    c
        elif d > EPS and c <= EPS: action, bkwh = BatteryAction.DISCHARGE, d
        elif c <= EPS and d <= EPS: action, bkwh = BatteryAction.IDLE,     0.0
        else:                       action, bkwh = BatteryAction.IDLE,     0.0   # both > 0 -> safety

        entry = HourlyEntry(
            hour=h,
            grid_kwh=_round(g),
            solar_used_kwh=_round(s),
            battery_action=action,
            battery_kwh=_round(bkwh),
            battery_energy_after_kwh=_round(e),
        )
        plan.append(entry)
        total_grid += entry.grid_kwh
        total_cost += entry.grid_kwh * tariff[h]
        peak_grid   = max(peak_grid, entry.grid_kwh)

    summary = _summarize(plan, directives, total_cost)

    return {
        "hourly_plan":         plan,
        "total_grid_kwh":      _round(total_grid),
        "total_cost_bdt":      _round(total_cost, 2),
        "peak_grid_kwh":       _round(peak_grid),
        "plan_summary":        summary,
    }


def _summarize(
    plan: List[HourlyEntry],
    directives: List[DirectiveInterpretation],
    total_cost: float,
) -> str:
    applied = [d for d in directives if d.applies and d.directive_type != DirectiveType.NO_OP]
    charge_hours  = [e.hour for e in plan if e.battery_action == BatteryAction.CHARGE]
    discharge_hours = [e.hour for e in plan if e.battery_action == BatteryAction.DISCHARGE]
    parts = [
        f"Optimized 24h plan: total cost {total_cost:.2f} BDT.",
        f"Charged during hours {charge_hours}.",
        f"Discharged during hours {discharge_hours}.",
    ]
    if applied:
        parts.append(f"Applied {len(applied)} directive(s): "
                     + "; ".join(d.explanation for d in applied))
    else:
        parts.append("No active directives; baseline cost minimization.")
    return " ".join(parts)

"""
schemas.py - Strict request/response contract for GridWise.
Maps 1:1 to the canonical spec. Do NOT loosen validation.
"""
from __future__ import annotations
from enum import Enum
from typing import List, Optional, Union, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, model_validator


# ----------------------------- Enums -----------------------------

class DirectiveType(str, Enum):
    SOLAR_REDUCTION        = "solar_reduction"
    MINIMUM_BATTERY_RESERVE = "minimum_battery_reserve"
    NO_CHARGE_WINDOW       = "no_charge_window"
    NO_DISCHARGE_WINDOW    = "no_discharge_window"
    MAX_GRID_WINDOW        = "max_grid_window"
    NO_OP                  = "no_op"


class BatteryAction(str, Enum):
    CHARGE    = "charge"
    DISCHARGE = "discharge"
    IDLE      = "idle"


# ----------------------------- Hour ------------------------------

class Hour(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int            = Field(ge=0, le=23)
    demand_kwh: float    = Field(ge=0)
    solar_kwh: float     = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


# --------------------------- Battery -----------------------------

class Battery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwh: float                  = Field(gt=0)
    initial_energy_kwh: float           = Field(ge=0)
    minimum_energy_kwh: float           = Field(ge=0)
    max_charge_kwh_per_hour: float      = Field(ge=0)
    max_discharge_kwh_per_hour: float   = Field(ge=0)

    @model_validator(mode="after")
    def _bounds(self):
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


# ----------------------- Directive payloads ----------------------

class SolarReductionAdjust(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]    = Field(min_length=1)
    factor: float       = Field(ge=0.0, le=1.0)

class MinReserveAdjust(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]            = Field(min_length=1)
    minimum_energy_kwh: float   = Field(ge=0)

class MaxGridAdjust(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]    = Field(min_length=1)
    max_grid_kwh: float = Field(ge=0)


# ----------------------- Interpretation --------------------------

class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[
        Union[SolarReductionAdjust, MinReserveAdjust, MaxGridAdjust, Dict[str, Any]]
    ] = None
    explanation: str


# ------------------------- Hourly plan ---------------------------

class HourlyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int
    grid_kwh: float                 = Field(ge=0)
    solar_used_kwh: float           = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float              = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def _idle_zero(self):
        if self.battery_action == BatteryAction.IDLE and self.battery_kwh != 0:
            raise ValueError("battery_kwh must be 0 when action is idle")
        return self


# ------------------------- Request / Response --------------------

class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    operator_notes: List[str] = Field(min_length=1, max_length=3)
    hours: List[Hour]         = Field(min_length=24, max_length=24)
    battery: Battery

    @model_validator(mode="after")
    def _hour_sequence(self):
        if [h.hour for h in self.hours] != list(range(24)):
            raise ValueError("hours must contain exactly hour 0..23 in order")
        if any(not (n and n.strip()) for n in self.operator_notes):
            raise ValueError("operator_notes must contain non-empty strings")
        return self


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

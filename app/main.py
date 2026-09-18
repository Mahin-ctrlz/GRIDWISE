"""
main.py - Public API surface.

    GET  /health           -> {"status": "ok"}
    POST /optimize-energy  -> OptimizeResponse

PRD §11: 400 on malformed/structurally invalid request, 500 on internal error.
Never leak API keys, secrets, or stack traces.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Any, Dict, List

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError

from app.schemas import (
    OptimizeRequest,
    OptimizeResponse,
    DirectiveInterpretation,
    DirectiveType,
)
from app.llm.interpreter import interpret
from app.directives.validator import validate
from app.optimizer.solver import solve, InfeasibleScenarioError
from app.validator.replay import replay, TOL

log = logging.getLogger("gridwise")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="GridWise API", version="1.0.0")


# ----------------------------- Errors -----------------------------

def _sanitize_errors(errors):
    """Strip non-JSON-serializable objects (e.g. ValueError instances in ctx)
    so the envelope always renders cleanly. Keeps loc/msg/type which are what
    clients actually need.
    """
    safe = []
    for e in errors:
        safe.append(
            {
                "loc":  list(e.get("loc",  [])),
                "msg":  str(e.get("msg",  "")),
                "type": str(e.get("type", "")),
            }
        )
    return safe


def _bad_request(detail):
    """Canonical 400 shape."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "invalid_request", "detail": detail},
    )


@app.exception_handler(RequestValidationError)
async def _pydantic_validation_handler(_: Request, exc: RequestValidationError):
    """Schema validation failures -> 400 (PRD §11)."""
    return _bad_request(_sanitize_errors(exc.errors()))


@app.exception_handler(json.JSONDecodeError)
async def _json_decode_handler(_: Request, exc: json.JSONDecodeError):
    """Malformed JSON body -> 400."""
    return _bad_request(f"invalid JSON: {exc.msg}")


@app.exception_handler(InfeasibleScenarioError)
async def _infeasible_handler(_: Request, exc: InfeasibleScenarioError):
    """Directive combination leaves no feasible plan -> 400 with a clean detail."""
    return _bad_request(str(exc))


@app.exception_handler(StarletteHTTPException)
async def _http_handler(_: Request, exc: StarletteHTTPException):
    """Force every HTTP exception through our sanitized envelope."""
    if exc.status_code == 400:
        return _bad_request(exc.detail)
    if exc.status_code >= 500:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "server_error", "detail": "an internal error occurred"},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "http_error", "detail": str(exc.detail)},
    )


@app.exception_handler(Exception)
async def _internal_handler(_: Request, exc: Exception):
    """Catch-all for unexpected errors. Never leak internals."""
    log.exception("internal error")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "an internal error occurred"},
    )


# ----------------------------- Routes -----------------------------

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(req: OptimizeRequest):
    t0 = time.perf_counter()

    # ---- 1. LLM interpretation (raw envelope) --------------------
    raw = interpret(req.operator_notes, req.battery)
    raw_list = raw.get("interpretations") or []

    # ---- 2. Guardrail pass (per note) ---------------------------
    safe: List[DirectiveInterpretation] = []
    for idx, note in enumerate(req.operator_notes):
        # Find a matching raw entry, else synthesize a no_op input
        match = next(
            (r for r in raw_list if r.get("note_index") == idx),
            {
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "no interpretation returned",
            },
        )
        safe.append(validate(match, idx, req.battery))

    # ---- 3. Solve ------------------------------------------------
    result = solve(req.hours, req.battery, safe)

    # ---- 4. Replay verification + recompute totals ---------------
    verified = replay(req.hours, req.battery, safe, result["hourly_plan"])

    # Overwrite the optimizer's totals with the replay-computed ones
    # (within 0.01 tolerance we keep the replay numbers as authoritative).
    for key in ("total_grid_kwh", "peak_grid_kwh"):
        if abs(result[key] - verified[key]) > TOL:
            raise RuntimeError(
                f"{key} tolerance violated: solver {result[key]} vs replay {verified[key]}"
            )
    result["total_grid_kwh"] = verified["total_grid_kwh"]
    result["total_cost_bdt"] = round(verified["total_cost_bdt"], 2)
    result["peak_grid_kwh"]  = verified["peak_grid_kwh"]

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log.info("scenario_id=%s solved in %.1f ms", req.scenario_id, elapsed_ms)

    return OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=safe,
        hourly_plan=result["hourly_plan"],
        total_grid_kwh=result["total_grid_kwh"],
        total_cost_bdt=result["total_cost_bdt"],
        peak_grid_kwh=result["peak_grid_kwh"],
        plan_summary=result["plan_summary"],
    )

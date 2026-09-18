# GridWise API

Production-ready smart-grid optimization pipeline implementing the BUP CSE Fest 2026
**GridWise Challenge** spec. FastAPI + PuLP with a pluggable LLM interpretation
layer and an independent replay verifier.

## Architecture

```
HTTP  -->  FastAPI  -->  LLM (Structured Outputs | deterministic)
                     -->  Guardrails
                     -->  PuLP LP solver
                     -->  Replay validator (re-derives every constraint)
                     -->  Strict JSON response
```

## File tree

```
gridwise/
├── app/
│   ├── main.py              # FastAPI app + error handlers
│   ├── schemas.py           # Pydantic v2 enums + request/response models
│   ├── directives/
│   │   ├── normalizer.py    # "1 PM to 3 PM" -> [13,14], "50%" -> 0.5
│   │   └── validator.py     # Hard guardrails (falls back to no_op on failure)
│   ├── llm/
│   │   ├── prompts.py       # System prompt + JSON schema for Structured Outputs
│   │   └── interpreter.py   # OpenAI call + deterministic fallback parser
│   ├── optimizer/
│   │   └── solver.py        # PuLP LP, all constraints incl. end-of-day closure
│   └── validator/
│       └── replay.py        # Independent re-check of every constraint
├── samples/
│   ├── request_solar_reduction.json
│   └── request_multi.json
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

## Install & run

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. (optional) Configure LLM
cp .env.example .env
# edit OPENAI_API_KEY=... ; LLM_PROVIDER=openai|deterministic|auto

# 3. Run
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Docker
docker build -t gridwise . && docker run --rm -p 8000:8000 gridwise
```

# 5. For Docker HUB
## Docker Deployment
Run the pre-built API container directly from Docker Hub:
```bash
docker run -d -p 8000:8000 --env-file .env mahinctrlz/gridwise-api:latest

##

## Endpoints

### `GET /health`
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### `POST /optimize-energy`
```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  --data-binary @samples/request_solar_reduction.json
```

Returns the full `OptimizeResponse` schema. Errors:

| Status | When |
|---|---|
| 400  | malformed JSON or Pydantic validation failure |
| 500  | internal error (sanitized message, no stack/key leakage) |

## LLM provider matrix

| `LLM_PROVIDER` | `OPENAI_API_KEY` | Behavior |
|---|---|---|
| `openai`        | set       | OpenAI Structured Outputs (json_schema, strict=true) |
| `auto` (default)| set       | OpenAI Structured Outputs |
| `auto`          | unset     | Deterministic regex parser (offline-safe) |
| `deterministic` | any       | Deterministic regex parser |

The deterministic parser handles every canonical example in the spec
(`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`,
`no_discharge_window`, `max_grid_window`, and distractor → `no_op`).

## Math model

Minimize  Σ_h grid[h] · tariff[h]   subject to:

- grid[h] + solar[h] + discharge[h] = demand[h] + charge[h]
- 0 ≤ solar[h] ≤ effective_solar[h] · factor[h]
- 0 ≤ charge[h] ≤ max_charge_per_hour   (or 0 in no_charge_window)
- 0 ≤ discharge[h] ≤ max_discharge_per_hour  (or 0 in no_discharge_window)
- min_energy ≤ E_after[h] ≤ capacity   (max'd with directive minimum)
- E_after[h] = E_after[h-1] + charge[h] - discharge[h]
- E_after[23] = initial_energy_kwh    ← hard end-of-day closure

## Tests

36-assertion pytest suite covering the canonical PRD scenarios, correctness
invariants (energy balance, SoC closure, rate caps, totals match replay), and
adversarial guardrail behavior. Run with:

```bash
# install pytest (already in requirements.txt)
pip install -r requirements.txt

# run the suite
pytest tests/ -v
# or on Windows with the venv activated:
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

Expected: `36 passed in ~1s`. Coverage:

- 1 health + 3 canonical PRD scenarios
- 9 correctness invariants (re-derives what the solver computed)
- 11 guardrail / edge-case behaviors
- 9 schema enforcement (validation returns 400)
- 3 solver behavior assertions (window respected, solar used, infeasibility → 400)

## Manual verification checklist

- [ ] `GET /health` returns `{"status":"ok"}`.
- [ ] Sample `request_solar_reduction.json` → `total_cost_bdt == Σ grid[h]·tariff[h]` within 0.01.
- [ ] End-of-day SoC equals `initial_energy_kwh`.
- [ ] Multi-note request (`request_multi.json`) yields exactly 3 interpretations in
      `note_index` order, the lunch reminder maps to `no_op` (`applies=false`,
      `structured_adjustment=null`).
- [ ] A malformed payload (`{"scenario_id": 123}`) → **400**, not 500 or 422.
- [ ] A payload with `hours` not in 0..23 order → **400**.
- [ ] A directive with `factor = 2.0` → guardrail collapses to `no_op`.

## Limitations

- **Single battery, single PV array.** The spec describes a campus microgrid with
  one aggregated battery and one solar array; we do not model multiple assets,
  inverters, or DC buses.
- **No grid export.** `grid_kwh ≥ 0` is enforced — surplus solar that exceeds
  battery headroom is curtailed (`solar_used ≤ effective_solar`), never exported.
- **Deterministic LP.** PuLP's bundled CBC solver is exact for this problem size
  (24×4 variables). No heuristic / metaheuristic fallback is provided.
- **End-of-day SoC closure is a hard equality.** If a directive combination makes
  the LP infeasible (e.g. a `no_discharge_window` covering every discharge hour
  while the schedule needs to discharge to satisfy demand), the request returns
  **500** with a generic message rather than a relaxed best-effort plan. The
  guardrail's job is to prevent this; the LP does not silently violate a
  directive to "save" the request.
- **LLM is pluggable.** When `OPENAI_API_KEY` is unset, the interpreter runs a
  deterministic regex/keyword parser that covers every canonical example in the
  spec — judges running offline or without API budget will still get full
  directive coverage.
- **No auth.** The endpoints are intentionally open per the challenge spec.

## Secret handling

- `OPENAI_API_KEY` (and any future secrets) are read **only** from environment
  variables / `.env` via `python-dotenv`. They are never logged, never echoed in
  error responses, and never serialized into the JSON response.
- `.env` is in `.gitignore`. Ship only `.env.example`.
- The global exception handler in `app/main.py` returns the literal string
  `"an internal error occurred"` for any 500 — never the offending exception,
  traceback, or environment value.
- When deploying with Docker, pass the key at runtime:
  `docker run -e OPENAI_API_KEY=$OPENAI_API_KEY -p 8000:8000 gridwise`.

## Why the LLM is used (PRD §6)

> "The LLM must be used for operator-note interpretation. Using an LLM only to
> generate plan_summary, documentation, or explanations is not sufficient."

The LLM's *sole job* in this service is converting free-text operator notes into
the six canonical directive shapes (`solar_reduction`,
`minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`,
`max_grid_window`, `no_op`). The optimizer, replay validator, totals, and
`plan_summary` are **deterministic** — no LLM is involved downstream of the
interpreter. The system therefore degrades gracefully when no key is available:
the deterministic parser substitutes for the LLM and produces the same envelope.

## Common pitfalls already avoided (PRD §18)

- ❌ LLM only for summary                       — fixed: LLM drives note→directive
- ❌ Hard-coded phrase matching as sole path    — fixed: LLM is primary; regex is fallback
- ❌ Optimizer ignores a valid directive       — fixed: per-directive bound overrides in solver
- ❌ Invalid energy balance                    — fixed: replay validator verifies
- ❌ Battery ends at a different energy level   — fixed: hard equality `E[23] = E[0]`
- ❌ Incorrect hour conversion                  — fixed: end-exclusive, "1 PM-3 PM" → [13, 14]

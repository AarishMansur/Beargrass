# Touch Grass Agent

An open-source AI agent powered by **Hermes** that watches your screen time and
*forces* you to touch grass — it turns a "you've been on this screen for 2
hours" alert into a concrete, offline plan: **a real nearby park or cafe, a
real walking route, and zero further scrolling.**

Built as a single-repository **monolith**: FastAPI server + Hermes tool-calling
engine + OpenStreetMap clients + a local trigger client, all in one modular
project. Designed for the open-innovation track: no proprietary maps, no
mandatory cloud LLM, no telemetry.

---

## Architecture & Monolith Breakdown

One deployable unit, four clearly bounded modules:

```
Touch-grass/
├── app/                        # ── SERVER (the monolith)
│   ├── main.py                 # FastAPI entry point, app factory, CORS, /health
│   ├── config.py               # pydantic-settings: every env var in one place
│   ├── routes.py               # POST /api/v1/trigger
│   ├── schemas.py              # Pydantic contracts (request/response models)
│   ├── agent/
│   │   └── hermes.py           # Hermes system prompt, tool schema, tool loop
│   └── services/
│       └── osm.py              # Nominatim + Overpass + OSRM wrappers (keyless)
├── client/                     # ── LOCAL TRIGGER (stdlib only, no pip install)
│   ├── tracker.py              # cross-platform active/idle time accounting
│   └── monitor.py              # sampling loop -> POST alert to the backend
├── tests/                      # 46 unit/integration tests (network mocked)
├── render.yaml                 # Render Blueprint (single Python Web Service)
├── requirements.txt            # server dependencies
├── pyproject.toml              # package metadata + pytest config
└── .env.example                # copy to .env for local development
```

### Request flow

```
screen-time alert (client/monitor.py)
        │  { minutes: 132, lat, lon, preferences }
        ▼
POST /api/v1/trigger  (app/routes.py)
        ▼
HermesAgent.plan()    (app/agent/hermes.py)
        │   tools: find_outdoor_spots · get_walking_route · reverse_location
        ▼
OpenStreetMap        (app/services/osm.py)
        │   Overpass -> nearby POIs   (fallback: Nominatim bounded search)
        │   Nominatim -> reverse geocode / search fallback
        │   OSRM foot  -> walking distance, duration, turn-by-turn steps
        ▼
TriggerResponse { headline, plan, place, route, alternatives }
        ▼
monitor.py prints: "Walk 640 m (~8 min) to Bryant Park. Phone stays in your pocket."
```

### Why it is a monolith (on purpose)

| Benefit | What it means here |
| --- | --- |
| **One process, one deploy** | `uvicorn app.main:app` is the entire server; Render runs exactly that. |
| **Shared config** | `Settings` is the single source of truth for server *and* tests. |
| **Local agent + tools share memory** | The OSM TTL cache and rate-limits live in one `OSMService`. |
| **Simple to audit** | The whole privacy surface is two files: `osm.py` and `monitor.py`. |

The module boundaries (`agent/`, `services/`, `client/`) are the future
extraction seams — each could become a service later without a rewrite.

---

## Setup & Execution

### 1. Server (local)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # optional; sane defaults work
uvicorn app.main:app --reload
```

Verify: <http://127.0.0.1:8000/health>

### 2. Wire up Hermes (optional but recommended)

Any OpenAI-compatible endpoint works — the agent degrades gracefully to a
deterministic local planner if no LLM is configured.

```bash
# .env — OpenRouter (hosted Hermes)
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_MODEL=nousresearch/hermes-3-llama-3.1-405b

# .env — Ollama (fully local, zero cost, zero egress)
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=hermes3
```

### 3. Local trigger client (no install needed)

```bash
python client/monitor.py                                  # real tracking, 2h threshold
python client/monitor.py --once                           # fire one trigger now
python client/monitor.py --dry-run --once                 # inspect the payload
python client/monitor.py --simulate --threshold-minutes 1 --interval 1
python client/monitor.py --lat 40.7580 --lon -73.9855 --preferences quiet,coffee
```

Environment knobs: `TRIGGER_ENDPOINT`, `DEFAULT_LAT`, `DEFAULT_LON`,
`SCREEN_TIME_THRESHOLD_MIN` (see `.env.example`).

### 4. API

`POST /api/v1/trigger`

```json
{
  "event": { "minutes": 132, "application": "browser", "device": "desktop" },
  "latitude": 40.7580,
  "longitude": -73.9855,
  "radius_m": 1500,
  "preferences": ["quiet", "coffee"]
}
```

Response (trimmed):

```json
{
  "headline": "Go touch grass.",
  "plan": "Walk 640 m (~8 min) to Bryant Park for a proper patch of grass and sky. Route: Start on 5th Ave…",
  "place": { "name": "Bryant Park", "kind": "park", "distance_m": 640.0 },
  "route": { "summary": "640 m on foot", "duration_min": 8.0, "source": "osrm" },
  "alternatives": [],
  "model_used": "nousresearch/hermes-3-llama-3.1-405b",
  "data_source": "OpenStreetMap"
}
```

Interactive docs: `/docs` (Swagger UI).

### 5. Tests

```bash
pip install -r requirements.txt
python -m pytest        # 46 tests: agent, OSM parsers, client, blueprint
```

### 6. Deploy on Render

The repo ships a [Render Blueprint](render.yaml): push to GitHub, then
**New → Blueprint** in the Render dashboard (or `render blueprint apply`).

- Python Web Service, `uvicorn app.main:app`, health check at `/health`
- `LLM_API_KEY` is `sync: false` — set it in the dashboard, never in git
- Works immediately with `ALLOW_OFFLINE_PLANNER=true` (no key required)

---

## Why Open Innovation Matters

**1. Privacy by construction.** Location never touches a proprietary map
vendor. Lookups use **OpenStreetMap** — Nominatim for geocoding, Overpass for
nearby parks/cafes, OSRM for routes — all free, keyless, and community-run.
The client deliberately does *not* report which application was eating your
attention (see `build_payload` in `client/monitor.py`); the agent only ever
learns "how long" and "roughly where". Coordinates are rounded to 6 decimal
places (~11 cm) before they leave your machine, and nothing is logged
server-side.

**2. Uncensored, portable routing.** The brain is **Hermes** behind an
OpenAI-compatible interface, so you choose the harness: OpenRouter for a
hosted model, Ollama for a fully local one, or your own direct API. No vendor
lock-in, no silent model swaps, no content policy between you and your own
agent's system prompt. If an endpoint goes down, the deterministic offline
planner still returns a real OSM-backed plan instead of a 500.

**3. Cost efficiency.** Zero-cost inputs everywhere it counts: OSM/Nominatim/
OSRM are free with fair-use limits (the service rate-limits and caches itself),
open-weight Hermes models are free to self-host, the local client is stdlib
only, and the whole server fits in Render's free tier. A hackathon project
that costs $0 to run after the hackathon is a project that survives the
hackathon.

---

## License

[MIT](LICENSE) © 2026 Aarish Mansur

Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL).

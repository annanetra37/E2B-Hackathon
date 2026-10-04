# Grid Substation Portal (MVP)

A portal that shows **every substation on a map of Armenia** (green = working, amber = warning,
red = problem, grey = no data). Click a substation to see its **live single-line diagram**, with the
status and the measured values next to every element.

Two kinds of substation are supported:

| Type | Diagram | Data sources |
|---|---|---|
| **Primary** 110/10 kV | 110 kV line → Q0 breaker → transformer → Q1 breaker → 10 kV busbar → feeders | Modbus TCP meters + IEC 104 RTU (breakers, frequency) |
| **Transformer station** 10/35/110 kV → 0.4 kV (the hand-drawn sketch) | MV line → QS disconnector → FU fuses → transformer (temperature) → 0.4 kV busbar with L1/L2/L3 → outgoing lines (with or without current meters) → data box | Modbus TCP only |

It runs **without a real grid**: a simulator plays 14 substations that develop faults and recover.

```
 simulator.py (fake grid)        gateway.py (collect + judge)       web.py + static/ (portal)
 14 Modbus TCP servers  ◄──────  polls all substations in parallel   map of Armenia
  5 IEC 104 RTUs        ◄──────  health.py: ok / warning / fault ──► single-line diagrams
                                 SQLite: snapshots, events, history   trends, events, demo buttons
```

## Run it on your computer (Python 3.10+)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Three terminals, each with the venv active:

| Terminal | Command |
|---|---|
| 1 | `python simulator.py` |
| 2 | `python gateway.py` |
| 3 | `uvicorn web:app --port 8000` → open http://localhost:8000 |

On a substation page, **Demo controls** let you trigger a problem (a fuse blows, a breaker trips, …)
and watch the diagram, map and event list react within seconds.

## Files

| File | Role |
|---|---|
| `substations.yaml` | the only file to edit: substations, positions, register maps, limits |
| `simulator.py` | fake grid: realistic values and faults for both substation types |
| `gateway.py` | reads every device (Modbus TCP + IEC 104), stores snapshots / events / history. **Read-only.** |
| `health.py` | the rules: what is working, warning or not working, and why (plain sentences) |
| `web.py` | portal server + API (`/api/substations`, `/api/substations/{id}`, `/events`, `/history`) |
| `static/` | map page, substation page, styles; Leaflet is bundled in `static/vendor` |
| `Dockerfile`, `start.sh`, `railway.json` | Railway deployment (all three programs in one container) |
| `deploy/gateway.service` | run only the gateway 24/7 on an on-site Linux box |

## Deploy on Railway

1. Push this folder to a GitHub repo.
2. railway.com → **New Project → Deploy from GitHub repo**. It builds from the `Dockerfile`.
3. **Variables**: `DASHBOARD_PASSWORD` (the browser asks for it; any user name works).
   Optional: `RETENTION_HOURS` (default 24), `SIM_FAULT_EVERY_S` (default 500, lower = more faults).
4. Optional: **Volumes** → mount a volume at `/data` to keep history across redeploys.
5. **Settings → Networking → Generate Domain**.

Health check: `/healthz`. If any program crashes, the container exits and Railway restarts it.

## Adding a real substation

1. Copy a block in `substations.yaml`, set its real IP addresses, `lat`/`lon`, ratings, lines.
2. If its devices use different register maps, add a template or adjust the existing one.
3. Run the gateway **on site** (it must reach the devices on the substation network) and set
   `RUN_SIMULATOR=false`. The portal can stay in the cloud.

**Safety:** the gateway only reads. The demo buttons only talk to the simulator, never to real equipment,
and they are switched off when `RUN_SIMULATOR=false`.

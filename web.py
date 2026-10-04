"""
THE PORTAL  (ELI5: the website people open)
===========================================
Serves the web pages (folder "static") and a small API that reads the
gateway's notebook:

  GET  /api/substations                 all substations: status for the map
  GET  /api/substations/{id}            one substation: every element of the diagram
  GET  /api/substations/{id}/events     what changed recently
  GET  /api/substations/{id}/history    trend data for one element
  GET  /api/events                      recent events across the whole grid
  POST /api/demo/{id}/{action}          demo only: make the FAKE grid misbehave
  GET  /healthz                         for Railway's health check

If DASHBOARD_PASSWORD is set, the browser asks for it (any user name works).
Run:  uvicorn web:app --port 8000      then open http://localhost:8000
"""
import base64
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import config

CFG = config.load()
DB = config.db_path(CFG)
STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DEMO = os.environ.get("RUN_SIMULATOR", "true").lower() == "true"
DEMO_ACTIONS = {   # what the demo buttons can make the FAKE grid do, per substation type
    "primary": {"feeder_trip": "Feeder breaker trips", "main_trip": "110 kV breaker trips",
                "overheat": "Transformer overheats", "undervoltage": "Incoming voltage sags",
                "overload": "Feeder overload", "comm_loss": "Transformer monitor goes silent"},
    "distribution": {"fuse_blown": "A fuse blows", "supply_loss": "Incoming line loses supply",
                     "disconnector_open": "Disconnector opens", "overheat": "Transformer overheats",
                     "overload": "Line overload", "overvoltage": "One phase overvoltage",
                     "comm_loss": "Temperature sensor goes silent"},
}
SUBS = {s["id"]: s for s in CFG["substations"]}
SUB_IDS = set(SUBS)

app = FastAPI(title="Grid substation portal", docs_url=None, redoc_url=None)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if PASSWORD and request.url.path != "/healthz":
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Grid portal"'})
    return await call_next(request)


def q(sql: str, params=()) -> list[sqlite3.Row]:
    if not DB.exists():
        return []
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []                       # tables not created yet


def age_seconds(ts: str) -> float:
    t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def load_snapshot(row) -> dict:
    snap = json.loads(row["data"])
    snap["age_s"] = round(age_seconds(row["ts"]))
    if snap["age_s"] > 30:                  # gateway stopped updating: don't show stale "green"
        snap["status"], snap["headline"] = "offline", f"No fresh data for {snap['age_s']} s (gateway down?)"
    return snap


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/substations")
def substations():
    snaps = {r["substation"]: load_snapshot(r) for r in q("SELECT * FROM snapshots")}
    out = []
    for s in CFG["substations"]:
        snap = snaps.get(s["id"])
        if snap:
            snap = {k: v for k, v in snap.items() if k != "elements"}
        else:
            snap = {"id": s["id"], "name": s["name"], "type": s["type"], "region": s.get("region", ""),
                    "lat": s["lat"],
                    "lon": s["lon"], "status": "offline", "headline": "Waiting for first data", "problem_count": 0}
        out.append(snap)
    return {"substations": out, "demo": DEMO}


@app.get("/api/substations/{sid}")
def substation(sid: str):
    rows = q("SELECT * FROM snapshots WHERE substation=?", (sid,))
    if sid not in SUB_IDS:
        raise HTTPException(404, "Unknown substation")
    if not rows:
        return JSONResponse({"id": sid, "name": SUBS[sid]["name"], "type": SUBS[sid]["type"],
                             "status": "offline", "headline": "Waiting for first data",
                             "elements": [], "demo": False, "demo_actions": {}})
    return {**load_snapshot(rows[0]), "demo": DEMO,
            "demo_actions": DEMO_ACTIONS[SUBS[sid]["type"]] if DEMO else {}}


@app.get("/api/substations/{sid}/events")
def substation_events(sid: str, limit: int = 30):
    return [dict(r) for r in q("SELECT ts, element, label, status, message FROM events "
                               "WHERE substation=? ORDER BY id DESC LIMIT ?", (sid, min(limit, 200)))]


@app.get("/api/events")
def all_events(limit: int = 20):
    names = {s["id"]: s["name"] for s in CFG["substations"]}
    rows = q("SELECT ts, substation, element, label, status, message FROM events ORDER BY id DESC LIMIT ?",
             (min(limit, 200),))
    return [{**dict(r), "substation_name": names.get(r["substation"], r["substation"])} for r in rows]


@app.get("/api/substations/{sid}/history")
def history(sid: str, element: str, minutes: int = 30):
    rows = q("SELECT ts, metric, value, unit FROM history WHERE substation=? AND element=? "
             "AND ts >= datetime('now', ?) ORDER BY ts", (sid, element, f"-{min(minutes, 1440)} minutes"))
    series: dict[str, dict] = {}
    for r in rows:
        s = series.setdefault(r["metric"], {"metric": r["metric"], "unit": r["unit"], "points": []})
        s["points"].append([r["ts"], r["value"]])
    return list(series.values())


@app.post("/api/demo/{sid}/{action}")
def demo(sid: str, action: str):
    if not DEMO:
        raise HTTPException(403, "Demo controls are off (RUN_SIMULATOR=false)")
    if sid not in SUB_IDS or (action != "clear" and action not in DEMO_ACTIONS[SUBS[sid]["type"]]):
        raise HTTPException(400, "Unknown substation or action")
    with sqlite3.connect(DB, timeout=5) as db:
        db.execute("INSERT INTO sim_commands (substation, action, ts) VALUES (?,?,datetime('now'))", (sid, action))
    return {"queued": action}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/substation/{sid}")
def substation_page(sid: str):
    return FileResponse(STATIC / "substation.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

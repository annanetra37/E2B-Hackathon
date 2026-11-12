"""
HTTP API + pages for the Live tab (§7, §8).

  GET  /live                         Live tab (?kiosk=1 for the stand screen)
  GET  /demo/join                    visitor sign-up (QR target)
  GET  /demo/admin                   presenter control page (asks for ADMIN_TOKEN)
  GET  /u/{token}                    unsubscribe link from the welcome SMS

  GET  /api/live/state               station, status, last reading, thresholds, mqtt flag
  GET  /api/live/history?seconds=120 readings for the chart (max 3600 s, downsampled)
  GET  /api/live/events?limit=10     latest state changes, newest first
  GET  /api/live/stream              Server-Sent Events: reading, status, event, thresholds, ping
  GET  /api/live/diag                diagnostics (no phone numbers)
  GET  /api/demo/qr.svg              QR code pointing at /demo/join
  POST /api/demo/subscribe           {phone, lang, consent:true}
  POST /api/demo/admin/...           source, simulate, thresholds, reset, test-sms   (Bearer ADMIN_TOKEN)
  DELETE /api/demo/admin/subscribers delete every stored number
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from html import escape
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from .notifier import normalize_phone

STATIC = Path(__file__).resolve().parent.parent / "static"
PING_EVERY_S = 15


class SubscribeIn(BaseModel):
    phone: str
    lang: str = "en"
    consent: bool = False


class SourceIn(BaseModel):
    source: str


class SimulateIn(BaseModel):
    hairDryer: bool | None = None
    level: str = "high"
    light: bool | None = None


class ThresholdsIn(BaseModel):
    warnW: float | None = None
    alarmW: float | None = None
    holdS: float | None = None
    clearS: float | None = None
    clearW: float | None = None
    minAlarmW: float | None = None
    baselineMultiplier: float | None = None
    baselineWindowS: float | None = None


class TestSmsIn(BaseModel):
    phone: str


class RateLimiter:
    def __init__(self, limit: int, per_s: float):
        self.limit, self.per_s = limit, per_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.per_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10_000:                # keep memory bounded
            for k in [k for k, v in self.hits.items() if not v][:5000]:
                self.hits.pop(k, None)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def build_router(get_service) -> APIRouter:
    """get_service() returns the running LiveService, or None when DEMO_ENABLED=false."""
    r = APIRouter()
    subscribe_limit = RateLimiter(5, 60)

    def svc():
        s = get_service()
        if s is None:
            raise HTTPException(404, "Live demo is disabled (DEMO_ENABLED=false)")
        return s

    def admin(request: Request):
        s = svc()
        token = s.s.admin_token
        if not token:
            raise HTTPException(403, "ADMIN_TOKEN is not set: admin actions are disabled")
        auth = request.headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not given or not secrets.compare_digest(given, token):
            raise HTTPException(401, "Wrong admin token", headers={"WWW-Authenticate": "Bearer"})
        return s

    # ------------------------------------------------------------------ pages
    @r.get("/live", include_in_schema=False)
    def live_page():
        svc()
        return FileResponse(STATIC / "live.html")

    @r.get("/demo/join", include_in_schema=False)
    def join_page():
        svc()
        return FileResponse(STATIC / "join.html")

    @r.get("/demo/admin", include_in_schema=False)
    def admin_page():
        svc()
        return FileResponse(STATIC / "admin.html")

    @r.get("/u/{token}", include_in_schema=False)
    async def unsubscribe(token: str):
        s = svc()
        ok = await asyncio.to_thread(s.store.unsubscribe, token)
        title = "You are unsubscribed" if ok else "Link already used or expired"
        hy = "Դուք ապաբաժանորդագրվել եք" if ok else "Հղումն արդեն օգտագործված է կամ ժամկետանց"
        body = ("Your number will receive no more messages and is deleted from our demo within a minute."
                if ok else "There is nothing to unsubscribe. Numbers are deleted automatically after 24 h.")
        return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/live.css"></head>
<body class="join-body"><main class="join-card"><h1>{escape(title)}</h1><p lang="hy">{escape(hy)}</p>
<p>{escape(body)}</p></main></body></html>""")

    # ------------------------------------------------------------------ read API
    @r.get("/api/live/state")
    async def state():
        return await svc().state()

    @r.get("/api/live/history")
    async def history(seconds: int = 120):
        return {"seconds": max(1, min(3600, seconds)), "readings": await svc().history(seconds)}

    @r.get("/api/live/events")
    async def events(limit: int = 10):
        return {"events": await svc().events(limit)}

    @r.get("/api/live/diag")
    async def diag():
        return await svc().diag()

    @r.get("/api/live/stream")
    async def stream(request: Request):
        s = svc()
        q = s.add_client()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                yield f"event: status\ndata: {json.dumps(s.status_payload())}\n\n"
                while True:
                    try:
                        kind, data = await asyncio.wait_for(q.get(), timeout=PING_EVERY_S)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield f"event: ping\ndata: {json.dumps({'t': time.time()})}\n\n"
                        continue
                    yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
            finally:
                s.remove_client(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    @r.get("/api/demo/qr.svg", include_in_schema=False)
    def qr(request: Request):
        s = svc()
        import segno
        url = s.join_url()
        if url.startswith("/"):
            url = str(request.base_url).rstrip("/") + url
        import io
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, dark="#1d2b33", light="#ffffff",
                                        xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})

    # ------------------------------------------------------------------ visitors
    @r.post("/api/demo/subscribe")
    async def subscribe(body: SubscribeIn, request: Request):
        s = svc()
        if not subscribe_limit.allow(client_ip(request)):
            raise HTTPException(429, "too_many_requests")
        if not body.consent:
            raise HTTPException(400, "consent_required")
        try:
            return await s.subscribe(body.phone, body.lang)
        except ValueError:
            raise HTTPException(400, "invalid_phone")

    # ------------------------------------------------------------------ admin
    @r.post("/api/demo/admin/check")
    def admin_check(request: Request):
        admin(request)
        return {"ok": True}

    @r.post("/api/demo/admin/source")
    def admin_source(body: SourceIn, request: Request):
        s = admin(request)
        if body.source not in ("live", "simulator"):
            raise HTTPException(400, "source must be live or simulator")
        s.set_source(body.source)
        return {"source": s.source}

    @r.post("/api/demo/admin/simulate")
    def admin_simulate(body: SimulateIn, request: Request):
        s = admin(request)
        if body.level not in ("low", "high"):
            raise HTTPException(400, "level must be low or high")
        s.simulate(body.hairDryer, body.level, body.light)
        return {"hairDryer": s.sim.hair_dryer, "level": s.sim.level, "light": s.sim.light, "source": s.source,
                "note": None if s.source == "simulator" else "simulator is not on air (source=live)"}

    @r.post("/api/demo/admin/thresholds")
    def admin_thresholds(body: ThresholdsIn, request: Request):
        s = admin(request)
        vals = body.model_dump()
        if any(v is not None and v < 0 for v in vals.values()):
            raise HTTPException(400, "thresholds must be positive")
        return s.set_thresholds(**vals)

    @r.post("/api/demo/admin/reset")
    def admin_reset(request: Request):
        s = admin(request)
        s.reset()
        return s.status_payload()

    @r.post("/api/demo/admin/test-sms")
    async def admin_test_sms(body: TestSmsIn, request: Request):
        s = admin(request)
        phone = normalize_phone(body.phone)
        if not phone:
            raise HTTPException(400, "invalid_phone")
        s.audit("test-sms", f"to {phone[:4]}…{phone[-3:]}")
        res = await s.notifier.test(phone)
        return {"ok": res.ok, "error": res.error, "providerMsgId": res.provider_msg_id}

    @r.delete("/api/demo/admin/subscribers")
    async def admin_delete_subscribers(request: Request):
        s = admin(request)
        n = await asyncio.to_thread(s.store.delete_all_subscribers)
        s.audit("delete-subscribers", f"{n} numbers deleted")
        return {"deleted": n}

    @r.get("/api/demo/admin/sms-log")
    async def admin_sms_log(request: Request):
        s = admin(request)
        rows = await asyncio.to_thread(s.store.recent_sms, 20)
        return {"sms": [{**row, "ts": row["ts"].isoformat() if row["ts"] else None} for row in rows]}

    return r

"""
Live demo: one real mini-substation (energy meter -> IWR405 gateway -> MQTT) on the map,
with an alarm engine, a live tab, visitor SMS alerts and a simulator as Plan B.

Wiring (see web.py):
    from live import LiveDemo
    demo = LiveDemo()                       # reads env; inactive unless DEMO_ENABLED=true
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from .api import build_router
from .settings import Settings

log = logging.getLogger("live")

# paths visitors must reach without the dashboard password (QR sign-up, unsubscribe, stand screen)
PUBLIC_PREFIXES_ALWAYS = ("/demo/join", "/api/demo/subscribe", "/u/", "/demo/admin", "/api/demo/admin/",
                          "/api/demo/qr.svg", "/static/")
PUBLIC_PREFIXES_LIVE = ("/live", "/api/live/")


class LiveDemo:
    def __init__(self, settings: Settings | None = None, **service_kw):
        self.settings = settings or Settings.from_env()
        self.service = None
        self._service_kw = service_kw
        self.router = build_router(lambda: self.service)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def is_public(self, path: str) -> bool:
        if not self.enabled:
            return False
        if path.startswith(PUBLIC_PREFIXES_ALWAYS):
            return True
        return self.settings.live_public and (path == "/live" or path.startswith(PUBLIC_PREFIXES_LIVE))

    @asynccontextmanager
    async def lifespan(self, app):
        if self.enabled:
            from .service import LiveService
            if not logging.getLogger().handlers:
                logging.basicConfig(level=logging.INFO, format="%(asctime)s LIVE %(levelname)s %(message)s")
            logging.getLogger("live").setLevel(logging.INFO)
            self.service = LiveService(self.settings, **self._service_kw)
            await self.service.start()
            log.info("live demo started: station=%s source=%s", self.settings.station_id, self.service.source)
        try:
            yield
        finally:
            if self.service:
                await self.service.stop()
                self.service = None

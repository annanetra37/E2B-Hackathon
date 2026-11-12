"""
The live pipeline, running inside the web process (single replica, §2):

  MQTT (or simulator) -> parse/normalise -> batched insert -> alarm engine -> SSE fan-out
                                                                   └── RED entry -> SMS fan-out
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timezone

from . import advice, alarm
from .alarm import AlarmEngine, Transition
from .mqtt_client import MqttIngestor
from .notifier import Notifier, mask_phone, normalize_phone
from .parser import iso, parse, parse_ts
from .settings import Settings
from .simulator import MeterSimulator
from .store import Store

log = logging.getLogger("live")

MAX_HISTORY_POINTS = 600


def _epoch(ts_iso: str) -> float:
    dt = parse_ts(ts_iso)
    return dt.timestamp() if dt else time.time()


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def event_out(e: dict) -> dict:
    ts = e["ts"]
    return {"id": e["id"], "ts": iso(ts) if isinstance(ts, datetime) else ts, "fromState": e["from_state"],
            "toState": e["to_state"], "peakW": e["peak_power_w"], "message": e["message"],
            "smsSent": e["sms_sent"] or 0}


def reading_out(row: dict) -> dict:
    """DB row -> the normalised reading shape used by the API."""
    ts = row["ts"]
    return {"ts": iso(ts) if isinstance(ts, datetime) else ts, "voltageV": row.get("voltage_v"),
            "currentA": row.get("current_a"), "powerW": row.get("power_w"), "pf": row.get("pf"),
            "freqHz": row.get("freq_hz"), "energyKwh": row.get("energy_kwh"),
            "late": bool(row.get("late")), "source": row.get("source")}


def downsample(points: list[dict], max_points: int = MAX_HISTORY_POINTS) -> list[dict]:
    """Keep the chart light; each bucket keeps its highest-power reading so spikes stay visible."""
    if len(points) <= max_points:
        return points
    size = len(points) / max_points
    out = []
    for i in range(max_points):
        bucket = points[int(i * size):int((i + 1) * size)] or [points[min(int(i * size), len(points) - 1)]]
        out.append(max(bucket, key=lambda p: p.get("powerW") or 0))
    return out


class LiveService:
    def __init__(self, settings: Settings, store: Store | None = None, provider=None, start_mqtt: bool = True):
        self.s = settings
        self.store = store
        self._provider = provider
        self.start_mqtt = start_mqtt
        self.source = settings.source if settings.source in ("live", "simulator") else "live"
        self.engine = AlarmEngine(settings.thresholds, settings.stale_after_s)
        self.sim = MeterSimulator(settings.station_id)
        self.notifier: Notifier | None = None
        self.mqtt: MqttIngestor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self.last_reading: dict | None = None
        self.last_reading_at: float | None = None
        self.buffer: list[dict] = []
        self.red_event_id: int | None = None
        self.transitions: asyncio.Queue | None = None
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        self.counters = {"parsed": 0, "rejected": 0, "late": 0, "ignoredLive": 0, "stored": 0, "dbErrors": 0}
        self.admin_log: deque = deque(maxlen=50)
        self.db_error: str | None = None
        self.welcome_failed = 0
        self.episode: advice.Episode | None = None
        self.alert_tasks: dict[int, asyncio.Task] = {}

    # ================================================================== lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.store is None:
            self.store = await asyncio.to_thread(Store, self.s.database_url, self.s.sqlite_path)
        log.info("live demo storage: %s", self.store.kind)
        self.notifier = Notifier(self.s, self.store, self._provider)
        self.transitions = asyncio.Queue()
        try:
            last = await asyncio.to_thread(self.store.last_reading, self.s.station_id)
            if last:
                self.last_reading = reading_out(last)
        except Exception as e:
            log.warning("could not load last reading: %s", e)
        if self.start_mqtt and self.s.mqtt_url:
            self.mqtt = MqttIngestor(self.s.mqtt_url, self.s.mqtt_topic, self.s.mqtt_client_id,
                                     self.s.mqtt_username, self.s.mqtt_password,
                                     on_message=self.on_mqtt_message, loop=self.loop)
            self.mqtt.start()
        elif not self.s.mqtt_url:
            log.warning("MQTT_URL not set: live source has no data (use DEMO_SOURCE=simulator for Plan B)")
        self.tasks = [asyncio.create_task(c) for c in
                      (self._ticker(), self._flusher(), self._transition_worker(), self._maintenance())]
        self._check_templates()

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.mqtt:
            self.mqtt.stop()
        await self._flush()
        if self.store:
            self.store.close()

    def _check_templates(self) -> None:
        from .notifier import is_gsm7
        sample = self.notifier.alert_text(12345, None, 12345)
        if len(sample) > 160:
            log.warning("SMS_TEMPLATE renders to %d characters (> 160): it will be split", len(sample))
        if not is_gsm7(sample):
            log.warning("SMS_TEMPLATE contains non GSM-7 characters: limit drops to 70 per SMS")

    # ================================================================== input
    def on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Runs on the event loop (called via call_soon_threadsafe from the MQTT thread)."""
        reading = parse(payload, fmt=self.s.payload_format, fields=self.s.fields, station_id=self.s.station_id,
                        late_after_s=self.s.late_after_s, device_time=self.s.device_time)
        if reading is None:
            self.counters["rejected"] += 1
            return
        self.counters["parsed"] += 1
        if self.source != "live":
            self.counters["ignoredLive"] += 1        # gateway is alive, but the simulator is on air
            return
        try:
            reading["raw"] = json.loads(payload)
        except Exception:
            reading["raw"] = None
        self.handle_reading(reading)

    def handle_reading(self, r: dict) -> None:
        """One normalised reading from either source."""
        self.buffer.append(r)
        public = {k: v for k, v in r.items() if k != "raw"}
        if r.get("late"):
            self.counters["late"] += 1
        else:
            self.last_reading = public
            self.last_reading_at = time.time()
        self.broadcast("reading", public)
        if self.episode is not None and not r.get("late"):
            self.episode.add(public, _epoch(r["ts"]))
        trs = self.engine.on_reading(_epoch(r["ts"]), r.get("powerW"), time.time(), bool(r.get("late")))
        for tr in trs:
            self._queue_transition(tr)

    def _queue_transition(self, tr: Transition | None) -> None:
        if tr is None:
            return
        self._attach_episode(tr)
        self.broadcast("status", self.status_payload())
        self.transitions.put_nowait(tr)

    def _attach_episode(self, tr: Transition) -> None:
        """Freeze what the meter measured into the transition and build the SMS text now,
        so the message describes exactly this moment."""
        s = self.s
        t = self.notifier.fmt_time(_dt(tr.ts))
        if tr.entered_red:
            reading = self.last_reading or {"powerW": tr.power_w}
            self.episode = advice.Episode.start(reading, tr.started_ts or tr.ts, tr.usual_w, tr.ts)
            tr.episode = self.episode
            if s.sms_style == "tailored":
                tr.sms_text = advice.alert_sms(self.episode, station=s.station_name, time=t,
                                               rated_a=s.circuit_rated_a, tariff=s.tariff_per_kwh,
                                               currency=s.tariff_currency)
        elif tr.left_red:
            ep, self.episode = self.episode, None
            tr.episode = ep
            if ep is None:
                return
            if tr.to_state == alarm.GREY:
                tr.sms_text = advice.lost_sms(ep, station=s.station_name, time=t)
            elif s.sms_style == "tailored":
                now_w = (self.last_reading or {}).get("powerW", tr.power_w)
                tr.sms_text = advice.recovery_sms(ep, station=s.station_name, time=t, now_w=now_w,
                                                  ended_ts=tr.dropped_ts or tr.ts, rated_a=s.circuit_rated_a,
                                                  tariff=s.tariff_per_kwh, currency=s.tariff_currency)
            else:
                tr.sms_text = self.notifier.recovery_template_text(tr.power_w or 0, _dt(tr.ts))

    # ================================================================== background tasks
    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                if self.source == "simulator":
                    self.handle_reading(self.sim.reading())
                self._queue_transition(self.engine.tick(time.time()))
            except Exception:
                log.exception("ticker error")

    async def _flush(self) -> None:
        if not self.buffer or self.store is None:
            return
        batch, self.buffer = self.buffer, []
        try:
            await asyncio.to_thread(self.store.insert_readings, batch)
            self.counters["stored"] += len(batch)
            self.db_error = None
        except Exception as e:
            self.counters["dbErrors"] += 1
            self.db_error = f"{type(e).__name__}: {e}"
            log.warning("could not store %d readings: %s", len(batch), e)

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self._flush()

    async def _maintenance(self) -> None:
        last_retention = 0.0
        while True:
            try:
                n = await asyncio.to_thread(self.store.delete_expired_subscribers)
                if n:
                    log.info("deleted %d expired/unsubscribed phone numbers", n)
                if time.time() - last_retention > 3600:
                    last_retention = time.time()
                    n = await asyncio.to_thread(self.store.delete_old_readings, self.s.retention_days)
                    if n:
                        log.info("retention: deleted %d readings older than %g days", n, self.s.retention_days)
            except Exception as e:
                log.warning("maintenance failed: %s", e)
            await asyncio.sleep(60)

    async def _transition_worker(self) -> None:
        """Persist events in order; kick off SMS without blocking the next event."""
        while True:
            tr: Transition = await self.transitions.get()
            try:
                await self._persist_transition(tr)
            except Exception:
                log.exception("could not persist transition %s", tr)

    async def _persist_transition(self, tr: Transition) -> None:
        ts = _dt(tr.ts)
        msg = tr.reason
        if tr.entered_red:
            msg = f"Power spike: {tr.power_w:,.0f} W" if tr.power_w is not None else "Power spike"
            if tr.usual_w is not None:
                msg += f" (usual {tr.usual_w:,.0f} W)"
        elif tr.left_red and tr.episode is not None and tr.to_state != alarm.GREY and tr.reason != "Reset by operator":
            ep = tr.episode
            msg = (f"Back to normal after {advice.duration((tr.dropped_ts or tr.ts) - ep.started_ts)}"
                   f" (peak {ep.peak_w:,.0f} W, {ep.extra_wh:,.0f} Wh extra)")
        eid = await asyncio.to_thread(self.store.insert_event, self.s.station_id, ts, tr.from_state,
                                      tr.to_state, msg, tr.peak_w if tr.left_red else
                                      (tr.power_w if tr.entered_red else None))
        log.info("status %s -> %s (%s)", tr.from_state, tr.to_state, msg)
        if tr.entered_red:
            self.red_event_id = eid
            self.alert_tasks[eid] = asyncio.create_task(self._alert(eid, tr.power_w or 0, ts, tr.usual_w, tr.sms_text))
        if tr.left_red:
            red_eid, self.red_event_id = self.red_event_id, None
            if red_eid is not None and tr.peak_w is not None:
                await asyncio.to_thread(self.store.update_event, red_eid, peak=tr.peak_w)
                await self._push_event(red_eid)
            send = tr.sms_text and (tr.to_state == alarm.GREY or self.s.sms_on_recovery)
            if send and tr.from_state == alarm.RED and tr.reason != "Reset by operator":
                asyncio.create_task(self._recovery(red_eid, eid, tr.sms_text))
        await self._push_event(eid)

    async def _push_event(self, eid: int) -> None:
        e = await asyncio.to_thread(self.store.get_event, eid)
        if e:
            self.broadcast("event", event_out(e))

    async def _recovery(self, red_eid: int | None, eid: int, text: str) -> None:
        try:
            task = self.alert_tasks.pop(red_eid, None) if red_eid is not None else None
            if task is not None:
                await asyncio.wait({task}, timeout=60)     # the alert must be out before "back to normal"
            n = await self.notifier.recovery(red_eid, text, eid)
            if n:
                await asyncio.to_thread(self.store.update_event, eid, sms_sent=n)
                await self._push_event(eid)
                log.info("back-to-normal SMS delivered to provider for %d numbers", n)
        except Exception:
            log.exception("recovery SMS failed")

    async def _alert(self, eid: int, power: float, ts: datetime, usual: float | None = None,
                     text: str | None = None) -> None:
        try:
            n = await self.notifier.alert(power, eid, ts, usual, text)
            await asyncio.to_thread(self.store.update_event, eid, sms_sent=n)
            await self._push_event(eid)
            log.info("overload SMS delivered to provider for %d numbers", n)
        except Exception:
            log.exception("SMS fan-out failed")

    # ================================================================== SSE
    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def broadcast(self, kind: str, data: dict) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:          # a stuck browser must not slow everyone else down
                self.clients.discard(q)

    # ================================================================== views
    def status_payload(self) -> dict:
        e = self.engine
        return {"status": e.state, "text": alarm.TEXT[e.state],
                "since": iso(_dt(e.since)) if e.since else None, "source": self.source,
                "peakW": e.peak_w if e.state == alarm.RED else None}

    def station(self) -> dict:
        return {"id": self.s.station_id, "name": self.s.station_name, "lat": self.s.lat, "lng": self.s.lng}

    async def state(self) -> dict:
        return {"station": self.station(), **self.status_payload(), "lastReading": self.last_reading,
                "thresholds": self.s.thresholds.public(),
                "mqttConnected": bool(self.mqtt and self.mqtt.connected),
                "smsEnabled": self.s.sms_enabled, "staleAfterS": self.s.stale_after_s,
                "joinUrl": self.join_url(), "serverTime": iso(datetime.now(timezone.utc))}

    def join_url(self) -> str:
        return f"{self.s.public_base_url}/demo/join" if self.s.public_base_url else "/demo/join"

    async def history(self, seconds: float) -> list[dict]:
        seconds = max(1, min(3600, seconds))
        await self._flush()
        rows = await asyncio.to_thread(self.store.history, self.s.station_id, seconds)
        return downsample([reading_out(r) for r in rows])

    async def events(self, limit: int) -> list[dict]:
        rows = await asyncio.to_thread(self.store.events, self.s.station_id, max(1, min(100, limit)))
        return [event_out(e) for e in rows]

    async def diag(self) -> dict:
        now = time.time()
        try:
            subs = await asyncio.to_thread(self.store.count_active_subscribers)
            db_ok = True
        except Exception as e:
            subs, db_ok = None, False
            self.db_error = str(e)
        return {
            "ok": db_ok and (self.source == "simulator" or bool(self.mqtt and self.mqtt.connected)),
            "status": self.engine.state, "source": self.source,
            "mqttConnected": bool(self.mqtt and self.mqtt.connected),
            "lastMessageAgeS": round(now - self.last_reading_at, 1) if self.last_reading_at else None,
            "mqtt": self.mqtt.diag() if self.mqtt else {"configured": False},
            "payloadFormat": self.s.payload_format, "counters": dict(self.counters),
            "sms": self.notifier.status(), "subscriberCount": subs,
            "db": {"kind": self.store.kind, "ok": db_ok, "lastError": self.db_error},
            "sseClients": len(self.clients), "uptimeS": round(now - self.started_at),
            "simulator": {"hairDryer": self.sim.hair_dryer, "level": self.sim.level, "light": self.sim.light,
                          "plugged": dict(self.sim.state)},
            "thresholds": self.s.thresholds.public(),
            "adminLog": list(self.admin_log)[-15:][::-1],
        }

    # ================================================================== admin actions
    def audit(self, action: str, detail: str = "") -> None:
        log.info("ADMIN %s %s", action, detail)
        self.admin_log.append({"ts": iso(datetime.now(timezone.utc)), "action": action, "detail": detail})

    def set_source(self, source: str) -> None:
        self.source = source
        self.audit("source", source)
        self.broadcast("status", self.status_payload())
        self.broadcast("sim", self.sim_state())

    def simulate(self, hair_dryer: bool | None, level: str, light: bool | None = None) -> None:
        parts = []
        if hair_dryer is not None:
            self.sim.set_hair_dryer(hair_dryer, level)
            parts.append(f"hairDryer={hair_dryer} level={level}")
        if light is not None:
            self.sim.set_light(light)
            parts.append(f"light={light}")
        self.audit("simulate", " ".join(parts))
        self.broadcast("sim", self.sim_state())

    def set_appliance(self, appliance_id: str, mode: str) -> None:
        self.sim.set_appliance(appliance_id, mode)          # ValueError on unknown id / mode
        self.audit("socket", f"{appliance_id}={mode}")
        self.broadcast("sim", self.sim_state())

    def unplug_all(self) -> None:
        self.sim.all_off()
        self.audit("socket", "all off")
        self.broadcast("sim", self.sim_state())

    def sim_state(self) -> dict:
        return {"source": self.source, "appliances": self.sim.appliances(),
                "nominalW": round(self.sim.nominal_w(), 1)}

    def set_thresholds(self, **kw) -> dict:
        t = self.s.thresholds
        mapping = {"warnW": "warn_w", "alarmW": "alarm_w", "holdS": "hold_s", "clearS": "clear_s",
                   "clearW": "clear_w", "minAlarmW": "min_alarm_w", "baselineMultiplier": "baseline_multiplier",
                   "baselineWindowS": "baseline_window_s"}
        changed = {}
        for k, v in kw.items():
            if v is None or k not in mapping:
                continue
            setattr(t, mapping[k], float(v))
            changed[k] = float(v)
        self.audit("thresholds", ", ".join(f"{k}={v:g}" for k, v in changed.items()))
        self.broadcast("thresholds", t.public())
        return t.public()

    def reset(self) -> None:
        self.audit("reset")
        if self.engine.state == alarm.RED:
            self.red_event_id = None
        tr = self.engine.reset(time.time())
        if self.last_reading_at is None or time.time() - self.last_reading_at > self.s.stale_after_s:
            self.engine.last_received = time.time()   # give the data a fresh stale window after a reset
        self._queue_transition(tr)

    # ================================================================== subscribers
    async def subscribe(self, phone_raw: str, lang: str) -> dict:
        phone = normalize_phone(phone_raw)
        if not phone:
            raise ValueError("invalid_phone")
        lang = lang if lang in ("en", "hy") else "en"
        row, created = await asyncio.to_thread(self.store.upsert_subscriber, phone, lang,
                                               self.s.subscriber_ttl_hours, secrets.token_urlsafe(16))
        res = await self.notifier.welcome(row)
        if not res.ok:
            self.welcome_failed += 1
        return {"ok": True, "created": created, "masked": mask_phone(phone),
                "expiresAt": iso(row["expires_at"]), "welcomeSent": res.ok,
                "welcomeError": None if res.ok else "welcome_failed"}

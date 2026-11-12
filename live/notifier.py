"""
SMS (§9, SMS-1..SMS-5).

Provider adapters share one interface:  send(to_e164, text) -> SendResult(ok, provider_msg_id, error)
  console          logs the message (default; use until the flow works)
  twilio           Twilio REST API (Messaging Service SID, or TWILIO_FROM number)
  android_gateway  an Android phone with a local SIM running an SMS-gateway app with an HTTP API
                   (format of "SMS Gateway for Android": POST {SMSGATE_URL}/message, basic auth,
                    {"message": ..., "phoneNumbers": [...]}; cloud mode URL is
                    https://api.sms-gate.app/3rdparty/v1, local mode http://<phone-ip>:8080)

Safety rails (cost + privacy):
  per-number cooldown SMS_COOLDOWN_SECONDS, per-number cap SMS_MAX_PER_NUMBER per 24 h,
  global cap SMS_MAX_PER_HOUR, concurrency 5, every attempt logged to sms_log
  (with a hash of the number, never the number itself).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .settings import Settings
from .store import Store, utcnow

log = logging.getLogger("live.sms")

E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw: str, default_cc: str = "+374") -> str | None:
    """'+374 77 12-34-56', '0037477123456', '077123456' -> '+37477123456'. None if not E.164."""
    if not isinstance(raw, str):
        return None
    s = re.sub(r"[\s\-().]", "", raw.strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0") and default_cc:        # national format, e.g. 077 123456 in Armenia
        s = default_cc + s[1:]
    elif not s.startswith("+") and s.isdigit() and default_cc and len(s) == 8:
        s = default_cc + s
    return s if E164.match(s) else None


def mask_phone(e164: str) -> str:
    """+37477123456 -> '+374 •• ••• 456' (numbers are never shown in full in the UI)."""
    if not e164 or len(e164) < 7:
        return "•••"
    return f"{e164[:4]} •• ••• {e164[-3:]}"


def phone_hash(e164: str) -> str:
    return hashlib.sha256(e164.encode()).hexdigest()[:32]


def is_gsm7(text: str) -> bool:
    basic = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")
    return all(c in basic for c in text)


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, **values) -> str:
    return template.format_map(SafeDict(values))


# ======================================================================= providers

@dataclass
class SendResult:
    ok: bool
    provider_msg_id: str | None = None
    error: str | None = None


class ConsoleProvider:
    name = "console"

    def __init__(self, settings: Settings):
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> SendResult:
        log.info("SMS (console) to %s: %s", mask_phone(to), text)
        self.sent.append((to, text))
        return SendResult(True, f"console-{len(self.sent)}")

    def configured(self) -> tuple[bool, str]:
        return True, "console: messages are only logged"


class TwilioProvider:
    name = "twilio"

    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None):
        self.s = s
        self.client = client or httpx.AsyncClient(timeout=15)

    def configured(self) -> tuple[bool, str]:
        if not (self.s.twilio_account_sid and self.s.twilio_auth_token):
            return False, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing"
        if not (self.s.twilio_messaging_service_sid or self.s.twilio_from):
            return False, "TWILIO_MESSAGING_SERVICE_SID (or TWILIO_FROM) missing"
        return True, "twilio configured"

    async def send(self, to: str, text: str) -> SendResult:
        ok, why = self.configured()
        if not ok:
            return SendResult(False, error=why)
        data = {"To": to, "Body": text}
        if self.s.twilio_messaging_service_sid:
            data["MessagingServiceSid"] = self.s.twilio_messaging_service_sid
        else:
            data["From"] = self.s.twilio_from
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.s.twilio_account_sid}/Messages.json"
        try:
            r = await self.client.post(url, data=data, auth=(self.s.twilio_account_sid, self.s.twilio_auth_token))
            body = r.json() if r.content else {}
        except Exception as e:
            return SendResult(False, error=f"twilio: {type(e).__name__}: {e}")
        if r.status_code >= 300:
            return SendResult(False, error=f"twilio {r.status_code}: {body.get('message', r.text[:200])}")
        return SendResult(True, body.get("sid"))


class AndroidGatewayProvider:
    name = "android_gateway"

    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None):
        self.s = s
        self.client = client or httpx.AsyncClient(timeout=15)

    def configured(self) -> tuple[bool, str]:
        if not self.s.smsgate_url:
            return False, "SMSGATE_URL missing"
        return True, "android gateway configured"

    async def send(self, to: str, text: str) -> SendResult:
        ok, why = self.configured()
        if not ok:
            return SendResult(False, error=why)
        url = self.s.smsgate_url if self.s.smsgate_url.endswith("/message") else self.s.smsgate_url + "/message"
        auth = (self.s.smsgate_username, self.s.smsgate_password) if self.s.smsgate_username else None
        try:
            r = await self.client.post(url, json={"message": text, "phoneNumbers": [to]}, auth=auth)
        except Exception as e:
            return SendResult(False, error=f"android_gateway: {type(e).__name__}: {e}")
        if r.status_code >= 300:
            return SendResult(False, error=f"android_gateway {r.status_code}: {r.text[:200]}")
        try:
            msg_id = (r.json() or {}).get("id")
        except ValueError:
            msg_id = None
        return SendResult(True, msg_id)


PROVIDERS = {"console": ConsoleProvider, "twilio": TwilioProvider, "android_gateway": AndroidGatewayProvider}


def make_provider(s: Settings):
    cls = PROVIDERS.get(s.sms_provider)
    if cls is None:
        log.warning("unknown SMS_PROVIDER %r, falling back to console", s.sms_provider)
        cls = ConsoleProvider
    return cls(s)


# ======================================================================= notifier

class Notifier:
    def __init__(self, settings: Settings, store: Store, provider=None):
        self.s = settings
        self.store = store
        self.provider = provider or make_provider(settings)
        self._loop = None
        self.inflight = 0                      # sends allowed but not yet logged (keeps caps exact)
        self.inflight_numbers: set[str] = set()
        self.last_error: str | None = None
        self.alerted: dict[int, dict[str, int | None]] = {}   # alert event id -> numbers that got it

    def _primitives(self):
        """asyncio locks belong to one event loop; (re)create them for the running one."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._sem, self._cap_lock = loop, asyncio.Semaphore(5), asyncio.Lock()
        return self._sem, self._cap_lock

    async def _db(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def fmt_time(self, ts: datetime | None = None) -> str:
        return (ts or utcnow()).astimezone(self.s.zone).strftime("%H:%M:%S")

    async def _allowed(self, number: str, kind: str) -> str | None:
        """Return None if allowed (and reserve a slot), or the reason it is blocked.
        Caller must call _release(number) after logging the attempt when allowed."""
        if not self.s.sms_enabled:
            return "SMS_ENABLED=false"
        async with self._primitives()[1]:
            now = utcnow()
            sent_hour = await self._db(self.store.sms_sent_since, now - timedelta(hours=1))
            if sent_hour + self.inflight >= self.s.sms_max_per_hour:
                return "global hourly cap reached"
            if kind not in ("welcome", "test", "recovery"):   # recovery only goes to who got the alert
                h = phone_hash(number)
                if number in self.inflight_numbers:
                    return "cooldown"
                if number not in self._team() and \
                        await self._db(self.store.sms_sent_since, now - timedelta(hours=24), h) >= self.s.sms_max_per_number:
                    return "per-number daily cap reached"
                last = await self._db(self.store.last_sms_to, h, kind)
                if last and (now - last).total_seconds() < self.s.sms_cooldown_s:
                    return "cooldown"
            self.inflight += 1
            self.inflight_numbers.add(number)
            return None

    def _team(self) -> set[str]:
        """SMS_ALWAYS_NOTIFY numbers (team / testers): they test many times a day, so the
        per-number daily cap does not apply to them. Cooldown and the global cap still do."""
        return {p for p in (normalize_phone(n) for n in self.s.sms_always_notify) if p}

    def _release(self, number: str) -> None:
        self.inflight = max(0, self.inflight - 1)
        self.inflight_numbers.discard(number)

    async def send_one(self, number: str, text: str, *, kind: str, subscriber_id: int | None = None,
                       event_id: int | None = None) -> SendResult:
        async with self._primitives()[0]:
            blocked = await self._allowed(number, kind)
            if blocked:
                await self._db(self.store.log_sms, subscriber_id=subscriber_id, event_id=event_id,
                               provider=self.provider.name, status="skipped", provider_msg_id=None,
                               error=blocked, phone_hash=phone_hash(number), kind=kind)
                return SendResult(False, error=blocked)
            try:
                try:
                    res = await self.provider.send(number, text)
                except Exception as e:       # a provider bug must never stop the fan-out
                    res = SendResult(False, error=f"{type(e).__name__}: {e}")
                if not res.ok:
                    self.last_error = res.error
                    log.warning("SMS to %s failed: %s", mask_phone(number), res.error)
                await self._db(self.store.log_sms, subscriber_id=subscriber_id, event_id=event_id,
                               provider=self.provider.name, status="sent" if res.ok else "failed",
                               provider_msg_id=res.provider_msg_id, error=res.error,
                               phone_hash=phone_hash(number), kind=kind)
            finally:
                self._release(number)
            if res.ok and subscriber_id is not None:
                await self._db(self.store.mark_sms, subscriber_id)
            return res

    async def _targets(self) -> dict[str, int | None]:
        subs = await self._db(self.store.active_subscribers)
        targets: dict[str, int | None] = {s["phone_e164"]: s["id"] for s in subs}
        for p in self._team():
            targets.setdefault(p, None)
        return targets

    async def _fan_out(self, text: str, kind: str, event_id: int | None,
                       targets: dict[str, int | None] | None = None) -> dict[str, int | None]:
        """Send to every target; returns the numbers the provider accepted."""
        targets = await self._targets() if targets is None else targets
        if not targets:
            return {}
        items = list(targets.items())
        results = await asyncio.gather(*(self.send_one(p, text, kind=kind, subscriber_id=sid, event_id=event_id)
                                         for p, sid in items))
        return {p: sid for (p, sid), r in zip(items, results) if r.ok}

    def alert_text(self, power_w: float, ts: datetime | None = None, usual_w: float | None = None) -> str:
        usual = f"{usual_w:,.0f}" if usual_w is not None else "0"
        return render(self.s.sms_template, station=self.s.station_name, powerW=f"{power_w:,.0f}",
                      usualW=usual, alarmW=f"{self.s.thresholds.alarm_w:,.0f}", time=self.fmt_time(ts))

    async def alert(self, power_w: float, event_id: int | None, ts: datetime | None = None,
                    usual_w: float | None = None, text: str | None = None) -> int:
        """RED entry: send the spike SMS to everyone. Returns how many were delivered to the provider.
        `text` = tailored text from advice.py; without it SMS_TEMPLATE is used."""
        sent = await self._fan_out(text or self.alert_text(power_w, ts, usual_w), "alert", event_id)
        if event_id is not None:
            self.alerted[event_id] = sent
            while len(self.alerted) > 50:
                self.alerted.pop(next(iter(self.alerted)))
        return len(sent)

    def recovery_template_text(self, power_w: float, ts: datetime | None = None) -> str:
        return render(self.s.sms_recovery_template, station=self.s.station_name, powerW=f"{power_w:,.0f}",
                      time=self.fmt_time(ts))

    async def recovery(self, alert_event_id: int | None, text: str, event_id: int | None = None) -> int:
        """Back to normal (or data lost): only to the numbers that received this episode's alert,
        and only if they are still subscribed (team numbers always)."""
        got = self.alerted.pop(alert_event_id, {}) if alert_event_id is not None else {}
        if not got:
            return 0
        active = await self._targets()
        targets = {p: sid for p, sid in got.items() if p in active}
        return len(await self._fan_out(text, "recovery", event_id, targets))

    def unsub_url(self, token: str) -> str:
        return f"{self.s.public_base_url}/u/{token}" if self.s.public_base_url else f"/u/{token}"

    async def welcome(self, subscriber: dict) -> SendResult:
        text = render(self.s.sms_welcome_template, station=self.s.station_name,
                      unsubUrl=self.unsub_url(subscriber["unsubscribe_token"]))
        return await self.send_one(subscriber["phone_e164"], text, kind="welcome", subscriber_id=subscriber["id"])

    async def test(self, number: str) -> SendResult:
        text = f"E2B test SMS from {self.s.station_name}. {self.fmt_time()}"
        return await self.send_one(number, text, kind="test")

    def status(self) -> dict:
        ok, why = self.provider.configured()
        return {"provider": self.provider.name, "configured": ok, "detail": why,
                "enabled": self.s.sms_enabled, "lastError": self.last_error}

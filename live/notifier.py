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

from . import advice
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


EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]{2,}$")


def normalize_email(raw: str) -> str | None:
    """'  Ann@Example.AM ' -> 'ann@example.am'; None if it does not look like an address."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    return s if len(s) <= 254 and EMAIL.match(s) else None


def mask_address(addr: str) -> str:
    """Phone: '+374 •• ••• 456'; email: 'a•••@example.am' (never shown in full)."""
    if addr and "@" in addr:
        user, _, domain = addr.partition("@")
        return f"{user[:1]}•••@{domain}"
    return mask_phone(addr)


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

    async def send(self, to: str, text: str, **meta) -> SendResult:
        log.info("%s (console) to %s: %s", "Email" if "@" in to else "SMS", mask_address(to), text)
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

    async def send(self, to: str, text: str, **meta) -> SendResult:
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

    async def send(self, to: str, text: str, **meta) -> SendResult:
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


class ResendProvider:
    """Email through the Resend API (https://resend.com/docs/api-reference/emails/send-email).
    Resend allows about 2 requests per second by default, so sends are spaced and a 429 is retried once."""
    name = "resend"
    MIN_GAP_S = 0.55

    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None):
        self.s = s
        self.client = client or httpx.AsyncClient(timeout=15)
        self._lock: asyncio.Lock | None = None
        self._loop = None
        self._last = 0.0

    def configured(self) -> tuple[bool, str]:
        if not self.s.resend_api_key:
            return False, "RESEND_API_KEY missing"
        if not self.s.email_from:
            return False, "EMAIL_FROM missing"
        return True, "resend configured" + (" (onboarding@resend.dev only delivers to your own Resend address)"
                                            if "resend.dev" in self.s.email_from else "")

    @staticmethod
    def subject_of(text: str) -> str:
        parts = re.split(r"(?<=[.։])\s", text.strip())
        first = parts[0]
        if len(first) < 30 and len(parts) > 1:          # "GridBox ԱՀԱԶԱՆԳ." alone says too little
            first = f"{first} {parts[1]}"
        first = first.rstrip(".։ ")
        return (first[:117] + "…") if len(first) > 120 else (first or "GridBox alert")

    @staticmethod
    def html_of(text: str, unsub_url: str | None) -> str:
        from html import escape
        paras = "".join(f"<p style='margin:0 0 12px'>{escape(p).replace(chr(10), '<br>')}</p>"
                        for p in text.split("\n\n") if p.strip())
        foot = (f"<p style='margin:20px 0 0;font-size:12px;color:#5f6d74'>You get this because you signed up at the "
                f"GridBox live demo. <a href='{escape(unsub_url)}'>Stop these emails</a></p>") if unsub_url else ""
        return (f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.45;"
                f"color:#1d2b33;max-width:560px'>{paras}{foot}</div>")

    async def send(self, to: str, text: str, subject: str | None = None, unsub_url: str | None = None,
                   **meta) -> SendResult:
        ok, why = self.configured()
        if not ok:
            return SendResult(False, error=why)
        body = {"from": self.s.email_from, "to": [to], "subject": subject or self.subject_of(text),
                "text": text + (f"\n\nStop these emails: {unsub_url}" if unsub_url else ""),
                "html": self.html_of(text, unsub_url)}
        if self.s.email_reply_to:
            body["reply_to"] = self.s.email_reply_to
        headers = {"Authorization": f"Bearer {self.s.resend_api_key}"}
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._lock = loop, asyncio.Lock()
        for attempt in (1, 2):
            async with self._lock:                       # one request at a time, spaced for the rate limit
                wait = self.MIN_GAP_S - (loop.time() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self.client.post(self.s.resend_url, json=body, headers=headers)
                except Exception as e:
                    return SendResult(False, error=f"resend: {type(e).__name__}: {e}")
                finally:
                    self._last = loop.time()
            if r.status_code == 429 and attempt == 1:
                try:
                    delay = min(5.0, float(r.headers.get("retry-after", "1")))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            break
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        if r.status_code >= 300:
            return SendResult(False, error=f"resend {r.status_code}: {data.get('message', r.text[:200])}")
        return SendResult(True, data.get("id"))


PROVIDERS = {"console": ConsoleProvider, "twilio": TwilioProvider, "android_gateway": AndroidGatewayProvider}


def make_provider(s: Settings):
    if s.alert_channel == "email":
        return ResendProvider(s)
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
        self.alerted: dict[int, dict] = {}   # alert event id -> {address: (subscriber id, lang)} that got it
        self.channel = settings.alert_channel if settings.alert_channel in ("sms", "email") else "sms"
        self._tokens: dict[str, str] = {}    # address -> unsubscribe token (email footer link)
        self.last_fanout: dict = {}

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
        """SMS_ALWAYS_NOTIFY numbers / EMAIL_ALWAYS_NOTIFY addresses (team / testers): they test many
        times a day, so the per-number daily cap does not apply to them. Cooldown and the global cap still do."""
        if self.channel == "email":
            return {e for e in (normalize_email(x) for x in self.s.email_always_notify) if e}
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
            meta = {"kind": kind}
            if self.channel == "email":
                tok = self._tokens.get(number)
                meta["unsub_url"] = self.unsub_url(tok) if tok else None
            try:
                try:
                    res = await self.provider.send(number, text, **meta)
                except Exception as e:       # a provider bug must never stop the fan-out
                    res = SendResult(False, error=f"{type(e).__name__}: {e}")
                if not res.ok:
                    self.last_error = res.error
                    log.warning("%s to %s failed: %s", "Email" if self.channel == "email" else "SMS",
                                mask_address(number), res.error)
                await self._db(self.store.log_sms, subscriber_id=subscriber_id, event_id=event_id,
                               provider=self.provider.name, status="sent" if res.ok else "failed",
                               provider_msg_id=res.provider_msg_id, error=res.error,
                               phone_hash=phone_hash(number), kind=kind)
            finally:
                self._release(number)
            if res.ok and subscriber_id is not None:
                await self._db(self.store.mark_email if self.channel == "email" else self.store.mark_sms,
                               subscriber_id)
            return res

    def lang_for(self, subscriber_lang: str | None, team: bool = False) -> str:
        """SMS_LANG=per_user: the language picked on the join page (team: SMS_TEAM_LANG);
        SMS_LANG=en / hy / both: everyone gets that."""
        if self.s.sms_lang in ("en", "hy", "both"):
            return self.s.sms_lang
        if team:
            return self.s.sms_team_lang if self.s.sms_team_lang in ("en", "hy", "both") else "both"
        return subscriber_lang if subscriber_lang in ("en", "hy") else "en"

    async def _targets(self, for_recovery: bool = False) -> dict[str, tuple[int | None, str]]:
        """address (phone or email) -> (subscriber id or None for team members, language).
        Email with EMAIL_RECIPIENTS=latest: only the most recent sign-up (if not unsubscribed);
        for a "restored" message every stored, still-subscribed address may qualify (it goes to
        whoever got the outage alert, even if someone newer has signed up since)."""
        if self.channel == "email":
            if self.s.email_recipients == "latest":
                if for_recovery:
                    subs = await self._db(self.store.subscribed_email_rows)
                else:
                    row = await self._db(self.store.latest_email_subscriber)
                    subs = [row] if row and row.get("unsubscribed_at") is None else []
            else:
                subs = await self._db(self.store.active_email_subscribers)
            for s in subs:
                self._tokens[s["email"]] = s["unsubscribe_token"]
            targets = {s["email"]: (s["id"], self.lang_for(s.get("lang"))) for s in subs}
        else:
            subs = await self._db(self.store.active_subscribers)
            targets = {s["phone_e164"]: (s["id"], self.lang_for(s.get("lang"))) for s in subs}
        for p in self._team():
            targets.setdefault(p, (None, self.lang_for(None, team=True)))
        return targets

    async def _fan_out(self, texts, kind: str, event_id: int | None,
                       targets: dict[str, tuple[int | None, str]] | None = None) -> dict[str, tuple[int | None, str]]:
        """Send to every target in its language; returns the targets the provider accepted.
        `texts` = {"en", "hy", "both"} from advice.py, or one plain string (templates)."""
        targets = await self._targets() if targets is None else targets
        self.last_fanout = {"kind": kind, "targets": len(targets), "sent": 0,
                            "byLang": {lang: sum(1 for _, l in targets.values() if l == lang)
                                       for lang in ("en", "hy", "both")}}
        if not targets:
            return {}
        items = list(targets.items())
        results = await asyncio.gather(*(self.send_one(p, advice.compose(texts, lang), kind=kind, subscriber_id=sid,
                                                       event_id=event_id) for p, (sid, lang) in items))
        sent = {p: t for (p, t), r in zip(items, results) if r.ok}
        self.last_fanout["sent"] = len(sent)
        return sent

    def alert_text(self, power_w: float, ts: datetime | None = None, usual_w: float | None = None) -> str:
        usual = f"{usual_w:,.0f}" if usual_w is not None else "0"
        return render(self.s.sms_template, station=self.s.station_name, powerW=f"{power_w:,.0f}",
                      usualW=usual, alarmW=f"{self.s.thresholds.alarm_w:,.0f}", time=self.fmt_time(ts))

    async def alert(self, power_w: float, event_id: int | None, ts: datetime | None = None,
                    usual_w: float | None = None, text=None) -> int:
        """RED entry: send the spike SMS to everyone. Returns how many were delivered to the provider.
        `text` = tailored texts from advice.py ({"en","hy","both"}); without it SMS_TEMPLATE is used."""
        sent = await self._fan_out(text or self.alert_text(power_w, ts, usual_w), "alert", event_id)
        if event_id is not None:
            self.alerted[event_id] = sent
            while len(self.alerted) > 50:
                self.alerted.pop(next(iter(self.alerted)))
        return len(sent)

    def recovery_template_text(self, power_w: float, ts: datetime | None = None) -> str:
        return render(self.s.sms_recovery_template, station=self.s.station_name, powerW=f"{power_w:,.0f}",
                      time=self.fmt_time(ts))

    async def recovery(self, alert_event_id: int | None, text, event_id: int | None = None) -> int:
        """Back to normal (or data lost): only to the numbers that received this episode's alert,
        and only if they are still subscribed (team numbers always)."""
        got = self.alerted.pop(alert_event_id, {}) if alert_event_id is not None else {}
        if not got and alert_event_id is not None:      # e.g. the app restarted during the outage
            got = await self._recipients_from_log(alert_event_id)
        if not got:
            self.last_fanout = {"kind": "recovery", "targets": 0, "sent": 0, "byLang": {}}
            return 0
        active = await self._targets(for_recovery=True)
        targets = {p: sid for p, sid in got.items() if p in active}
        return len(await self._fan_out(text, "recovery", event_id, targets))

    async def _recipients_from_log(self, event_id: int) -> dict:
        """Who got the alert for `event_id`, rebuilt from sms_log (phone numbers are only stored hashed)."""
        rows = await self._db(self.store.sms_recipients, event_id)
        if not rows:
            return {}
        ids = {r["subscriber_id"] for r in rows if r["subscriber_id"] is not None}
        hashes = {r["phone_hash"] for r in rows}
        targets = await self._targets(for_recovery=True)
        return {p: t for p, t in targets.items() if (t[0] is not None and t[0] in ids)
                or (t[0] is None and phone_hash(p) in hashes)}

    async def send_to_all(self, texts, kind: str, event_id: int | None = None) -> int:
        """Fan a message out to every active subscriber + team number (e.g. the DI no-data warning)."""
        return len(await self._fan_out(texts, kind, event_id))

    def unsub_url(self, token: str) -> str:
        return f"{self.s.public_base_url}/u/{token}" if self.s.public_base_url else f"/u/{token}"

    async def welcome(self, subscriber: dict) -> SendResult:
        url = self.unsub_url(subscriber["unsubscribe_token"])
        address = subscriber.get("email") or subscriber["phone_e164"]
        self._tokens[address] = subscriber["unsubscribe_token"]
        if self.s.sms_style == "template":
            text = render(self.s.sms_welcome_template, station=self.s.station_name, unsubUrl=url)
        else:
            text = advice.compose(advice.welcome_sms(station=self.s.station_name, unsub_url=url, channel=self.channel),
                                  self.lang_for(subscriber.get("lang")))
        return await self.send_one(address, text, kind="welcome", subscriber_id=subscriber["id"])

    async def test(self, number: str) -> SendResult:
        texts = advice.test_sms(station=self.s.station_name, time=self.fmt_time())
        return await self.send_one(number, advice.compose(texts, self.lang_for(None, team=True)), kind="test")

    def status(self) -> dict:
        ok, why = self.provider.configured()
        return {"channel": self.channel, "provider": self.provider.name, "configured": ok, "detail": why,
                "enabled": self.s.sms_enabled, "lastError": self.last_error}

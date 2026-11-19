"""ALERT_CHANNEL=email: sign-up with an email address, alerts through the Resend API."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live import LiveDemo
from live.notifier import Notifier, ResendProvider, mask_address, normalize_email
from live.settings import Settings
from live.store import Store

TOKEN = "secret-token"


def run(coro):
    return asyncio.run(coro)


def wait_for(fn, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


@pytest.mark.parametrize("raw,ok", [(" Ann@Example.AM ", "ann@example.am"), ("a.b+c@mail.co.uk", "a.b+c@mail.co.uk"),
                                    ("no-at-sign", None), ("a@b", None), ("a b@c.am", None), ("", None),
                                    ("x@y.am, z@w.am", None)])
def test_normalize_email(raw, ok):
    assert normalize_email(raw) == ok


def test_mask_address():
    assert mask_address("ann@example.am") == "a•••@example.am"
    assert mask_address("+37477123456") == "+374 •• ••• 456"


# ---------------------------------------------------------------- Resend provider

def resend(handler, **kw):
    s = Settings(alert_channel="email", resend_api_key="re_test", email_from="E2b Alerts <alerts@e2b.am>", **kw)
    p = ResendProvider(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    p.MIN_GAP_S = 0
    return p


def test_resend_request_shape():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"id": "em_1"})

    res = run(resend(handler, email_reply_to="team@e2b.am").send(
        "ann@example.am", "E2B ALERT: DigiTech Substation stopped supplying electricity at 14:02:11. Please check.",
        unsub_url="https://x.test/u/tok"))
    assert res.ok and res.provider_msg_id == "em_1"
    b = seen["body"]
    assert seen["url"] == "https://api.resend.com/emails" and seen["auth"] == "Bearer re_test"
    assert b["from"] == "E2b Alerts <alerts@e2b.am>" and b["to"] == ["ann@example.am"] and b["reply_to"] == "team@e2b.am"
    assert b["subject"] == "E2B ALERT: DigiTech Substation stopped supplying electricity at 14:02:11"
    assert "https://x.test/u/tok" in b["html"] and "https://x.test/u/tok" in b["text"]


def test_resend_retries_once_on_429_and_reports_errors():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"}) if len(calls) == 1 else httpx.Response(200, json={"id": "x"})

    assert run(resend(handler).send("a@b.am", "hi")).ok and len(calls) == 2
    bad = run(resend(lambda r: httpx.Response(403, json={"message": "domain not verified"})).send("a@b.am", "hi"))
    assert not bad.ok and "domain not verified" in bad.error


def test_resend_not_configured():
    p = ResendProvider(Settings(alert_channel="email"))
    assert p.configured()[0] is False
    assert not run(p.send("a@b.am", "hi")).ok


def test_armenian_subject_is_meaningful():
    s = ResendProvider.subject_of("E2B ԱՀԱԶԱՆԳ. DigiTech Substation ենթակայանը 14:02:11-ից չի մատակարարում։ Խնդրում")
    assert s.startswith("E2B ԱՀԱԶԱՆԳ. DigiTech Substation")


# ---------------------------------------------------------------- notifier fan-out by email

class FakeEmail:
    name = "resend"

    def __init__(self):
        self.sent = []

    async def send(self, to, text, **meta):
        from live.notifier import SendResult
        self.sent.append((to, text, meta))
        return SendResult(True, f"em_{len(self.sent)}")

    def configured(self):
        return True, "fake"


def test_email_fan_out_language_and_unsubscribe_link(tmp_path, db_url):
    from live.advice import di_outage_sms
    s = Settings(alert_channel="email", public_base_url="https://x.test", email_always_notify=["Team@E2B.am"],
                 sms_always_notify=["+37499000111"])
    store = Store(db_url, str(tmp_path / "e.db"))
    prov = FakeEmail()
    n = Notifier(s, store, prov)
    store.upsert_email_subscriber("en@x.am", "en", 24, "tok-en")
    store.upsert_email_subscriber("hy@x.am", "hy", 24, "tok-hy")
    store.upsert_subscriber("+37477123450", "en", 24, "tok-phone")      # phone sign-ups are not used in email mode
    texts = di_outage_sms(station="Demo", time="14:02:11")
    assert run(n.alert(0, event_id=1, text=texts)) == 3
    got = {to: (t, m) for to, t, m in prov.sent}
    assert set(got) == {"en@x.am", "hy@x.am", "team@e2b.am"}
    assert got["en@x.am"][0] == texts["en"] and got["hy@x.am"][0] == texts["hy"] and got["team@e2b.am"][0] == texts["both"]
    assert got["en@x.am"][1]["unsub_url"] == "https://x.test/u/tok-en"
    assert got["team@e2b.am"][1]["unsub_url"] is None
    assert run(n.recovery(1, texts, 2)) == 3                              # restored to the same addresses
    assert n.status()["channel"] == "email"


# ---------------------------------------------------------------- end to end through the API

@pytest.fixture
def email_app(tmp_path, db_url):
    s = Settings(enabled=True, source="simulator", live_signal="di", alert_channel="email",
                 sqlite_path=str(tmp_path / "live.db"), database_url=db_url, admin_token=TOKEN,
                 di_off_hold_s=2, di_on_hold_s=1, sms_cooldown_s=0, public_base_url="https://x.test")
    prov = FakeEmail()
    demo = LiveDemo(s, provider=prov, start_mqtt=False)
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    with TestClient(app) as c:
        c.prov = prov
        yield c


def test_email_signup_alert_and_unsubscribe(email_app):
    c = email_app
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/api/demo/channel").json() == {"channel": "email"}
    assert c.get("/api/live/state").json()["alertChannel"] == "email"
    assert c.post("/api/demo/subscribe", json={"email": "nope", "consent": True}).json()["detail"] == "invalid_email"
    r = c.post("/api/demo/subscribe", json={"email": "Ann@Example.am", "lang": "hy", "consent": True}).json()
    assert r["channel"] == "email" and r["masked"] == "a•••@example.am" and r["welcomeSent"] is True
    to, text, meta = c.prov.sent[0]
    assert to == "ann@example.am" and text.startswith("Բարի գալուստ") and "նամակ" in text
    unsub = meta["unsub_url"]
    assert unsub.startswith("https://x.test/u/")

    wait_for(lambda: c.get("/api/live/state").json()["status"] == "green")
    c.post("/api/demo/admin/sim/supply", json={"on": False}, headers=auth)
    wait_for(lambda: [t for to, t, m in c.prov.sent if "E2B ԱՀԱԶԱՆԳ" in t])
    c.post("/api/demo/admin/sim/supply", json={"on": True}, headers=auth)
    wait_for(lambda: [t for to, t, m in c.prov.sent if t.startswith("E2B.")])     # Armenian "restored"
    assert c.get("/api/live/diag").json()["subscriberCount"] == 1

    r = c.post("/api/demo/admin/test-sms", json={"email": "team@e2b.am"}, headers=auth).json()
    assert r["ok"] is True and c.prov.sent[-1][0] == "team@e2b.am"
    assert c.post("/api/demo/admin/test-sms", json={"email": "x"}, headers=auth).status_code == 400

    assert "unsubscribed" in c.get(unsub.replace("https://x.test", "")).text
    assert c.get("/api/live/diag").json()["subscriberCount"] == 0


def test_join_page_and_sms_mode_unchanged(tmp_path, db_url):
    s = Settings(enabled=True, source="simulator", sqlite_path=str(tmp_path / "s.db"), database_url=db_url)
    from live.notifier import ConsoleProvider
    demo = LiveDemo(s, provider=ConsoleProvider(s), start_mqtt=False)
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    with TestClient(app) as c:
        assert c.get("/api/demo/channel").json() == {"channel": "sms"}
        assert c.get("/demo/join").status_code == 200
        assert c.post("/api/demo/subscribe", json={"email": "a@b.am", "consent": True}).json()["detail"] == "invalid_phone"
        assert c.post("/api/demo/subscribe", json={"phone": "+37477123456", "consent": True}).json()["channel"] == "sms"

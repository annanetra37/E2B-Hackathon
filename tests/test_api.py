"""End-to-end through the HTTP API with the simulator as source (OPS-1, T3, T7, T9)."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live import LiveDemo
from live.notifier import ConsoleProvider
from live.settings import Settings, Thresholds

TOKEN = "secret-token"


def wait_for(fn, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


@pytest.fixture
def client(tmp_path, db_url):
    s = Settings(enabled=True, source="simulator", sqlite_path=str(tmp_path / "live.db"), admin_token=TOKEN,
                 database_url=db_url,
                 thresholds=Thresholds(hold_s=2, clear_s=2), sms_always_notify=[], public_base_url="https://x.test")
    provider = ConsoleProvider(s)
    demo = LiveDemo(s, provider=provider, start_mqtt=False)
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    with TestClient(app) as c:
        c.provider = provider
        yield c


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_disabled_returns_404(tmp_path):
    demo = LiveDemo(Settings(enabled=False))
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    with TestClient(app) as c:
        assert c.get("/api/live/state").status_code == 404
        assert c.get("/live").status_code == 404


def test_pages_served(client):
    for path in ("/live", "/live?kiosk=1", "/demo/join", "/demo/admin"):
        assert client.get(path).status_code == 200
    qr = client.get("/api/demo/qr.svg")
    assert qr.status_code == 200 and qr.text.startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in qr.text


def test_full_flow_simulator_red_and_sms(client):
    r = client.post("/api/demo/subscribe", json={"phone": "+374 77 123456", "lang": "en", "consent": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["masked"] == "+374 •• ••• 456" and body["welcomeSent"] is True
    assert "https://x.test/u/" in client.provider.sent[0][1]

    wait_for(lambda: client.get("/api/live/state").json()["status"] == "green")
    st = client.get("/api/live/state").json()
    assert st["source"] == "simulator" and 8 <= st["lastReading"]["powerW"] <= 10      # LED on

    client.post("/api/demo/admin/simulate", json={"light": False}, headers=auth())
    wait_for(lambda: client.get("/api/live/state").json()["lastReading"]["powerW"] == 0)
    assert client.get("/api/live/state").json()["status"] == "green"

    assert client.post("/api/demo/admin/simulate", json={"hairDryer": True, "level": "high"},
                       headers=auth()).status_code == 200
    wait_for(lambda: client.get("/api/live/state").json()["status"] == "red", timeout=8)
    ev = wait_for(lambda: [e for e in client.get("/api/live/events").json()["events"]
                           if e["toState"] == "red" and e["smsSent"] == 1])
    assert ev[0]["peakW"] > 1900
    assert any("E2B ALERT: Sudden power spike" in t and "(usual " in t and "limit 1,000 W" in t
               for _, t in client.provider.sent)

    client.post("/api/demo/admin/simulate", json={"hairDryer": False}, headers=auth())
    wait_for(lambda: client.get("/api/live/state").json()["status"] == "green", timeout=8)

    hist = client.get("/api/live/history?seconds=120").json()["readings"]
    assert len(hist) >= 5 and max(h["powerW"] for h in hist) > 1900
    diag = client.get("/api/live/diag").json()
    assert diag["subscriberCount"] == 1 and diag["db"]["ok"] and diag["counters"]["stored"] > 0
    assert "+37477123456" not in str(diag)


def test_subscribe_validation_and_rate_limit(client):
    assert client.post("/api/demo/subscribe", json={"phone": "+374 77 123456", "consent": False}).json()["detail"] \
        == "consent_required"
    assert client.post("/api/demo/subscribe", json={"phone": "12", "consent": True}).json()["detail"] == "invalid_phone"
    codes = [client.post("/api/demo/subscribe", json={"phone": f"+3747712345{i}", "consent": True}).status_code
             for i in range(5)]
    assert codes[-1] == 429


def test_unsubscribe_link(client):
    client.post("/api/demo/subscribe", json={"phone": "+37477123456", "consent": True})
    link = next(t for _, t in client.provider.sent if "/u/" in t).split("https://x.test")[1]
    assert "unsubscribed" in client.get(link).text
    assert "already used" in client.get(link).text


def test_admin_requires_token(client):
    assert client.post("/api/demo/admin/reset").status_code == 401
    assert client.post("/api/demo/admin/reset", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/demo/admin/reset", headers=auth()).status_code == 200


def test_admin_thresholds_source_delete(client):
    r = client.post("/api/demo/admin/thresholds", json={"warnW": 500, "alarmW": 1500}, headers=auth())
    assert r.json()["alarmW"] == 1500
    assert client.get("/api/live/state").json()["thresholds"]["warnW"] == 500
    assert client.post("/api/demo/admin/source", json={"source": "bogus"}, headers=auth()).status_code == 400
    assert client.post("/api/demo/admin/source", json={"source": "live"}, headers=auth()).json()["source"] == "live"
    client.post("/api/demo/subscribe", json={"phone": "+37477123456", "consent": True})
    assert client.request("DELETE", "/api/demo/admin/subscribers", headers=auth()).json()["deleted"] == 1
    r = client.post("/api/demo/admin/test-sms", json={"phone": "+37477000111"}, headers=auth())
    assert r.json()["ok"] is True
    log = client.get("/api/live/diag").json()["adminLog"]
    assert {"thresholds", "source", "delete-subscribers", "test-sms"} <= {e["action"] for e in log}

# The SSE stream (/api/live/stream) is checked against a real server in tests/smoke_live.py:
# Starlette's TestClient cannot close an endless stream.

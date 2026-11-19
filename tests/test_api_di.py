"""T4-T8 end to end through the app in on/off (DI) mode: bridge messages in, page state,
outage / restored SMS out, restart without a second SMS, grey without SMS, simulator switch."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live import LiveDemo
from live.notifier import ConsoleProvider
from live.settings import Settings

TOKEN = "secret-token"
DI_TOPIC = "e2b/demo/yvn-01/di"
STATUS_TOPIC = "e2b/demo/yvn-01/status"


def wait_for(fn, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


def make(tmp_path, db_url, source="live", **kw):
    s = Settings(enabled=True, source=source, live_signal="di", sqlite_path=str(tmp_path / "live.db"),
                 database_url=db_url, admin_token=TOKEN, sms_always_notify=["+37499000111"],
                 di_off_hold_s=3, di_on_hold_s=2, stale_after_s=kw.pop("stale", 10), sms_cooldown_s=0,
                 public_base_url="https://x.test", **kw)
    provider = ConsoleProvider(s)
    demo = LiveDemo(s, provider=provider, start_mqtt=False)
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    return demo, provider, app


def send(demo, topic, payload):
    svc = demo.service
    svc.loop.call_soon_threadsafe(svc.on_mqtt_message, topic, json.dumps(payload).encode())


def di(demo, value, offset_s, seq=0):
    ts = (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")
    send(demo, DI_TOPIC, {"station": "YVN-DEMO-01", "di1": value, "level": "LOW" if value else "HIGH",
                          "ts": ts.replace("+00:00", "Z"), "seq": seq, "src": "di-bridge"})


def state(c):
    return c.get("/api/live/state").json()


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def texts(provider, part):
    """Team-number SMS containing `part` (team numbers get Armenian + English in one SMS)."""
    return [t for to, t in provider.sent if to == "+37499000111" and part in t]


def test_outage_and_restored_flow(tmp_path, db_url):
    demo, provider, app = make(tmp_path, db_url)
    with TestClient(app) as c:
        st = state(c)
        assert st["signal"] == "di" and st["status"] == "grey" and st["supplyOn"] is None
        send(demo, STATUS_TOPIC, {"station": "YVN-DEMO-01", "bridge": "online"})
        for i, off in enumerate((-9, -8, -7, -6)):
            di(demo, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        assert state(c)["supplyOn"] is True and not texts(provider, "GridBox")      # first state: no SMS

        di(demo, 0, -5, seq=4)                                                 # bounce: 1 s of OFF
        di(demo, 1, -4, seq=5)
        time.sleep(0.5)
        assert state(c)["status"] == "green"

        for i, off in enumerate((-3, -2, -1, 0), start=6):                    # held OFF for 3 s
            di(demo, 0, off, seq=i)
        wait_for(lambda: state(c)["status"] == "red")
        outage = wait_for(lambda: texts(provider, "GridBox ALERT"))
        assert "stopped supplying electricity" in outage[0] and "DigiTech Substation" in outage[0]
        assert len(outage) == 1
        assert texts(provider, "GridBox ԱՀԱԶԱՆԳ")                                 # team: both languages
        ev = wait_for(lambda: [e for e in c.get("/api/live/events").json()["events"]
                               if e["toState"] == "red" and e["smsSent"] == 1])
        assert ev[0]["message"] == "Supply lost"

        for i, off in enumerate((1, 2, 3), start=10):                         # ON again for 2 s
            di(demo, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        restored = wait_for(lambda: texts(provider, "GridBox OK"))
        assert "supplying electricity again" in restored[0] and "outage lasted" in restored[0]
        evs = c.get("/api/live/events").json()["events"]
        lost = [e for e in evs if e["message"] == "Supply lost"][0]
        assert lost["durationS"] and lost["durationS"] >= 3
        assert any(e["message"].startswith("Supply restored ·") for e in evs)

        diag = c.get("/api/live/diag").json()
        assert diag["signal"] == "di" and diag["di"]["bridgeOnline"] is True and diag["di"]["lastDi1"] == 1
        assert diag["di"]["seqGaps"] == 0
        hist = c.get("/api/live/history?seconds=600").json()["readings"]
        assert {r["supplyOn"] for r in hist} >= {True, False} and all(r["powerW"] is None for r in hist)


def test_restart_during_outage_sends_no_second_sms(tmp_path, db_url):
    demo, provider, app = make(tmp_path, db_url)
    with TestClient(app) as c:
        for i, off in enumerate((-9, -8, -7)):
            di(demo, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        for i, off in enumerate((-4, -2, 0), start=3):
            di(demo, 0, off, seq=i)
        wait_for(lambda: state(c)["status"] == "red")
        wait_for(lambda: texts(provider, "GridBox ALERT"))
        time.sleep(0.3)                                       # let the state be saved

    demo2, provider2, app2 = make(tmp_path, db_url)           # "redeploy": same database, fresh process
    with TestClient(app2) as c:
        assert state(c)["status"] == "grey" and state(c)["supplyOn"] is False   # last known: off
        for i, off in enumerate((-3, -2, -1, 0)):             # retained OFF + heartbeats
            di(demo2, 0, off, seq=i)
        wait_for(lambda: state(c)["status"] == "red")
        time.sleep(0.5)
        assert not texts(provider2, "GridBox ALERT")              # no second outage SMS
        for i, off in enumerate((1, 2, 3), start=4):
            di(demo2, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        restored = wait_for(lambda: texts(provider2, "GridBox OK"))   # recipients rebuilt from the SMS log
        assert len(restored) == 1


def test_silence_goes_grey_without_outage_sms_and_bridge_offline(tmp_path, db_url):
    demo, provider, app = make(tmp_path, db_url, stale=2)
    with TestClient(app) as c:
        for i, off in enumerate((-3, -2, -1, 0)):
            di(demo, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        send(demo, STATUS_TOPIC, {"station": "YVN-DEMO-01", "bridge": "offline"})   # last will
        wait_for(lambda: state(c)["status"] == "grey", timeout=8)
        assert state(c)["bridgeOnline"] is False
        time.sleep(0.5)
        assert not texts(provider, "GridBox")                     # grey is not an outage
        for i, off in enumerate((-1, 0), start=4):            # back, same state: green, no SMS
            di(demo, 1, off, seq=i)
        wait_for(lambda: state(c)["status"] == "green")
        assert not texts(provider, "GridBox")


def test_rejected_payloads_counted(tmp_path, db_url):
    demo, provider, app = make(tmp_path, db_url)
    with TestClient(app) as c:
        send(demo, DI_TOPIC, {"di1": "maybe"})
        send(demo, DI_TOPIC, {"station": "OTHER-99", "di1": 1})
        wait_for(lambda: c.get("/api/live/diag").json()["counters"]["rejected"] == 2)
        assert state(c)["status"] == "grey"


def test_simulator_supply_switch_drives_same_alarm(tmp_path, db_url):
    demo, provider, app = make(tmp_path, db_url, source="simulator")
    with TestClient(app) as c:
        wait_for(lambda: state(c)["status"] == "green", timeout=8)
        sim = c.get("/api/demo/admin/sim", headers=auth()).json()
        assert sim["signal"] == "di" and sim["supplyOn"] is True
        assert c.post("/api/demo/admin/sim/supply", json={"on": False}).status_code == 401
        c.post("/api/demo/admin/sim/supply", json={"on": False}, headers=auth())
        wait_for(lambda: state(c)["status"] == "red", timeout=10)
        wait_for(lambda: texts(provider, "GridBox ALERT"))
        p = wait_for(lambda: state(c)["smsPreview"])
        assert p["kind"] == "outage" and p["texts"]["hy"].startswith("GridBox ԱՀԱԶԱՆԳ")
        c.post("/api/demo/admin/sim/supply", json={"on": True}, headers=auth())
        wait_for(lambda: state(c)["status"] == "green", timeout=10)
        wait_for(lambda: texts(provider, "GridBox OK"))
        thr = c.post("/api/demo/admin/thresholds", json={"diOffHoldS": 5}, headers=auth()).json()
        assert thr["diOffHoldS"] == 5


def test_power_mode_untouched(tmp_path, db_url):
    s = Settings(enabled=True, source="simulator", sqlite_path=str(tmp_path / "p.db"), database_url=db_url)
    demo = LiveDemo(s, provider=ConsoleProvider(s), start_mqtt=False)
    app = FastAPI(lifespan=demo.lifespan)
    app.include_router(demo.router)
    with TestClient(app) as c:
        wait_for(lambda: state(c)["status"] == "green", timeout=8)
        st = state(c)
        assert st["signal"] == "power" and st["lastReading"]["powerW"] > 0

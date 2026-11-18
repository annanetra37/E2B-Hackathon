"""
Fake IWR405: publishes meter readings over MQTT like the real gateway would, so the whole
live pipeline can be tested without hardware (and to rehearse T3/T4/T5 from the test plan).

  python tools/fake_gateway.py --url mqtt://127.0.0.1:1883 --topic e2b/demo/yvn-01
  python tools/fake_gateway.py --url mqtts://<cluster>:8883 --user gw --password ... --script dryer

Scripts:
  lamp      LED light only (~9 W) forever
  led       LED on 5 s / off 5 s (must stay green)
  dryer     20 s lamp, 12 s hair dryer high, 20 s lamp, repeat
  flicker   hair dryer on/off every second for 20 s, then lamp (T4)
  backfill  10 readings stamped 30 s in the past (cache back-fill, must be flagged late; T5)
  di        on/off mode, like tools/di_bridge.py: ON 10 s, a 1 s bounce to OFF (must be ignored),
            ON 5 s, OFF 15 s (outage), ON 10 s (restored), 20 s silence (grey, no SMS), ON again; repeats.
            Publishes to <topic>/di style topics (default e2b/demo/yvn-01/di) with QoS 1, retained,
            plus online/offline on --status-topic (last will). Use with LIVE_SIGNAL=di.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import paho.mqtt.client as mqtt


def payload(power: float, ts: float) -> str:
    v = random.uniform(228, 232) - (1.5 if power > 500 else 0)
    pf = 0.99 if power > 500 else 0.92
    return json.dumps({"ts": int(ts), "device": "demo_meter",
                       "values": {"voltage": round(v, 1), "current": round(power / (v * pf), 3),
                                  "power": round(power, 1), "pf": pf, "freq": 50.0, "energy": 12.4}})


def lamp() -> float:
    """LED light on the stand (~9 W)."""
    return random.uniform(8.5, 9.5)


def schedule(name: str):
    if name == "lamp":
        while True:
            yield lamp(), 0
    elif name == "dryer":
        while True:
            for _ in range(20):
                yield lamp(), 0
            for _ in range(12):
                yield lamp() + random.uniform(2050, 2150), 0
            for _ in range(20):
                yield lamp(), 0
    elif name == "led":
        while True:
            for _ in range(5):
                yield lamp(), 0
            for _ in range(5):
                yield 0.0, 0
    elif name == "flicker":
        for i in range(20):
            yield (lamp() + 2100 if i % 2 == 0 else lamp()), 0
        while True:
            yield lamp(), 0
    elif name == "backfill":
        for _ in range(10):
            yield lamp() + 2100, -30
        while True:
            yield lamp(), 0


def di_schedule():
    """(di1 or None for silence, seconds) steps of the DI test script."""
    while True:
        yield from [(1, 10), (0, 1), (1, 5), (0, 15), (1, 10), (None, 20), (1, 10)]


def run_di(c, a) -> None:
    seq = 0
    c.publish(a.status_topic, json.dumps({"station": a.station, "bridge": "online"}), qos=1, retain=True)
    steps = itertools.islice(di_schedule(), a.count or None)
    for value, seconds in steps:
        label = "silence (nothing published)" if value is None else f"di1={value} ({'ON' if value else 'OFF'})"
        print(f"{time.strftime('%H:%M:%S')}  {label} for {seconds} s", flush=True)
        end = time.time() + seconds
        while time.time() < end:
            if value is not None:
                now = datetime.now(timezone.utc)
                c.publish(a.topic, json.dumps({
                    "station": a.station, "di1": value, "level": "LOW" if value else "HIGH",
                    "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "seq": seq, "src": "di-bridge"}), qos=1, retain=True)
                seq += 1
            time.sleep(min(a.interval, max(0.05, end - time.time())))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="mqtt://127.0.0.1:1883")
    ap.add_argument("--topic", default="e2b/demo/yvn-01")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--script", default="dryer", choices=["lamp", "led", "dryer", "flicker", "backfill", "di"])
    ap.add_argument("--status-topic", default="e2b/demo/yvn-01/status", help="di script: bridge online/offline")
    ap.add_argument("--station", default="YVN-DEMO-01", help="di script: station id in each message")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--count", type=int, default=0, help="stop after N messages (0 = forever)")
    a = ap.parse_args()
    if a.script == "di" and a.topic == "e2b/demo/yvn-01":
        a.topic = "e2b/demo/yvn-01/di"

    u = urlparse(a.url)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"fake-iwr405-{random.randint(0, 9999)}")
    if a.user:
        c.username_pw_set(a.user, a.password)
    if u.scheme in ("mqtts", "ssl"):
        c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    if a.script == "di":
        c.will_set(a.status_topic, json.dumps({"station": a.station, "bridge": "offline"}), qos=1, retain=True)
    c.connect(u.hostname, u.port or (8883 if u.scheme in ("mqtts", "ssl") else 1883), keepalive=30)
    c.loop_start()
    if a.script == "di":
        try:
            run_di(c, a)
        finally:
            c.loop_stop()
            c.disconnect()
        return
    n = 0
    for power, offset in itertools.islice(schedule(a.script), a.count or None):
        c.publish(a.topic, payload(power, time.time() + offset), qos=0)
        n += 1
        print(f"{n:5d}  {power:7.1f} W{'  (late)' if offset else ''}", flush=True)
        time.sleep(a.interval)
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()

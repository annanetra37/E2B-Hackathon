#!/usr/bin/env python3
"""
DI bridge (task T1): reads the IWR405 router's digital input (DI) over its local web API and
publishes it to the EMQX broker, where the Railway app picks it up (LIVE_SIGNAL=di).

    router (LAN, https://10.18.249.233)  --POST /v1/rpc do_di_status_get, every 1 s-->  this script
    this script  --MQTT/TLS 8883, QoS 1, retained-->  e2b/demo/yvn-01/di   {"di1": 1, ...}
                                                      e2b/demo/yvn-01/status {"bridge": "online"/"offline"}

Translation (the app never sees electrical levels):
    LOW  = switch closed = electricity on  -> di1 = 1      (DI_LOW_MEANS_ON=true, the bench result)
    HIGH = switch open   = electricity off -> di1 = 0
The IWR405 answers {"result": {"di_status": 1}} and its own page then shows "low level", so a
number 1 is read as LOW and 0 as HIGH (ROUTER_DI_ONE_IS=LOW). With the stand's wiring (switch
closes DI+ to GND) that gives di1 = 1 when the light is on. If the router cannot be read, NOTHING is published for di1 (never a
guessed 0): the app then turns grey ("no data"), which is correct.

Usage
    python tools/di_bridge.py --probe      log in, call the DI method once, print the raw reply + what was understood
    python tools/di_bridge.py --dry-run    poll and print the MQTT messages, publish nothing
    python tools/di_bridge.py              run (Ctrl+C to stop)
    python tools/di_bridge.py --env /etc/e2b/di-bridge.env

Settings: environment variables, or a .env file next to this script (or --env PATH). See
tools/di-bridge.env.example. The defaults match the IWR405 web page's own calls, captured in
docs/payload-samples/router-rpc.md; the templates stay adjustable for other firmware.
Placeholders: {user} {password} {token} {method} {id}.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import paho.mqtt.client as mqtt

log = logging.getLogger("di-bridge")

DEFAULTS = {
    "ROUTER_URL": "https://10.18.249.233",
    "ROUTER_USER": "admin",
    "ROUTER_PASSWORD": "",
    "ROUTER_RPC_PATH": "/v1/rpc",
    "ROUTER_DI_METHOD": "do_di_status_get",
    "ROUTER_DI_BODY": '{"jsonrpc": "2.0", "id": {id}, "method": "{method}"}',
    "ROUTER_LOGIN_PATH": "/v1/rpc",
    "ROUTER_LOGIN_BODY": '{"jsonrpc": "2.0", "id": {id}, "method": "login", '
                         '"params": {"username": "{user}", "password": "{password}"}}',
    # IWR405 web page (captured 2026-11-19): the login reply's "sid" goes in the Authorization header
    # (no "Bearer") and in the Admin-Token cookie.
    "ROUTER_HEADERS": '{"Authorization": "{token}", "Cookie": "Admin-Token={token}"}',
    "ROUTER_DI_KEY": "di_status",            # name of the DI field in the reply; empty = find it automatically
    "ROUTER_DI_ONE_IS": "LOW",               # IWR405: di_status 1 = page shows "low level" = switch closed
    "ROUTER_TIMEOUT": "0.8",
    "ROUTER_INSECURE": "",                   # true = skip certificate check even for a public address (not advised)
    "MQTT_URL": "",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "MQTT_CLIENT_ID": "e2b-di-bridge-01",
    "MQTT_TOPIC": "e2b/demo/yvn-01/di",
    "STATUS_TOPIC": "e2b/demo/yvn-01/status",
    "STATION_ID": "YVN-DEMO-01",
    "POLL_SECONDS": "1",
    "HEARTBEAT_SECONDS": "2",
    "DI_LOW_MEANS_ON": "true",
}
TOKEN_KEYS = ("token", "access_token", "session", "sessionid", "session_id", "sid", "ubus_rpc_session", "stok")
LEVEL_KEYS = ("di_status", "di1", "di", "di_1", "din1", "level", "status", "state", "value", "val")


# ====================================================================== settings

def load_env(path: Path | None) -> dict[str, str]:
    """Environment variables win over the .env file, the .env file over the defaults."""
    cfg = dict(DEFAULTS)
    candidates = [path] if path else [Path(__file__).with_name(".env"), Path.cwd() / "di-bridge.env"]
    for p in candidates:
        if p and p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                cfg[k.strip()] = v
            log.info("settings from %s", p)
            break
    for k in DEFAULTS:
        if os.environ.get(k) is not None:
            cfg[k] = os.environ[k]
    return cfg


def truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def fill(template: str, **values) -> str:
    """Replace {name} placeholders without touching JSON braces; values are JSON-escaped."""
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", json.dumps(str(v))[1:-1] if k not in ("id",) else str(v))
    return out


def is_private_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


# ====================================================================== router

def find_key(doc, keys) -> object:
    """Depth-first search for the first matching key (case-insensitive) in a JSON reply."""
    keys = tuple(k.lower() for k in keys)
    stack = [doc]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in keys and not isinstance(v, (dict, list)):
                    return v
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def level_of(raw, one_is: str = "LOW") -> str | None:
    """Router value -> "LOW" / "HIGH". Numbers follow ROUTER_DI_ONE_IS: the IWR405 reports
    di_status 1 while its page says "low level", so by default 1 = LOW and 0 = HIGH."""
    one = "HIGH" if str(one_is).strip().upper() == "HIGH" else "LOW"
    zero = "LOW" if one == "HIGH" else "HIGH"
    if isinstance(raw, bool):
        return one if raw else zero
    if isinstance(raw, (int, float)):
        return one if raw == 1 else zero if raw == 0 else None
    if isinstance(raw, str):
        s = raw.strip().upper()
        if s in ("1", "0"):
            return one if s == "1" else zero
        if s in ("LOW", "L", "CLOSE", "CLOSED"):
            return "LOW"
        if s in ("HIGH", "H", "OPEN"):
            return "HIGH"
    return None


class RouterError(Exception):
    pass


class Router:
    def __init__(self, cfg: dict, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.base = cfg["ROUTER_URL"].rstrip("/")
        verify = True
        if self.base.startswith("https://"):
            if is_private_host(self.base):
                verify = False             # self-signed router certificate, LAN only
            elif truthy(cfg["ROUTER_INSECURE"]):
                verify = False
                log.warning("certificate check OFF for a non-LAN router address (ROUTER_INSECURE=true)")
        self.verify = verify
        self.http = httpx.Client(verify=verify, timeout=float(cfg["ROUTER_TIMEOUT"]), follow_redirects=True,
                                 transport=transport)
        self.token: str | None = None
        self.logged_in = False
        self.req_id = 0
        self.last_reply = None

    def _headers(self) -> dict:
        """ROUTER_HEADERS with {token} filled in; a header that needs a token is left out until we have one."""
        try:
            raw = json.loads(self.cfg["ROUTER_HEADERS"] or "{}")
        except ValueError:
            log.warning("ROUTER_HEADERS is not valid JSON, ignored")
            raw = {}
        out = {"Content-Type": "application/json"}
        for k, v in raw.items():
            v = str(v)
            if "{token}" in v:
                if not self.token:
                    continue
                v = v.replace("{token}", self.token)
            out[k] = v
        return out

    def _post(self, path: str, body_tpl: str, **values):
        self.req_id += 1
        body = fill(body_tpl, id=self.req_id, token=self.token or "", user=self.cfg["ROUTER_USER"],
                    password=self.cfg["ROUTER_PASSWORD"], method=self.cfg["ROUTER_DI_METHOD"], **values)
        try:
            r = self.http.post(self.base + path, content=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise RouterError(f"{type(e).__name__}: {e}") from None
        if r.status_code in (401, 403):
            raise RouterError(f"HTTP {r.status_code} (not logged in)")
        if r.status_code >= 300:
            raise RouterError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            raise RouterError(f"reply is not JSON: {r.text[:200]}") from None

    def login(self) -> None:
        if not self.cfg["ROUTER_LOGIN_BODY"].strip():
            return                         # no login needed (or cookie-only)
        reply = self._post(self.cfg["ROUTER_LOGIN_PATH"], self.cfg["ROUTER_LOGIN_BODY"])
        if isinstance(reply, dict) and reply.get("error"):
            raise RouterError(f"login refused: {reply.get('error')}")
        tok = find_key(reply, TOKEN_KEYS)
        self.token = str(tok) if tok else None     # cookies (if any) are kept by the client automatically
        self.logged_in = True
        log.info("logged in to the router%s", "" if self.token else " (no token in reply, using cookies)")

    def read_level(self) -> tuple[str, object]:
        """One DI read -> ("LOW"|"HIGH", raw value). Logs in again once on an auth error."""
        for attempt in (1, 2):
            try:
                if not self.logged_in and self.cfg["ROUTER_LOGIN_BODY"].strip():
                    self.login()
                reply = self._post(self.cfg["ROUTER_RPC_PATH"], self.cfg["ROUTER_DI_BODY"])
                self.last_reply = reply
                if isinstance(reply, dict) and reply.get("error"):
                    raise RouterError(f"error reply: {reply.get('error')}")
                key = self.cfg["ROUTER_DI_KEY"].strip()
                raw = find_key(reply, (key,)) if key else find_key(reply, LEVEL_KEYS)
                level = level_of(raw, self.cfg.get("ROUTER_DI_ONE_IS", "LOW"))
                if level is None:
                    raise RouterError(f"no LOW/HIGH or 0/1 found in reply: {json.dumps(reply)[:200]}")
                return level, raw
            except RouterError as e:
                if attempt == 1 and ("not logged in" in str(e) or "error reply" in str(e)):
                    self.token, self.logged_in = None, False   # session expired: log in again, retry once
                    continue
                raise
        raise RouterError("unreachable")


# ====================================================================== MQTT

class Publisher:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg, self.dry_run = cfg, dry_run
        self.connected = threading.Event()
        self.seq = 0
        self.client: mqtt.Client | None = None
        if dry_run:
            return
        u = urlparse(cfg["MQTT_URL"])
        if not u.hostname:
            raise SystemExit("MQTT_URL is not set (mqtts://<emqx-host>:8883)")
        tls = u.scheme in ("mqtts", "ssl", "tls", "wss")
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["MQTT_CLIENT_ID"],
                        transport="websockets" if u.scheme in ("ws", "wss") else "tcp")
        if cfg["MQTT_USERNAME"]:
            c.username_pw_set(cfg["MQTT_USERNAME"], cfg["MQTT_PASSWORD"])
        if tls:
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED)      # EMQX: always verified, never insecure
        c.will_set(cfg["STATUS_TOPIC"], self._status("offline"), qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        port = u.port or (8883 if tls else 1883)
        c.connect_async(u.hostname, port, keepalive=30)
        c.loop_start()
        self.client = c

    def _status(self, state: str) -> str:
        return json.dumps({"station": self.cfg["STATION_ID"], "bridge": state})

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        client.publish(self.cfg["STATUS_TOPIC"], self._status("online"), qos=1, retain=True)
        self.connected.set()
        log.info("MQTT connected to %s as %s", urlparse(self.cfg["MQTT_URL"]).hostname, self.cfg["MQTT_CLIENT_ID"])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if self.connected.is_set():
            log.warning("MQTT disconnected (%s), reconnecting", reason_code)
        self.connected.clear()

    def publish_di(self, di1: int, level: str) -> None:
        msg = {"station": self.cfg["STATION_ID"], "di1": di1, "level": level,
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "seq": self.seq, "src": "di-bridge"}
        self.seq += 1
        text = json.dumps(msg)
        if self.dry_run:
            print(f"{self.cfg['MQTT_TOPIC']}  {text}", flush=True)
            return
        self.client.publish(self.cfg["MQTT_TOPIC"], text, qos=1, retain=True)

    def close(self) -> None:
        if self.client:
            try:
                self.client.publish(self.cfg["STATUS_TOPIC"], self._status("offline"), qos=1, retain=True).wait_for_publish(2)
            except Exception:
                pass
            self.client.disconnect()
            self.client.loop_stop()


# ====================================================================== main loop

def run(cfg: dict, dry_run: bool) -> None:
    low_on = truthy(cfg["DI_LOW_MEANS_ON"])
    poll = max(0.2, float(cfg["POLL_SECONDS"]))
    heartbeat = max(poll, float(cfg["HEARTBEAT_SECONDS"]))
    router = Router(cfg)
    pub = Publisher(cfg, dry_run)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last_di: int | None = None
    last_level: str | None = None
    last_sent = 0.0
    failing = False
    log.info("polling %s every %g s, heartbeat %g s, LOW means %s", router.base, poll, heartbeat,
             "ON" if low_on else "OFF")
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                level, raw = router.read_level()
                if failing:
                    log.info("router readable again")
                failing = False
                di1 = 1 if (level == "LOW") == low_on else 0
                now = time.monotonic()
                if di1 != last_di:
                    if last_di is not None:
                        log.info("%s -> %s (di1 %s -> %s)", last_level, level, last_di, di1)
                    else:
                        log.info("reading: %s (di1 = %s)", level, di1)
                    pub.publish_di(di1, level)
                    last_di, last_level, last_sent = di1, level, now
                elif now - last_sent >= heartbeat:
                    pub.publish_di(di1, level)
                    last_sent = now
            except RouterError as e:
                if not failing:
                    log.warning("cannot read the router: %s (publishing nothing until it answers)", e)
                failing = True
                last_di = None                   # after a failure, the next good read is published at once
            stop.wait(max(0.0, poll - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        pub.close()
        log.info("stopped")


def probe(cfg: dict) -> int:
    router = Router(cfg)
    tls = "plain http" if router.base.startswith("http://") else (
        "certificate check on" if router.verify else "certificate check off: LAN address")
    print(f"router: {router.base}  ({tls})")
    try:
        if cfg["ROUTER_LOGIN_BODY"].strip():
            router.login()
            print(f"login ok, token: {'yes' if router.token else 'no (cookies)'}")
        level, raw = router.read_level()
    except RouterError as e:
        print(f"FAILED: {e}")
        if router.last_reply is not None:
            print("last reply:", json.dumps(router.last_reply, indent=2)[:2000])
        return 1
    print("raw reply:", json.dumps(router.last_reply, indent=2)[:2000])
    di1 = 1 if (level == "LOW") == truthy(cfg["DI_LOW_MEANS_ON"]) else 0
    print(f"understood: raw={raw!r} -> level {level} -> di1 = {di1} ({'electricity ON' if di1 else 'OFF'})")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="IWR405 DI -> MQTT bridge")
    ap.add_argument("--env", type=Path, help="settings file (default: .env next to this script)")
    ap.add_argument("--dry-run", action="store_true", help="print the MQTT messages instead of publishing")
    ap.add_argument("--probe", action="store_true", help="read the DI once, print the raw router reply, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_env(a.env)
    if a.probe:
        sys.exit(probe(cfg))
    run(cfg, a.dry_run)


if __name__ == "__main__":
    main()

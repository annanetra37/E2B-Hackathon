# Grid Substation Portal (MVP)

A portal that shows **every substation on a map of Armenia** (green = working, amber = warning,
red = problem, grey = no data). Click a substation to see its **live single-line diagram**, with the
status and the measured values next to every element.

Two kinds of substation are supported:

| Type | Diagram | Data sources |
|---|---|---|
| **Primary** 110/10 kV | 110 kV line → Q0 breaker → transformer → Q1 breaker → 10 kV busbar → feeders | Modbus TCP meters + IEC 104 RTU (breakers, frequency) |
| **Transformer station** 10/35/110 kV → 0.4 kV (the hand-drawn sketch) | MV line → QS disconnector → FU fuses → transformer (temperature) → 0.4 kV busbar with L1/L2/L3 → outgoing lines (with or without current meters) → data box | Modbus TCP only |

It runs **without a real grid**: a simulator plays 14 substations that develop faults and recover.

```
 simulator.py (fake grid)        gateway.py (collect + judge)       web.py + static/ (portal)
 14 Modbus TCP servers  ◄──────  polls all substations in parallel   map of Armenia
  5 IEC 104 RTUs        ◄──────  health.py: ok / warning / fault ──► single-line diagrams
                                 SQLite: snapshots, events, history   trends, events, demo buttons
```

## Run it on your computer (Python 3.10+)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Three terminals, each with the venv active:

| Terminal | Command |
|---|---|
| 1 | `python simulator.py` |
| 2 | `python gateway.py` |
| 3 | `uvicorn web:app --port 8000` → open http://localhost:8000 |

On a substation page, **Demo controls** let you trigger a problem (a fuse blows, a breaker trips, …)
and watch the diagram, map and event list react within seconds.

## Files

| File | Role |
|---|---|
| `substations.yaml` | the only file to edit: substations, positions, register maps, limits |
| `simulator.py` | fake grid: realistic values and faults for both substation types |
| `gateway.py` | reads every device (Modbus TCP + IEC 104), stores snapshots / events / history. **Read-only.** |
| `health.py` | the rules: what is working, warning or not working, and why (plain sentences) |
| `web.py` | portal server + API (`/api/substations`, `/api/substations/{id}`, `/events`, `/history`) |
| `static/` | map page, substation page, styles; Leaflet is bundled in `static/vendor` |
| `Dockerfile`, `start.sh`, `railway.json` | Railway deployment (all three programs in one container) |
| `deploy/gateway.service` | run only the gateway 24/7 on an on-site Linux box |
| `live/` | Live tab: MQTT ingest, parser, alarm engine, storage, SMS, simulator, API |
| `static/live.*`, `join.html`, `admin.html` | Live tab, visitor sign-up, presenter admin page |
| `tools/fake_gateway.py` | publishes IWR405-like messages over MQTT for testing without hardware |
| `.env.example` | every Live-tab variable, ready for Railway's Raw Editor |

## Deploy on Railway

1. Push this folder to a GitHub repo.
2. railway.com → **New Project → Deploy from GitHub repo**. It builds from the `Dockerfile`.
3. **Variables**: `DASHBOARD_PASSWORD` (the browser asks for it; any user name works).
   Optional: `RETENTION_HOURS` (default 24), `SIM_FAULT_EVERY_S` (default 500, lower = more faults).
4. Optional: **Volumes** → mount a volume at `/data` to keep history across redeploys.
5. **Settings → Networking → Generate Domain**.

Health check: `/healthz`. If any program crashes, the container exits and Railway restarts it.

## Adding a real substation

1. Copy a block in `substations.yaml`, set its real IP addresses, `lat`/`lon`, ratings, lines.
2. If its devices use different register maps, add a template or adjust the existing one.
3. Run the gateway **on site** (it must reach the devices on the substation network) and set
   `RUN_SIMULATOR=false`. The portal can stay in the cloud.

## Live tab: the real mini-substation on the stand

`/live` shows **one marker in Yerevan fed by a real meter**: single-phase Modbus meter → Four-Faith
IWR405 gateway → MQTT/TLS broker → this app. Normally only an LED light is on and the page shows
the exact numbers (≈9 W with the light on, 0 W off: both are green, "normal use"). Plug in a hair
dryer and power jumps to ~900–2,100 W: the dot turns red with a pulsing ring ("unusual · power
spike") and everyone who signed up through the QR code gets an SMS with the exact power, e.g.
*"E2B ALERT: Sudden power spike at DigiTech Substation: 2,134 W (usual 9 W, limit 1,000 W). 14:02:11"*.

The hair dryer is only the stand's example: **any device that makes consumption jump above the
usual level** triggers the alert (a 60 W bulb, a fan, a laptop charger, a kettle…).
Rule: red when power stays for `ALARM_HOLD_SECONDS` above
`min(POWER_ALARM_W, max(POWER_MIN_ALARM_W, BASELINE_MULTIPLIER × usual level))`, with the usual level
being the median of normal readings over `BASELINE_WINDOW_SECONDS`. Defaults: floor 50 W, 3× usual,
absolute limit 1,000 W. The LED switching on or off (0–9 W) never alarms. Red stays as long as the
extra device is on and clears 5 s after power drops back below 80 % of the trigger level.
Tune the floor on the admin page (in memory) or with `POWER_MIN_ALARM_W`.

**SMS content** (`SMS_STYLE=tailored`, default) is written from the measured numbers, max 2 SMS:

- *Alert:* "E2B ALERT DigiTech Substation 14:02:11: power jumped to 2,134 W (usual 9 W).
  9.3 A = 58% of the 16 A socket. Fix: if it is not needed, switch it off or unplug it; run heavy
  devices one at a time. Looks like a heating appliance (hair dryer, kettle or iron). It uses about
  2.1 kWh per hour." The device guess, the current vs `CIRCUIT_RATED_A`, voltage sag and power factor
  change the wording and the fix (e.g. over 16 A: "the breaker may trip. Fix: unplug it NOW").
- *Back to normal* (`SMS_ON_RECOVERY=true`, only to people who got the alert): "E2B OK … 14:03:15: back
  to normal, 9 W now. The spike lasted 1 min 4 s, peak 2,134 W (9.3 A), about 37 Wh extra. No action needed."
- *Data lost during an overload:* a warning to check the device and the gateway.

`SMS_STYLE=template` uses `SMS_TEMPLATE` / `SMS_RECOVERY_TEMPLATE` instead.

Testers get the SMS by signing up through the QR code / `/demo/join`, or permanently via
`SMS_ALWAYS_NOTIFY` (those team numbers are exempt from the per-number daily cap, so repeated test
runs keep arriving; the 60 s cooldown and the hourly cap still apply). Spec: *E2b Live Demo – Developer Tasks* (v1.0). The code lives in `live/`.

```
 IWR405 ──MQTT/TLS──► broker ──► live/mqtt_client.py ─► live/parser.py ─► live/store.py (Postgres / SQLite)
                                  (or live/simulator.py, Plan B)          live/alarm.py (green/amber/red/grey)
                                                                             ├─► SSE /api/live/stream ─► /live
                                                                             └─► live/notifier.py ─► SMS
```

Everything runs **inside the web process** (Railway: replicas = 1, app sleeping OFF).
It is off unless `DEMO_ENABLED=true`; all variables are listed in `.env.example` (§12.4 of the doc).

| Page | What |
|---|---|
| `/live` | map marker, live tiles (W, A, V, PF, Hz), 2-minute chart with threshold lines, event feed, click the dot for a mini scheme |
| `/live?kiosk=1` | stand screen: full screen on first click, large fonts, no nav, QR code to the sign-up page |
| `/demo/join` | mobile sign-up (EN + HY), consent required, number deleted after 24 h |
| `/demo/sim` | **Simulator tab** for your phone (admin token): a virtual socket strip with an LED lamp, charger, fan, 60 W bulb, laptop, TV, iron, hair dryer (low/high), heater and kettle. Tap to plug in/out; the simulated meter adds them up and drives the same map, alarm and SMS |
| `/demo/admin` | presenter's phone: source live/simulator, hair dryer ON/OFF (low/high), thresholds, reset, test SMS, delete all numbers, diagnostics. Needs `ADMIN_TOKEN` |
| `/api/live/diag` | is MQTT connected, last message age, SMS provider, subscriber count |

`DASHBOARD_PASSWORD` still protects the grid portal; `/demo/join`, `/u/<token>` and the admin page
(token-protected) are always reachable, and `/live` too unless `LIVE_PUBLIC=false`.

### Try it without hardware

```bash
pip install -r requirements-dev.txt
DEMO_ENABLED=true DEMO_SOURCE=simulator ADMIN_TOKEN=dev uvicorn web:app --port 8000
# open http://localhost:8000/live and, on your phone or another tab, /demo/admin (token: dev)
```

With a local broker and a fake gateway (exercises the real MQTT path):

```bash
amqtt -c tools/broker.yaml &                                   # MQTT on 127.0.0.1:1883
DEMO_ENABLED=true MQTT_URL=mqtt://127.0.0.1:1883 MQTT_TOPIC='e2b/demo/#' ADMIN_TOKEN=dev \
  uvicorn web:app --port 8000 &
python tools/fake_gateway.py --script dryer      # also: lamp, led (on/off, stays green), flicker (T4), backfill (T5)
```

Tests: `pytest` (alarm engine incl. hold/clear/flicker/baseline/late/grey, parser, SMS caps and
providers, API flow with the simulator). Real payload captures go in `docs/payload-samples/`
and are parsed by the tests automatically.

### On Railway (DEP-1)

1. Web service → **Settings**: Replicas = 1, App sleeping OFF, healthcheck `/healthz` (already in `railway.json`).
2. **+ New → Database → PostgreSQL**, then `DATABASE_URL=${{Postgres.DATABASE_URL}}` on the web service.
   Tables are created at start-up (no separate migration step). Without Postgres a SQLite file is used.
3. Paste `.env.example` into **Variables → Raw Editor**, fill in the `<...>` values, start with `SMS_PROVIDER=console`.
4. Check `/api/live/diag` (`mqttConnected: true`, `lastMessageAgeS < 3`), open `/live`, switch the hair dryer on,
   then set `SMS_PROVIDER=twilio` or `android_gateway`.

SMS providers: `console` (log only), `twilio` (Messaging Service SID or `TWILIO_FROM`),
`android_gateway` (an Android phone running *SMS Gateway for Android*: `SMSGATE_URL` =
`http://<phone-ip>:8080` locally or `https://api.sms-gate.app/3rdparty/v1` in cloud mode).
Caps: cooldown per number, daily cap per number, global hourly cap; every attempt goes to `sms_log`
with a hash of the number, never the number itself.

**Safety:** the gateway only reads. The demo buttons only talk to the simulator, never to real equipment,
and they are switched off when `RUN_SIMULATOR=false`.

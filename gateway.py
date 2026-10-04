"""
THE GATEWAY  (ELI5: the translator robot, now for a whole grid)
================================================================
Every few seconds, for EVERY substation in substations.yaml, at the same time:
  1. reads the meters and transformer monitor   (Modbus TCP)
  2. reads breaker positions and frequency        (IEC 104)
  3. asks the doctor (health.py): what is OK, what is wrong?
  4. writes into the notebook (SQLite):
       - a live snapshot per substation    -> what the map and diagram show
       - an event when something changes   -> "14:02 Gyumri F2 breaker tripped"
       - values every N seconds            -> the trend charts

SAFETY: this program only READS. It never sends commands to any device.

Run:  python gateway.py
"""
import asyncio
import json
import logging
import os
import sqlite3
import struct
import time
from datetime import datetime, timezone

import c104
from pymodbus.client import AsyncModbusTcpClient

import config
import health

logging.basicConfig(level=logging.INFO, format="%(asctime)s GW  %(levelname)s %(message)s")
log = logging.getLogger("gateway")
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (substation TEXT PRIMARY KEY, ts TEXT, status TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, substation TEXT, element TEXT,
                                   label TEXT, status TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_events ON events(substation, id);
CREATE TABLE IF NOT EXISTS history (ts TEXT, substation TEXT, element TEXT, metric TEXT, value REAL, unit TEXT);
CREATE INDEX IF NOT EXISTS idx_history ON history(substation, element, metric, ts);
CREATE TABLE IF NOT EXISTS sim_commands (id INTEGER PRIMARY KEY, substation TEXT, action TEXT,
                                         ts TEXT, done INTEGER DEFAULT 0);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ======================= translating raw Modbus numbers =======================
REG_COUNT = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2}
FMT = {"uint16": "H", "int16": "h", "uint32": "I", "int32": "i", "float32": "f"}


def decode(regs: list[int], p: dict) -> float:
    n = REG_COUNT[p["type"]]
    chunk = regs[p["address"]:p["address"] + n]
    if p.get("word_order", "big") == "little" and n == 2:
        chunk = [chunk[1], chunk[0]]
    value = struct.unpack(">" + FMT[p["type"]], struct.pack(">" + "H" * n, *chunk))[0]
    return value * p.get("scale", 1)


# ======================= one substation =======================
class SubstationReader:
    def __init__(self, cfg: dict, sub: dict, iec_client: c104.Client):
        self.cfg, self.sub = cfg, sub
        self.devices = config.modbus_devices(cfg, sub)
        self.modbus = AsyncModbusTcpClient(sub["modbus"]["host"], port=sub["modbus"].get("port", 502),
                                           timeout=1.5, retries=0, reconnect_delay=0)
        self.conn, self.iec_points = None, {}
        points = config.iec104_points(cfg, sub)
        if points:                               # only primary substations have an IEC 104 RTU
            i104 = sub["iec104"]
            self.conn = iec_client.add_connection(ip=i104["host"], port=i104.get("port", 2404),
                                                  init=c104.Init.ALL)
            station = self.conn.add_station(common_address=i104["common_address"])
            types = {"float": c104.Type.M_ME_NC_1, "single": c104.Type.M_SP_NA_1}
            self.iec_points = {p["key"]: station.add_point(io_address=p["ioa"], type=types[p["type"]])
                               for p in points}
        self.prev_status: dict[str, str] = {}

    async def read_modbus(self, data: dict, online: dict):
        for dev in self.devices:
            online[dev["key"]] = False
        if not self.modbus.connected:
            await self.modbus.connect()
        for dev in self.devices:
            if not self.modbus.connected:
                await self.modbus.connect()      # a silent device can drop the link: reconnect
                if not self.modbus.connected:
                    break                        # whole substation unreachable
            count = max(p["address"] + REG_COUNT[p["type"]] for p in dev["points"])
            try:
                rr = await self.modbus.read_holding_registers(0, count, slave=dev["unit_id"])
            except Exception:
                continue                         # this one device did not answer in time
            if rr.isError():
                continue                         # this one device does not answer
            data[dev["key"]] = {p["name"]: decode(rr.registers, p) for p in dev["points"]}
            online[dev["key"]] = True

    async def read_iec104(self, data: dict, online: dict):
        if self.conn is None:
            return
        online["rtu"] = False
        if not self.conn.is_connected:
            return
        if self.conn.is_muted:                   # link up but not started (also after reconnect)
            await asyncio.to_thread(self.conn.unmute)
            await asyncio.to_thread(self.conn.interrogation, common_address=self.sub["iec104"]["common_address"])
        data["rtu"] = {k: pt.value for k, pt in self.iec_points.items()}
        online["rtu"] = True

    async def poll(self) -> dict:
        data, online = {}, {}
        try:
            await asyncio.wait_for(self.read_modbus(data, online), timeout=10)
        except (asyncio.TimeoutError, OSError):
            pass
        await self.read_iec104(data, online)
        return health.evaluate(self.cfg, self.sub, data, online)


# ======================= the notebook =======================
class Storage:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def save(self, snap: dict, ts: str, reader: SubstationReader, write_history: bool):
        sid = snap["id"]
        snap["ts"] = ts
        self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                        (sid, ts, snap["status"], json.dumps(snap)))
        for el in snap["elements"]:
            now_st = "ok" if el["status"] == "unmonitored" else el["status"]
            before = reader.prev_status.get(el["id"])
            changed = before != now_st if before is not None else now_st != "ok"
            if changed:
                msg = el["message"] if now_st != "ok" else "Back to normal"
                self.db.execute("INSERT INTO events (ts, substation, element, label, status, message) "
                                "VALUES (?,?,?,?,?,?)", (ts, sid, el["id"], el["label"], el["status"], msg))
                log.info("%-14s %-4s %-8s %s", sid, el["id"], el["status"].upper(), msg)
            reader.prev_status[el["id"]] = now_st
            if write_history:
                self.db.executemany(
                    "INSERT INTO history VALUES (?,?,?,?,?,?)",
                    [(ts, sid, el["id"], v["label"], v["value"], v["unit"])
                     for v in el["values"]
                     if isinstance(v["value"], (int, float)) and v["label"] not in ("Rating",)])

    def cleanup(self, hours: float):
        self.db.execute("DELETE FROM history WHERE ts < datetime('now', ?)", (f"-{hours} hours",))
        self.db.execute("DELETE FROM events WHERE ts < datetime('now', '-7 days')")
        self.db.execute("DELETE FROM sim_commands WHERE done=1")
        self.db.commit()


# ======================= main loop =======================
async def main():
    cfg = config.load()
    storage = Storage(config.db_path(cfg))
    iec_client = c104.Client()
    readers = [SubstationReader(cfg, s, iec_client) for s in cfg["substations"]]
    iec_client.start()                            # IEC 104 runs in the background, reconnects by itself

    interval = cfg.get("poll_interval_s", 2)
    hist_every = cfg.get("history_interval_s", 10)
    retention = float(os.environ.get("RETENTION_HOURS", cfg.get("retention_hours", 24)))
    last_hist = last_clean = 0.0
    log.info("Gateway started: %d substations, polling every %ss", len(readers), interval)
    await asyncio.sleep(1.5)

    try:
        while True:
            started = time.monotonic()
            snaps = await asyncio.gather(*(r.poll() for r in readers))   # all substations in parallel
            ts, write_hist = now_utc(), started - last_hist >= hist_every
            for r, snap in zip(readers, snaps):
                storage.save(snap, ts, r, write_hist)
            storage.db.commit()
            if write_hist:
                last_hist = started
            if started - last_clean > 600:
                storage.cleanup(retention)
                last_clean = started
            await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
    finally:
        iec_client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Gateway stopped.")

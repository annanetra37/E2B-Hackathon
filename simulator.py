"""
FAKE GRID  (ELI5: toy substations that behave like real ones)
==============================================================
For every substation in substations.yaml this starts a pretend version:

  primary (110/10 kV)     one Modbus TCP server (meters, transformer monitor)
                          + one IEC 104 RTU (breakers, frequency)
  distribution (-> 0.4 kV) one Modbus TCP server (L1/L2/L3 meter, temperature
                          sensor, disconnector input, current meters on lines)

The physics is simple but consistent: line currents add up to the transformer
load, the transformer warms up with load, and when something opens or a fuse
blows, everything behind it loses voltage.

Every now and then a substation gets a FAULT, which clears after a few minutes.
  primary:      feeder_trip, main_trip, overheat, undervoltage, overload, comm_loss
  distribution: fuse_blown, supply_loss, disconnector_open, overheat, overload,
                overvoltage, comm_loss

Environment variables:
  SIM_FAULT_EVERY_S   average seconds between faults per substation (default 500)
  SIM_INITIAL_FAULTS  how many substations start with a fault (default 3)

Run:  python simulator.py
"""
import asyncio
import logging
import math
import os
import random
import sqlite3
import struct
import time

import c104
from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                ModbusSlaveContext)
from pymodbus.server import StartAsyncTcpServer

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s SIM %(message)s")
log = logging.getLogger("sim")
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

FAULT_EVERY_S = float(os.environ.get("SIM_FAULT_EVERY_S", 500))
INITIAL_FAULTS = int(os.environ.get("SIM_INITIAL_FAULTS", 3))
SQRT3, PF = math.sqrt(3), 0.95


def float_regs(*values: float) -> list[int]:
    regs = []
    for v in values:
        regs += list(struct.unpack(">HH", struct.pack(">f", v)))
    return regs


class FakeBase:
    """Shared parts: the Modbus 'memory' of every device, and fault start/stop."""
    FAULTS: dict[str, int] = {}
    COMM_UNIT_KEY = ""

    def __init__(self, cfg: dict, sub: dict):
        self.cfg, self.sub, self.id = cfg, sub, sub["id"]
        self.phase = random.uniform(0, 6.28)
        self.fault: dict | None = None
        self.slaves = {d["unit_id"]: ModbusSlaveContext(hr=ModbusSequentialDataBlock(0, [0] * 32), zero_mode=True)
                       for d in config.modbus_devices(cfg, sub)}
        self.ctx = ModbusServerContext(slaves=dict(self.slaves), single=False)

    def comm_unit(self) -> int:
        return self.lay[self.COMM_UNIT_KEY]

    def start_fault(self, kind: str, duration_s: float | None = None):
        self.clear_fault(quiet=True)
        self.fault = {"type": kind, "target": random.randrange(self.n_targets()),
                      "phase": random.randrange(3),
                      "until": time.time() + (duration_s or random.uniform(90, 300))}
        if kind == "comm_loss":
            del self.ctx[self.comm_unit()]
        self.on_fault_start()
        log.info("%-14s FAULT  %s", self.id, kind)

    def clear_fault(self, quiet: bool = False):
        if not self.fault:
            return
        if self.fault["type"] == "comm_loss":
            self.ctx[self.comm_unit()] = self.slaves[self.comm_unit()]
        self.on_fault_clear()
        if not quiet:
            log.info("%-14s CLEAR  %s", self.id, self.fault["type"])
        self.fault = None

    def maybe_change_fault(self):
        if self.fault and time.time() > self.fault["until"]:
            self.clear_fault()
        elif not self.fault and random.random() < 1 / FAULT_EVERY_S:
            self.start_fault(random.choices(list(self.FAULTS), weights=list(self.FAULTS.values()))[0])

    def on_fault_start(self): ...
    def on_fault_clear(self): ...
    def n_targets(self) -> int: return 1


# =========================== primary 110/10 kV ===========================
class FakePrimary(FakeBase):
    FAULTS = {"feeder_trip": 4, "overheat": 2, "undervoltage": 2, "overload": 2, "comm_loss": 2, "main_trip": 1}
    COMM_UNIT_KEY = "transformer_unit"

    def __init__(self, cfg: dict, sub: dict):
        self.lay = cfg["layout"]["primary"]
        super().__init__(cfg, sub)
        self.mva, self.n = sub["transformer_mva"], len(sub["feeders"])
        self.base_amps = [random.uniform(140, 250) for _ in range(self.n)]
        self.oil, self.tap = 50.0, 9
        self.q0, self.q1, self.closed = True, True, [True] * self.n
        i104 = sub["iec104"]
        self.server = c104.Server(ip=i104["host"], port=i104["port"])
        station = self.server.add_station(common_address=i104["common_address"])
        self.points = {}
        for p in config.iec104_points(cfg, sub):
            kind = c104.Type.M_ME_NC_1 if p["type"] == "float" else c104.Type.M_SP_NA_1
            self.points[p["key"]] = station.add_point(io_address=p["ioa"], type=kind, report_ms=1000)
        self.server.start()

    def n_targets(self): return self.n

    def on_fault_start(self):
        if self.fault["type"] == "feeder_trip":
            self.closed[self.fault["target"]] = False
        elif self.fault["type"] == "main_trip":
            self.q0 = False

    def on_fault_clear(self):
        self.q0, self.q1, self.closed = True, True, [True] * self.n

    def tick(self, t: float):
        f = self.fault["type"] if self.fault else None
        line_kv = 110 * (1 + 0.012 * math.sin(t / 50 + self.phase)) + random.uniform(-0.3, 0.3)
        if f == "undervoltage":
            line_kv *= 0.87
        freq = 50 + 0.03 * math.sin(t / 25 + self.phase) + random.uniform(-0.01, 0.01)
        bus_kv = 10.5 * line_kv / 110 + random.uniform(-0.02, 0.02) if (self.q0 and self.q1) else 0.0
        feeders = []
        for i in range(self.n):
            amps = 0.0
            if bus_kv and self.closed[i]:
                amps = self.base_amps[i] * (1 + 0.15 * math.sin(t / 40 + i + self.phase)) + random.uniform(-3, 3)
                if f == "overload" and i == self.fault["target"]:
                    amps = 455 + 10 * math.sin(t / 7)
            kv = bus_kv if self.closed[i] else 0.0          # meter sits after the breaker
            feeders.append((kv, max(amps, 0.0), max(SQRT3 * kv * amps * PF / 1000, 0.0)))
        total_mw = sum(mw for _, _, mw in feeders)
        load_pct = total_mw / PF / self.mva * 100
        target = 96 if f == "overheat" else 38 + 0.35 * load_pct
        self.oil += (target - self.oil) * (0.03 if f == "overheat" else 0.01)
        if random.random() < 0.003:
            self.tap = max(5, min(13, self.tap + random.choice([-1, 1])))
        line_mw = total_mw * 1.01
        line_a = line_mw * 1000 / (SQRT3 * line_kv * PF) if self.q0 else 0.0
        bus_a = total_mw * 1000 / (SQRT3 * bus_kv * PF) if bus_kv else 0.0

        L, S = self.lay, self.slaves
        S[L["line_meter_unit"]].setValues(3, 0, float_regs(line_kv, line_a, line_mw))
        S[L["bus_meter_unit"]].setValues(3, 0, float_regs(bus_kv, bus_a, total_mw))
        for i, (kv, a, mw) in enumerate(feeders):
            S[L["feeder_meter_unit_start"] + i].setValues(3, 0, float_regs(kv, a, mw))
        S[L["transformer_unit"]].setValues(3, 0, float_regs(self.oil, load_pct) + [self.tap])
        self.points["frequency"].value = freq
        self.points["Q0"].value, self.points["Q1"].value = self.q0, self.q1
        for i in range(self.n):
            self.points[f"QF{i + 1}"].value = self.closed[i]


# =================== distribution station (the sketch) ===================
class FakeDistribution(FakeBase):
    FAULTS = {"fuse_blown": 3, "supply_loss": 2, "disconnector_open": 1, "overheat": 2,
              "overload": 2, "overvoltage": 2, "comm_loss": 2}
    COMM_UNIT_KEY = "temp_sensor_unit"

    def __init__(self, cfg: dict, sub: dict):
        self.lay = cfg["layout"]["distribution"]
        super().__init__(cfg, sub)
        self.kva, self.lines = sub["transformer_kva"], sub["lines"]
        per_line = self.kva * 1000 / (3 * 230) / len(self.lines)       # amps per phase per line at 100 %
        self.base = [[per_line * random.uniform(0.35, 0.6) for _ in range(3)] for _ in self.lines]
        self.v_offset = [random.uniform(-3, 3), random.uniform(0, 9), random.uniform(-4, 2)]  # like 230 / 241
        self.temp = 50.0
        self.qs_closed = True

    def n_targets(self):
        measured = [i for i, ln in enumerate(self.lines) if ln.get("measured")]
        return max(1, len(measured))

    def on_fault_start(self):
        if self.fault["type"] == "disconnector_open":
            self.qs_closed = False
        if self.fault["type"] == "overload":   # pick a line that has a meter, so it can be seen
            measured = [i for i, ln in enumerate(self.lines) if ln.get("measured")]
            self.fault["target"] = random.choice(measured)

    def on_fault_clear(self):
        self.qs_closed = True

    def tick(self, t: float):
        f = self.fault["type"] if self.fault else None
        supply = self.qs_closed and f != "supply_loss"
        phase_alive = [supply] * 3
        if f == "fuse_blown":
            phase_alive[self.fault["phase"]] = False

        # currents of every line, per phase
        line_amps = []
        for i, ln in enumerate(self.lines):
            amps = []
            for p in range(3):
                a = 0.0
                if phase_alive[p]:
                    a = self.base[i][p] * (1 + 0.2 * math.sin(t / 45 + i * 1.3 + p + self.phase)) + random.uniform(-2, 2)
                    if f == "overload" and i == self.fault["target"]:
                        a = ln.get("rating_a", 250) * 1.18 + 5 * math.sin(t / 6 + p)
                amps.append(max(a, 0.0))
            line_amps.append(amps)
        total = [sum(la[p] for la in line_amps) for p in range(3)]

        volts = []
        for p in range(3):
            if not phase_alive[p]:
                volts.append(random.uniform(0, 2))
                continue
            v = 230 + self.v_offset[p] - total[p] * 0.012 + 1.5 * math.sin(t / 60 + p + self.phase) + random.uniform(-0.4, 0.4)
            if f == "overvoltage" and p == self.fault["phase"]:
                v = 259 + random.uniform(-1, 1)
            volts.append(v)
        kw = sum(v * a for v, a in zip(volts, total)) * PF / 1000
        load_pct = sum(v * a for v, a in zip(volts, total)) / 1000 / self.kva * 100
        target = 94 if f == "overheat" else 25 + 0.55 * load_pct
        self.temp += (target - self.temp) * (0.03 if f == "overheat" else 0.01)

        L, S = self.lay, self.slaves
        S[L["lv_meter_unit"]].setValues(3, 0, float_regs(*volts, *total, kw))
        S[L["temp_sensor_unit"]].setValues(3, 0, float_regs(self.temp))
        S[L["di_module_unit"]].setValues(3, 0, [1 if self.qs_closed else 0])
        for i, ln in enumerate(self.lines):
            if ln.get("measured"):
                S[L["ct_meter_unit_start"] + i].setValues(3, 0, float_regs(*line_amps[i]))


# ---------------- demo controls (buttons in the portal) ----------------
def read_demo_commands(db_file, subs: dict):
    """The portal's demo buttons drop commands into the database; we act on them here.
    This only changes the FAKE grid. The gateway itself never sends commands."""
    if not db_file.exists():
        return
    try:
        with sqlite3.connect(db_file, timeout=2) as db:
            rows = db.execute("SELECT id, substation, action FROM sim_commands WHERE done=0").fetchall()
            for cid, sid, action in rows:
                sub = subs.get(sid)
                if sub and action == "clear":
                    sub.clear_fault()
                elif sub and action in sub.FAULTS:
                    sub.start_fault(action, duration_s=600)
                db.execute("UPDATE sim_commands SET done=1 WHERE id=?", (cid,))
    except sqlite3.Error:
        pass  # table not created yet by the gateway


async def run():
    cfg = config.load()
    db_file = config.db_path(cfg)
    kinds = {"primary": FakePrimary, "distribution": FakeDistribution}
    subs = {s["id"]: kinds[s["type"]](cfg, s) for s in cfg["substations"]}
    for s in random.sample(list(subs.values()), min(INITIAL_FAULTS, len(subs))):
        s.start_fault(random.choice([k for k in s.FAULTS if k not in ("main_trip", "supply_loss")]))

    servers = [StartAsyncTcpServer(context=s.ctx, address=(s.sub["modbus"]["host"], s.sub["modbus"]["port"]))
               for s in subs.values()]
    log.info("Fake grid running: %d substations", len(subs))

    async def physics_loop():
        start = time.time()
        while True:
            t = time.time() - start
            for s in subs.values():
                s.maybe_change_fault()
                s.tick(t)
            read_demo_commands(db_file, subs)
            await asyncio.sleep(1)

    await asyncio.gather(physics_loop(), *servers)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nFake grid stopped.")

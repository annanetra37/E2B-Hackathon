"""
Plan B simulator (OPS-1): a virtual socket strip. Appliances are "plugged in" from the
Simulator tab (/demo/sim) and the meter readings they would cause flow through the SAME path
as real ones (store -> alarm engine -> SSE -> SMS).

Each appliance has a nominal power per mode and a power factor. The simulated meter adds them
up with a little noise: P = sum(P_i), apparent power S = sum(P_i / pf_i), PF = P / S,
I = S / V, and the voltage sags slightly under load, like a real socket.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone

from .parser import iso


def _light_w() -> float:
    try:
        return float(os.environ.get("SIM_LIGHT_W", "") or 9.0)
    except ValueError:
        return 9.0


# id, label, modes (name -> W), power factor. Order = order on the Simulator tab.
APPLIANCES: list[dict] = [
    {"id": "led", "name": "LED lamp", "modes": {"on": _light_w()}, "pf": 0.92},
    {"id": "charger", "name": "Phone charger", "modes": {"on": 12.0}, "pf": 0.6},
    {"id": "fan", "name": "Desk fan", "modes": {"on": 45.0}, "pf": 0.8},
    {"id": "bulb", "name": "Old 60 W bulb", "modes": {"on": 60.0}, "pf": 1.0},
    {"id": "laptop", "name": "Laptop", "modes": {"on": 65.0}, "pf": 0.9},
    {"id": "tv", "name": "TV", "modes": {"on": 110.0}, "pf": 0.95},
    {"id": "iron", "name": "Iron", "modes": {"on": 1800.0}, "pf": 1.0},
    {"id": "hair_dryer", "name": "Hair dryer", "modes": {"low": 900.0, "high": 2100.0}, "pf": 0.99},
    {"id": "heater", "name": "Heater", "modes": {"on": 2000.0}, "pf": 1.0},
    {"id": "kettle", "name": "Kettle", "modes": {"on": 2200.0}, "pf": 1.0},
]
BY_ID = {a["id"]: a for a in APPLIANCES}
LEVEL_W = {"small": 60.0, "low": 900.0, "high": 2100.0}   # legacy admin buttons


class MeterSimulator:
    def __init__(self, station_id: str, seed: int | None = None):
        self.station_id = station_id
        self.rng = random.Random(seed)
        self.state: dict[str, str] = {"led": "on"}          # appliance id -> mode; absent = off
        self.energy_kwh = 12.0 + self.rng.random()
        self._last: datetime | None = None
        self._jitter: dict[str, float] = {}

    # ------------------------------------------------------------------ control
    def set_appliance(self, appliance_id: str, mode: str) -> None:
        a = BY_ID.get(appliance_id)
        if a is None:
            raise ValueError("unknown appliance")
        if mode in ("off", "", None):
            self.state.pop(appliance_id, None)
        elif mode in a["modes"]:
            self.state[appliance_id] = mode
        else:
            raise ValueError("unknown mode")

    def all_off(self) -> None:
        self.state.clear()

    # legacy controls used by the admin page
    def set_light(self, on: bool) -> None:
        self.set_appliance("led", "on" if on else "off")

    def set_hair_dryer(self, on: bool, level: str = "high") -> None:
        for k in ("hair_dryer", "bulb"):
            self.state.pop(k, None)
        if on:
            if level == "small":
                self.state["bulb"] = "on"
            else:
                self.state["hair_dryer"] = level if level in ("low", "high") else "high"

    @property
    def light(self) -> bool:
        return "led" in self.state

    @property
    def hair_dryer(self) -> bool:
        return "hair_dryer" in self.state or "bulb" in self.state

    @property
    def level(self) -> str:
        if "hair_dryer" in self.state:
            return self.state["hair_dryer"]
        return "small" if "bulb" in self.state else "high"

    def nominal_w(self) -> float:
        return sum(BY_ID[k]["modes"][m] for k, m in self.state.items())

    def appliances(self) -> list[dict]:
        return [{"id": a["id"], "name": a["name"], "modes": a["modes"], "pf": a["pf"],
                 "mode": self.state.get(a["id"], "off")} for a in APPLIANCES]

    # ------------------------------------------------------------------ meter
    def reading(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        r = self.rng
        power = apparent = 0.0
        for k, mode in self.state.items():
            a = BY_ID[k]
            j = self._jitter.get(k, 1.0) + r.uniform(-0.006, 0.006)      # slow drift, +-3 %
            self._jitter[k] = j = min(1.03, max(0.97, j))
            p = a["modes"][mode] * j
            power += p
            apparent += p / a["pf"]
        voltage = r.uniform(228.0, 232.0) - power / 1000 * 1.5        # small sag under load
        pf = power / apparent if apparent > 0 else 1.0
        current = apparent / voltage if apparent > 0 else 0.0
        if self._last is not None:
            self.energy_kwh += power * (now - self._last).total_seconds() / 3_600_000
        self._last = now
        return {
            "stationId": self.station_id, "ts": iso(now), "receivedAt": iso(now),
            "voltageV": round(voltage, 1), "currentA": round(current, 2), "powerW": round(power, 1),
            "pf": round(pf, 3), "freqHz": round(50 + r.uniform(-0.03, 0.03), 2),
            "energyKwh": round(self.energy_kwh, 4), "late": False, "source": "simulator",
        }

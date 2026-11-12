"""
Plan B simulator (OPS-1): fake meter readings that flow through the SAME path as real ones
(store -> alarm engine -> SSE -> SMS). Lamp 30-50 W with noise, V 228-232;
hair dryer low ~900 W, high ~2,100 W. Driven from the admin page.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from .parser import iso

LEVEL_W = {"low": 900.0, "high": 2100.0}


class MeterSimulator:
    def __init__(self, station_id: str, seed: int | None = None):
        self.station_id = station_id
        self.rng = random.Random(seed)
        self.hair_dryer = False
        self.level = "high"
        self.energy_kwh = 12.0 + self.rng.random()
        self._last: datetime | None = None
        self._lamp = 40.0

    def set_hair_dryer(self, on: bool, level: str = "high") -> None:
        self.hair_dryer = bool(on)
        if level in LEVEL_W:
            self.level = level

    def reading(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        r = self.rng
        self._lamp = min(50.0, max(30.0, self._lamp + r.uniform(-2.5, 2.5)))
        voltage = r.uniform(228.0, 232.0)
        power = self._lamp
        pf = 0.95 + r.uniform(-0.02, 0.02)
        if self.hair_dryer:
            dryer = LEVEL_W[self.level] * r.uniform(0.97, 1.03)
            voltage -= dryer / 1000.0                    # a small sag under load looks real
            power += dryer
            pf = 0.99 + r.uniform(-0.005, 0.005)
        if self._last is not None:
            self.energy_kwh += power * (now - self._last).total_seconds() / 3_600_000
        self._last = now
        current = power / (voltage * pf)
        return {
            "stationId": self.station_id, "ts": iso(now), "receivedAt": iso(now),
            "voltageV": round(voltage, 1), "currentA": round(current, 2), "powerW": round(power, 1),
            "pf": round(pf, 3), "freqHz": round(50 + r.uniform(-0.03, 0.03), 2),
            "energyKwh": round(self.energy_kwh, 4), "late": False, "source": "simulator",
        }

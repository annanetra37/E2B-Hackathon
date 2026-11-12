"""
Tailored SMS texts (alert, back-to-normal, data lost), built from what the meter measured
during the episode: how big the jump is, what kind of device that looks like, current vs the
socket rating, voltage sag, power factor, duration, peak and extra energy, plus a fix that
matches the situation.

All texts are plain GSM-7 (no curly quotes, no "~", no Armenian script) and kept within
MAX_CHARS (2 concatenated SMS); lower-priority details are dropped first if a text is too long.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_CHARS = 306          # 2 GSM-7 segments of 153 characters
NOMINAL_V = 230.0
LOW_V = 207.0            # -10 %: below the legal band
LOW_PF = 0.8


def w(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f}"


def duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


@dataclass
class Episode:
    """One overload: from the first reading above the trigger until power is back down."""
    started_ts: float
    usual_w: float
    power_w: float                       # power when the alert fired
    current_a: float | None = None
    voltage_v: float | None = None
    pf: float | None = None
    peak_w: float = 0.0
    max_current_a: float | None = None
    min_voltage_v: float | None = None
    extra_wh: float = 0.0
    last_ts: float | None = None
    last_power: float | None = None
    samples: list = field(default_factory=list, repr=False)

    @classmethod
    def start(cls, reading: dict, started_ts: float, usual_w: float | None, now_ts: float) -> "Episode":
        p = reading.get("powerW") or 0.0
        ep = cls(started_ts=started_ts, usual_w=usual_w or 0.0, power_w=p, current_a=reading.get("currentA"),
                 voltage_v=reading.get("voltageV"), pf=reading.get("pf"))
        ep.extra_wh = max(0.0, p - ep.usual_w) * max(0.0, now_ts - started_ts) / 3600   # the hold time
        ep.add(reading, now_ts)
        return ep

    def add(self, reading: dict, ts: float) -> None:
        p = reading.get("powerW")
        if p is None:
            return
        if self.last_ts is not None and self.last_power is not None and ts > self.last_ts:
            self.extra_wh += max(0.0, self.last_power - self.usual_w) * min(ts - self.last_ts, 30) / 3600
        self.last_ts, self.last_power = ts, p
        self.peak_w = max(self.peak_w, p)
        a, v = reading.get("currentA"), reading.get("voltageV")
        if a is not None:
            self.max_current_a = a if self.max_current_a is None else max(self.max_current_a, a)
        if v is not None:
            self.min_voltage_v = v if self.min_voltage_v is None else min(self.min_voltage_v, v)

    @property
    def extra_w(self) -> float:
        return max(0.0, self.power_w - self.usual_w)


def device_guess(extra_w: float) -> str:
    if extra_w < 250:
        return "a small extra device (lamp, fan or charger)"
    if extra_w < 1200:
        return "a medium appliance (iron on low, small heater, microwave)"
    if extra_w < 2600:
        return "a heating appliance (hair dryer, kettle or iron)"
    return "a very heavy load, maybe several heaters at once"


def fit(parts: list[tuple[int, str]], limit: int = MAX_CHARS) -> str:
    """parts = [(priority, text)], lower number = more important. Drop the least important
    parts until the joined text fits."""
    keep = list(parts)
    while True:
        text = " ".join(t for _, t in keep if t)
        if len(text) <= limit or len(keep) <= 1:
            return text[:limit]
        keep.remove(max(keep, key=lambda x: x[0]))


def alert_sms(ep: Episode, *, station: str, time: str, rated_a: float, tariff: float = 0.0,
              currency: str = "AMD") -> str:
    head = f"E2B ALERT {station} {time}: power jumped to {w(ep.power_w)} W (usual {w(ep.usual_w)} W)."
    pct = (ep.current_a / rated_a * 100) if ep.current_a and rated_a else None
    if pct is None:
        load, fix = "", "Fix: switch off or unplug the device you just added; run heavy devices one at a time."
    elif pct >= 100:
        load = f"{ep.current_a:,.1f} A is OVER the {rated_a:g} A socket limit ({pct:.0f}%), the breaker may trip."
        fix = "Fix: unplug it NOW. Never run two heaters on one socket."
    elif pct >= 80:
        load = f"{ep.current_a:,.1f} A = {pct:.0f}% of the {rated_a:g} A socket, close to the limit."
        fix = "Fix: switch it off or do not add anything else on this line."
    else:
        load = f"{ep.current_a:,.1f} A = {pct:.0f}% of the {rated_a:g} A socket."
        fix = ("Fix: switch off the device you just added if it is not needed." if ep.extra_w < 250 else
               "Fix: switch it off or unplug it if not needed; run heavy devices one at a time.")
    kwh = ep.extra_w / 1000
    rate = f"It uses about {kwh:,.1f} kWh per hour" + (f" ({kwh * tariff:,.0f} {currency}/h)." if tariff else ".")
    guess = f"Looks like {device_guess(ep.extra_w)}."
    volt = ""
    v = ep.min_voltage_v if ep.min_voltage_v is not None else ep.voltage_v
    if v is not None and v < LOW_V:
        volt = f"Voltage sagged to {v:,.0f} V (normal {NOMINAL_V:.0f} V): the line is struggling."
    pfx = ""
    if ep.pf is not None and abs(ep.pf) < LOW_PF:
        pfx = f"PF {abs(ep.pf):.2f}: motor-type load."
    load_prio = 1 if pct is None or pct >= 50 else 4     # a low share of the socket matters less than the guess
    return fit([(0, head), (load_prio, load), (1, fix), (2, volt), (3, guess), (4, rate), (5, pfx)])


def recovery_sms(ep: Episode, *, station: str, time: str, now_w: float | None, ended_ts: float,
                 rated_a: float, tariff: float = 0.0, currency: str = "AMD") -> str:
    head = f"E2B OK {station} {time}: back to normal, {w(now_w)} W now."
    lasted = f"The spike lasted {duration(ended_ts - ep.started_ts)}, peak {w(ep.peak_w)} W"
    if ep.max_current_a is not None:
        lasted += f" ({ep.max_current_a:,.1f} A)"
    energy = f", about {ep.extra_wh:,.0f} Wh extra" if ep.extra_wh >= 1 else ""
    if energy and tariff:
        energy += f" ({ep.extra_wh / 1000 * tariff:,.0f} {currency})"
    lasted += energy + "."
    if ep.max_current_a is not None and rated_a and ep.max_current_a >= rated_a:
        tail = "Current went over the socket limit: check the breaker and that the plug is not hot."
    elif ep.min_voltage_v is not None and ep.min_voltage_v < LOW_V:
        tail = "Voltage dipped during the spike; avoid running heavy loads together."
    else:
        tail = "No action needed."
    return fit([(0, head), (1, lasted), (2, tail)])


def lost_sms(ep: Episode, *, station: str, time: str) -> str:
    return fit([
        (0, f"E2B WARNING {station} {time}: no data during the overload (last {w(ep.last_power)} W)."),
        (1, "Check the device on site and the gateway Wi-Fi/SIM; the screen turns green again when data is back."),
    ])

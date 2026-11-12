"""
Alarm engine (ALM-1, ALM-2, ALM-4): a small, pure state machine. No I/O here,
so it is easy to unit-test; the service persists events and sends SMS.

  GREEN  normal                     AMBER  power >= warn
  RED    overload held >= hold_s    GREY   no data for stale_after_s

Spec (§6):
  on reading r (skip if r.late):
    baseline = median(powerW of NORMAL readings in last BASELINE_WINDOW_SECONDS)
    overload = powerW >= ALARM  or (baseline > 0 and powerW >= baseline*MULT and powerW >= MIN_ALARM)
    highSince / lowSince track how long we have been above / below
    not RED and held overload >= HOLD          -> RED (SMS only on entry)
    RED and below CLEAR_W for >= CLEAR_S       -> AMBER / GREEN
    not RED                                    -> AMBER / GREEN (only if changed)
  every 1 s: no reading for STALE_AFTER_SECONDS -> GREY
  first reading after GREY: recompute state from that reading
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

from .settings import Thresholds

GREEN, AMBER, RED, GREY = "green", "amber", "red", "grey"
TEXT = {GREEN: "Normal", AMBER: "High load", RED: "Overload", GREY: "No data"}


@dataclass
class Transition:
    from_state: str
    to_state: str
    ts: float              # epoch seconds (reading time, or wall clock for GREY)
    power_w: float | None  # power at the moment of the transition
    peak_w: float | None   # peak power of the RED episode that just ended (on RED exit)
    reason: str

    @property
    def entered_red(self) -> bool:
        return self.to_state == RED and self.from_state != RED

    @property
    def left_red(self) -> bool:
        return self.from_state == RED and self.to_state != RED


class AlarmEngine:
    def __init__(self, thresholds: Thresholds, stale_after_s: float = 15, initial: str = GREY):
        self.t = thresholds
        self.stale_after_s = stale_after_s
        self.state = initial
        self.since: float | None = None
        self.high_since: float | None = None
        self.low_since: float | None = None
        self.last_received: float | None = None     # wall clock of the last fresh reading
        self.peak_w: float | None = None
        self.red_entered_at: float | None = None
        self._normal: deque[tuple[float, float]] = deque()   # (ts, powerW) of GREEN readings

    # ------------------------------------------------------------------ helpers
    def baseline(self, now_ts: float) -> float | None:
        cutoff = now_ts - self.t.baseline_window_s
        while self._normal and self._normal[0][0] < cutoff:
            self._normal.popleft()
        if not self._normal:
            return None
        return statistics.median(p for _, p in self._normal)

    def is_overload(self, power: float, ts: float) -> bool:
        if power >= self.t.alarm_w:
            return True
        b = self.baseline(ts)
        return bool(b and b > 0 and power >= b * self.t.baseline_multiplier and power >= self.t.min_alarm_w)

    def _level(self, power: float) -> str:
        return AMBER if power >= self.t.warn_w else GREEN

    def _go(self, to: str, ts: float, power: float | None, reason: str) -> Transition | None:
        if to == self.state:
            return None
        tr = Transition(self.state, to, ts, power, None, reason)
        if tr.entered_red:
            self.peak_w = power
            self.red_entered_at = ts
        if tr.left_red:
            tr.peak_w = self.peak_w
            self.red_entered_at = None
        self.state, self.since = to, ts
        return tr

    # ------------------------------------------------------------------ inputs
    def on_reading(self, ts: float, power: float | None, received_at: float, late: bool = False) -> list[Transition]:
        """Feed one reading. Late (cache back-fill) readings never change the state."""
        if late or power is None:
            if not late and power is None:
                self.last_received = received_at
            return []
        self.last_received = received_at
        out: list[Transition] = []

        if self.state == GREY:                      # recompute from this reading, fresh timers
            self.high_since = self.low_since = None
            tr = self._go(self._level(power), ts, power, "Data is back")
            if tr:
                out.append(tr)

        overload = self.is_overload(power, ts)
        self.high_since = (self.high_since if self.high_since is not None else ts) if overload else None
        self.low_since = (self.low_since if self.low_since is not None else ts) if power < self.t.clear_w else None

        if self.state == RED:
            self.peak_w = max(self.peak_w or 0, power)

        if self.state != RED and self.high_since is not None and ts - self.high_since >= self.t.hold_s:
            tr = self._go(RED, ts, power, f"Power {power:,.0f} W above limit for {self.t.hold_s:g} s")
        elif self.state == RED and self.low_since is not None and ts - self.low_since >= self.t.clear_s:
            tr = self._go(self._level(power), ts, power, f"Power back below {self.t.clear_w:,.0f} W")
        elif self.state != RED:
            lvl = self._level(power)
            tr = self._go(lvl, ts, power, "High load" if lvl == AMBER else "Back to normal")
        else:
            tr = None
        if tr:
            out.append(tr)

        if self.state == GREEN and not overload:    # the "usual" level only learns from normal readings
            self._normal.append((ts, power))
        return out

    def tick(self, now: float) -> Transition | None:
        """Call every second with wall-clock time."""
        if self.state == GREY:
            return None
        if self.last_received is None or now - self.last_received > self.stale_after_s:
            self.high_since = self.low_since = None
            return self._go(GREY, now, None, f"No data for {self.stale_after_s:g} s")
        return None

    def reset(self, now: float) -> Transition | None:
        """Admin: force GREEN and clear timers."""
        self.high_since = self.low_since = None
        self._normal.clear()
        return self._go(GREEN, now, None, "Reset by operator")

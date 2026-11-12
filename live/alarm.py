"""
Alarm engine (ALM-1, ALM-2, ALM-4): a small, pure state machine. No I/O here,
so it is easy to unit-test; the service persists events and sends SMS.

  GREEN  normal                     AMBER  power >= warn
  RED    overload held >= hold_s    GREY   no data for stale_after_s

Spec (§6):
  on reading r (skip if r.late):
    baseline = median(powerW of NORMAL readings in last BASELINE_WINDOW_SECONDS)
    trigger  = min(ALARM, max(MIN_ALARM, baseline*MULT))   # any device that jumps above normal
    overload = powerW >= trigger                          # (no history yet: trigger = ALARM)
    RED clears below min(ALARM_CLEAR_W, 0.8 x trigger at RED entry)
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
CLEAR_FRACTION = 0.8
TEXT = {GREEN: "Normal", AMBER: "High load", RED: "Overload", GREY: "No data"}


@dataclass
class Transition:
    from_state: str
    to_state: str
    ts: float              # epoch seconds (reading time, or wall clock for GREY)
    power_w: float | None  # power at the moment of the transition
    peak_w: float | None   # peak power of the RED episode that just ended (on RED exit)
    reason: str
    usual_w: float | None = None   # usual (baseline) power just before entering RED
    started_ts: float | None = None  # RED entry: first reading above the trigger
    dropped_ts: float | None = None  # RED exit: first reading back below the clear level
    episode: object = None           # filled by the service (advice.Episode)
    sms_text: str | None = None      # filled by the service

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
        self.red_clear_w: float | None = None      # clear level frozen at RED entry
        self._normal: deque[tuple[float, float]] = deque()   # (ts, powerW) of GREEN readings

    # ------------------------------------------------------------------ helpers
    def baseline(self, now_ts: float) -> float | None:
        cutoff = now_ts - self.t.baseline_window_s
        while self._normal and self._normal[0][0] < cutoff:
            self._normal.popleft()
        if not self._normal:
            return None
        return statistics.median(p for _, p in self._normal)

    def trigger_w(self, ts: float) -> float:
        """Power at which a reading counts as unusual.

        = min(POWER_ALARM_W, max(POWER_MIN_ALARM_W, BASELINE_MULTIPLIER x usual level))
        i.e. above the absolute limit, or a sudden jump of any device: at least the floor and
        at least N x what is normally drawn. With only the LED on (~9 W, usual 0-9 W) any extra
        device >= the floor counts, whatever it is. Without any history yet (first reading after
        start or GREY) only the absolute limit applies, so a load already running is not a "jump"."""
        b = self.baseline(ts)
        if b is None:
            return self.t.alarm_w
        return min(self.t.alarm_w, max(self.t.min_alarm_w, max(b, 0.0) * self.t.baseline_multiplier))

    def is_overload(self, power: float, ts: float) -> bool:
        return power >= self.trigger_w(ts)

    def clear_level(self, trigger_w: float) -> float:
        """RED clears below this: under ALARM_CLEAR_W and clearly under the level that triggered
        (hysteresis), so a small device left on stays RED instead of flapping red/green."""
        return min(self.t.clear_w, trigger_w * CLEAR_FRACTION)

    def usual_w(self, now_ts: float) -> float | None:
        return self.baseline(now_ts)

    def _level(self, power: float) -> str:
        return AMBER if power >= self.t.warn_w else GREEN

    def _go(self, to: str, ts: float, power: float | None, reason: str) -> Transition | None:
        if to == self.state:
            return None
        tr = Transition(self.state, to, ts, power, None, reason)
        if tr.entered_red:
            tr.usual_w = self.baseline(ts)
            tr.started_ts = self.high_since if self.high_since is not None else ts
            self.peak_w = power
            self.red_entered_at = ts
        if tr.left_red:
            tr.dropped_ts = self.low_since if self.low_since is not None else ts
            tr.peak_w = self.peak_w
            self.red_entered_at = None
            self.red_clear_w = None
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

        trigger = self.trigger_w(ts)
        overload = power >= trigger
        clear_w = self.red_clear_w if (self.state == RED and self.red_clear_w is not None) else self.clear_level(trigger)
        self.high_since = (self.high_since if self.high_since is not None else ts) if overload else None
        self.low_since = (self.low_since if self.low_since is not None else ts) if power < clear_w else None

        if self.state == RED:
            self.peak_w = max(self.peak_w or 0, power)

        if self.state != RED and self.high_since is not None and ts - self.high_since >= self.t.hold_s:
            tr = self._go(RED, ts, power, f"Power {power:,.0f} W above {trigger:,.0f} W for {self.t.hold_s:g} s")
            if tr:
                self.red_clear_w = self.clear_level(trigger)
        elif self.state == RED and self.low_since is not None and ts - self.low_since >= self.t.clear_s:
            tr = self._go(self._level(power), ts, power, f"Power back below {clear_w:,.0f} W")
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

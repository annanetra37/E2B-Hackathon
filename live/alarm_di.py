"""
On/off (DI) alarm engine: the gateway's digital input says whether the substation supplies
electricity (di1 = 1) or not (di1 = 0). Pure state machine, no I/O, so it is easy to test;
the service persists the confirmed state and sends the SMS.

  GREEN  di1 = 1 held >= DI_ON_HOLD_SECONDS        "Supplying electricity"
  RED    di1 = 0 held >= DI_OFF_HOLD_SECONDS       "No electricity output"   (outage SMS)
  GREY   no message for STALE_AFTER_SECONDS        "No data from gateway"     (never an outage)

Two notions of state:
  confirmed  the last real on/off state (green / red / None = never known). Persisted, so an app
             restart or a GREY period never re-sends an SMS: an SMS goes out only when a new reading,
             held long enough, differs from the confirmed state.
  state      what the page shows: confirmed, or GREY while data is missing.

Transition kinds (what the service does with them):
  outage    confirmed -> red         outage SMS to everyone
  restored  confirmed red -> green   restored SMS (with the outage duration) to who got the outage SMS
  display   page change only (grey, data back, first state ever)
  nodata    GREY lasted NO_DATA_SMS_AFTER_SECONDS (optional SMS_ON_NO_DATA)
"""
from __future__ import annotations

from dataclasses import dataclass

GREEN, RED, GREY = "green", "red", "grey"
TEXT = {GREEN: "Supplying electricity", RED: "No electricity output", GREY: "No data from gateway"}


@dataclass
class DiTransition:
    from_state: str
    to_state: str
    ts: float                         # when the engine decided (reading time, or wall clock for GREY)
    reason: str
    kind: str = "display"             # outage | restored | display | nodata
    changed_at: float | None = None   # when the input really changed (start of the held 0 or 1)
    outage_started: float | None = None
    duration_s: float | None = None   # outage length, on "restored"
    # same attributes as alarm.Transition so the service can treat both alike
    power_w: float | None = None
    peak_w: float | None = None
    usual_w: float | None = None
    started_ts: float | None = None
    dropped_ts: float | None = None
    episode: object = None
    sms_text: object = None

    @property
    def entered_red(self) -> bool:
        return self.kind == "outage"

    @property
    def left_red(self) -> bool:
        return self.kind == "restored"


class DiAlarmEngine:
    def __init__(self, settings, *, confirmed: str | None = None, confirmed_since: float | None = None,
                 outage_started: float | None = None):
        self.s = settings                 # reads di_on_hold_s / di_off_hold_s / stale_after_s live
        self.state = GREY
        self.since: float | None = None
        self.confirmed = confirmed if confirmed in (GREEN, RED) else None
        self.confirmed_since = confirmed_since
        self.outage_started = outage_started if self.confirmed == RED else None
        self.last_received: float | None = None
        self.last_value: bool | None = None
        self.cand: str | None = None      # value currently being held
        self.cand_since: float | None = None
        self.nodata_sent = False
        self.peak_w = None                # power-engine attribute, unused in DI mode

    @property
    def stale_after_s(self) -> float:
        return self.s.stale_after_s

    # ------------------------------------------------------------------ inputs
    def on_reading(self, ts: float, value: bool | None, received_at: float, late: bool = False) -> list[DiTransition]:
        """Feed one reading (value = supplyOn). Late (cached) readings never change anything."""
        if late or value is None:
            return []
        self.last_received = received_at
        self.last_value = bool(value)
        v = GREEN if value else RED
        out: list[DiTransition] = []
        if self.cand != v:
            self.cand, self.cand_since = v, ts

        # data back after GREY with the same state as before: show it at once, no SMS
        if self.state == GREY and self.confirmed == v:
            out.append(DiTransition(GREY, v, ts, "Data is back", "display", changed_at=self.confirmed_since))
            self.state, self.since, self.nodata_sent = v, ts, False

        hold = self.s.di_on_hold_s if v == GREEN else self.s.di_off_hold_s
        if self.confirmed != v and ts - self.cand_since >= hold:
            prev, frm = self.confirmed, self.state
            if v == RED:
                tr = DiTransition(frm, RED, ts, "Supply lost", "outage", changed_at=self.cand_since,
                                  outage_started=self.cand_since)
                self.outage_started = self.cand_since
            elif prev == RED:
                started = self.outage_started
                tr = DiTransition(frm, GREEN, ts, "Supply restored", "restored", changed_at=self.cand_since,
                                  outage_started=started,
                                  duration_s=(self.cand_since - started) if started is not None else None)
                self.outage_started = None
            else:                        # first state ever known: nothing to report
                tr = DiTransition(frm, GREEN, ts, "Supplying electricity", "display", changed_at=self.cand_since)
            self.confirmed, self.confirmed_since = v, self.cand_since
            self.state, self.since, self.nodata_sent = v, self.cand_since, False
            out.append(tr)
        return out

    def tick(self, now: float) -> list[DiTransition]:
        """Call every second with wall-clock time."""
        if self.state != GREY:
            if self.last_received is None or now - self.last_received > self.s.stale_after_s:
                frm = self.state
                self.state, self.since, self.cand, self.cand_since = GREY, now, None, None
                return [DiTransition(frm, GREY, now, "No data from gateway", "display")]
            return []
        if (self.s.sms_on_no_data and not self.nodata_sent and self.since is not None
                and now - self.since >= self.s.no_data_sms_after_s):
            self.nodata_sent = True
            return [DiTransition(GREY, GREY, now, "No data from gateway", "nodata", changed_at=self.since)]
        return []

    def reset(self, now: float) -> DiTransition | None:
        """Admin: forget the outage, treat the supply as on, clear timers (no SMS)."""
        frm = self.state
        self.cand = self.cand_since = None
        self.confirmed, self.confirmed_since, self.outage_started = GREEN, now, None
        self.state, self.since = GREEN, now
        if self.last_received is None or now - self.last_received > self.s.stale_after_s:
            self.last_received = now
        return DiTransition(frm, GREEN, now, "Reset by operator", "display") if frm != GREEN else None

    # ------------------------------------------------------------------ views
    @property
    def supply_on(self) -> bool | None:
        return True if self.confirmed == GREEN else False if self.confirmed == RED else None

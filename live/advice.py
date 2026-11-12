"""
Tailored SMS texts (alert, back-to-normal, data lost), built from what the meter measured
during the episode: how big the jump is, what kind of device that looks like, current vs the
socket rating, voltage sag, power factor, duration, peak and extra energy, plus a fix that
matches the situation.

Every text exists in English ("en") and Armenian ("hy"); each function returns {"en": ..., "hy": ...}.
compose(texts, lang) picks one, or both ("both": Armenian first, then English).

Length: English is GSM-7 (153 characters per concatenated SMS part), Armenian script needs UCS-2
(67 characters per part). Limits: English 2 parts (306), Armenian 4 parts (268), both 6 parts (402).
Lower-priority details are dropped first when a text is too long.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_CHARS = 306          # English: 2 GSM-7 parts
MAX_CHARS_HY = 268       # Armenian: 4 UCS-2 parts
MAX_CHARS_BOTH = 402     # both languages in one SMS: 6 UCS-2 parts
NOMINAL_V = 230.0
LOW_V = 207.0            # -10 %: below the legal band
LOW_PF = 0.8
LANGS = ("en", "hy")


def w(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f}"


def duration(seconds: float, lang: str = "en") -> str:
    s = max(0, int(round(seconds)))
    sec, mn, hr = ("s", "min", "h") if lang == "en" else ("վրկ", "րոպե", "ժամ")
    if s < 60:
        return f"{s} {sec}"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} {mn} {s} {sec}" if s else f"{m} {mn}"
    h, m = divmod(m, 60)
    return f"{h} {hr} {m} {mn}"


def money(currency: str, lang: str) -> str:
    return "դրամ" if lang == "hy" and currency.upper() == "AMD" else currency


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


DEVICES = {
    "en": ["a small extra device (lamp, fan or charger)",
           "a medium appliance (iron on low, small heater, microwave)",
           "a heating appliance (hair dryer, kettle or iron)",
           "a very heavy load, maybe several heaters at once"],
    "hy": ["փոքր լրացուցիչ սարք (լամպ, օդափոխիչ կամ լիցքավորիչ)",
           "միջին սարք (արդուկ, փոքր տաքացուցիչ, միկրոալիքային վառարան)",
           "տաքացնող սարք (վարսահարդարիչ, թեյնիկ կամ արդուկ)",
           "շատ մեծ բեռ, գուցե միանգամից մի քանի տաքացուցիչ"],
}


def device_guess(extra_w: float, lang: str = "en") -> str:
    i = 0 if extra_w < 250 else 1 if extra_w < 1200 else 2 if extra_w < 2600 else 3
    return DEVICES[lang][i]


def fit(parts: list[tuple[int, str]], limit: int = MAX_CHARS) -> str:
    """parts = [(priority, text)], lower number = more important. Drop the least important
    parts until the joined text fits."""
    keep = [p for p in parts if p[1]]
    while True:
        text = " ".join(t for _, t in keep)
        if len(text) <= limit or len(keep) <= 1:
            return text[:limit]
        keep.remove(max(keep, key=lambda x: x[0]))


def limit_for(lang: str) -> int:
    return MAX_CHARS if lang == "en" else MAX_CHARS_HY


def _all(parts_for) -> dict[str, str]:
    """{"en", "hy", "both"}; "both" = Armenian then English, each fitted to half of the
    6-part budget by dropping whole details (never cut mid-sentence)."""
    out = {lang: fit(parts_for(lang), limit_for(lang)) for lang in LANGS}
    half = (MAX_CHARS_BOTH - 2) // 2
    out["both"] = fit(parts_for("hy"), half) + "\n\n" + fit(parts_for("en"), half)
    return out


# ======================================================================= alert

def _alert_parts(ep: Episode, lang: str, station: str, time: str, rated_a: float, tariff: float,
                 currency: str) -> list[tuple[int, str]]:
    en = lang == "en"
    head = (f"E2B ALERT {station} {time}: power jumped to {w(ep.power_w)} W (usual {w(ep.usual_w)} W)." if en else
            f"E2B ԱՀԱԶԱՆԳ {station} {time}․ հզորությունը կտրուկ աճեց մինչև {w(ep.power_w)} Վտ "
            f"(սովորաբար {w(ep.usual_w)} Վտ)։")
    pct = (ep.current_a / rated_a * 100) if ep.current_a and rated_a else None
    a = f"{ep.current_a:,.1f}" if ep.current_a is not None else ""
    if pct is None:
        load = ""
        fix = ("Fix: switch off or unplug the device you just added; run heavy devices one at a time." if en else
               "Ինչ անել․ անջատեք նոր միացված սարքը, հզոր սարքերը միացրեք մեկ առ մեկ։")
    elif pct >= 100:
        load = (f"{a} A is OVER the {rated_a:g} A socket limit ({pct:.0f}%), the breaker may trip." if en else
                f"{a} Ա-ն ԳԵՐԱԶԱՆՑՈՒՄ է վարդակի {rated_a:g} Ա սահմանը ({pct:.0f}%), ավտոմատը կարող է անջատվել։")
        fix = ("Fix: unplug it NOW. Never run two heaters on one socket." if en else
               "Ինչ անել․ ԱՆՄԻՋԱՊԵՍ հանեք վարդակից։ Երկու տաքացուցիչ մեկ վարդակին մի միացրեք։")
    elif pct >= 80:
        load = (f"{a} A = {pct:.0f}% of the {rated_a:g} A socket, close to the limit." if en else
                f"{a} Ա = վարդակի {rated_a:g} Ա սահմանի {pct:.0f}%-ը, մոտ է սահմանին։")
        fix = ("Fix: switch it off or do not add anything else on this line." if en else
               "Ինչ անել․ անջատեք այն կամ այլ սարք մի միացրեք այս գծին։")
    else:
        load = (f"{a} A = {pct:.0f}% of the {rated_a:g} A socket." if en else
                f"{a} Ա = վարդակի {rated_a:g} Ա սահմանի {pct:.0f}%-ը։")
        if ep.extra_w < 250:
            fix = ("Fix: switch off the device you just added if it is not needed." if en else
                   "Ինչ անել․ անջատեք նոր միացված սարքը, եթե այն պետք չէ։")
        else:
            fix = ("Fix: switch it off or unplug it if not needed; run heavy devices one at a time." if en else
                   "Ինչ անել․ անջատեք կամ հանեք վարդակից, եթե պետք չէ։ Հզոր սարքերը միացրեք մեկ առ մեկ։")
    kwh = ep.extra_w / 1000
    cost = f" ({kwh * tariff:,.0f} {money(currency, lang)}/{'h' if en else 'ժ'})" if tariff else ""
    rate = (f"It uses about {kwh:,.1f} kWh per hour{cost}." if en else
            f"Այն ծախսում է մոտ {kwh:,.1f} կՎտժ ժամում{cost}։")
    guess = (f"Looks like {device_guess(ep.extra_w, lang)}." if en else
             f"Կարծես {device_guess(ep.extra_w, lang)} է։")
    volt = ""
    v = ep.min_voltage_v if ep.min_voltage_v is not None else ep.voltage_v
    if v is not None and v < LOW_V:
        volt = (f"Voltage sagged to {v:,.0f} V (normal {NOMINAL_V:.0f} V): the line is struggling." if en else
                f"Լարումն իջավ մինչև {v:,.0f} Վ (նորմա՝ {NOMINAL_V:.0f} Վ)․ գիծը ծանրաբեռնված է։")
    pfx = ""
    if ep.pf is not None and abs(ep.pf) < LOW_PF:
        pfx = f"PF {abs(ep.pf):.2f}: motor-type load." if en else f"PF {abs(ep.pf):.2f}․ շարժիչային բեռ։"
    load_prio = 1 if pct is None or pct >= 50 else 4     # a low share of the socket matters less than the guess
    return [(0, head), (load_prio, load), (1, fix), (2, volt), (3, guess), (4, rate), (5, pfx)]


def alert_sms(ep: Episode, *, station: str, time: str, rated_a: float, tariff: float = 0.0,
              currency: str = "AMD") -> dict[str, str]:
    return _all(lambda lang: _alert_parts(ep, lang, station, time, rated_a, tariff, currency))


# ======================================================================= back to normal

def _recovery_parts(ep: Episode, lang: str, station: str, time: str, now_w: float | None, ended_ts: float,
                    rated_a: float, tariff: float, currency: str) -> list[tuple[int, str]]:
    en = lang == "en"
    head = (f"E2B OK {station} {time}: back to normal, {w(now_w)} W now." if en else
            f"E2B ՆՈՐՄԱ {station} {time}․ ամեն ինչ նորմալ է, հիմա {w(now_w)} Վտ։")
    dur = duration(ended_ts - ep.started_ts, lang)
    lasted = (f"The spike lasted {dur}, peak {w(ep.peak_w)} W" if en else
              f"Թռիչքը տևեց {dur}, առավելագույնը՝ {w(ep.peak_w)} Վտ")
    if ep.max_current_a is not None:
        lasted += f" ({ep.max_current_a:,.1f} {'A' if en else 'Ա'})"
    if ep.extra_wh >= 1:
        lasted += (f", about {ep.extra_wh:,.0f} Wh extra" if en else f", մոտ {ep.extra_wh:,.0f} Վտժ լրացուցիչ")
        if tariff:
            lasted += f" ({ep.extra_wh / 1000 * tariff:,.0f} {money(currency, lang)})"
    lasted += "." if en else "։"
    if ep.max_current_a is not None and rated_a and ep.max_current_a >= rated_a:
        tail = ("Current went over the socket limit: check the breaker and that the plug is not hot." if en else
                "Հոսանքը գերազանցեց վարդակի սահմանը․ ստուգեք ավտոմատը և որ խրոցը տաք չէ։")
    elif ep.min_voltage_v is not None and ep.min_voltage_v < LOW_V:
        tail = ("Voltage dipped during the spike; avoid running heavy loads together." if en else
                "Թռիչքի ժամանակ լարումն իջավ․ հզոր սարքերը միասին մի միացրեք։")
    else:
        tail = "No action needed." if en else "Ոչինչ անել պետք չէ։"
    return [(0, head), (1, lasted), (2, tail)]


def recovery_sms(ep: Episode, *, station: str, time: str, now_w: float | None, ended_ts: float,
                 rated_a: float, tariff: float = 0.0, currency: str = "AMD") -> dict[str, str]:
    return _all(lambda lang: _recovery_parts(ep, lang, station, time, now_w, ended_ts, rated_a, tariff, currency))


# ======================================================================= data lost

def lost_sms(ep: Episode, *, station: str, time: str) -> dict[str, str]:
    parts = {
        "en": [(0, f"E2B WARNING {station} {time}: no data during the overload (last {w(ep.last_power)} W)."),
               (1, "Check the device on site and the gateway Wi-Fi/SIM; the screen turns green again when "
                   "data is back.")],
        "hy": [(0, f"E2B ԶԳՈՒՇԱՑՈՒՄ {station} {time}․ գերբեռնման ժամանակ տվյալները կորան "
                   f"(վերջինը՝ {w(ep.last_power)} Վտ)։"),
               (1, "Ստուգեք սարքը տեղում և gateway-ի Wi-Fi/SIM-ը․ կապը վերականգնվելիս էկրանը կրկին կկանաչի։")],
    }
    return _all(lambda lang: parts[lang])


# ======================================================================= welcome / test

def welcome_sms(*, station: str, unsub_url: str) -> dict[str, str]:
    parts = {"en": [(0, f"Welcome to the E2b live demo at {station}. You will get an SMS when power spikes."),
                    (0, f"Stop: {unsub_url}")],
             "hy": [(0, f"Բարի գալուստ E2b-ի կենդանի ցուցադրություն ({station})։ SMS կստանաք հզորության "
                        f"թռիչքի դեպքում։"), (0, f"Դադարեցնել՝ {unsub_url}")]}
    return _all(lambda lang: parts[lang])


def test_sms(*, station: str, time: str) -> dict[str, str]:
    parts = {"en": [(0, f"E2B test SMS from {station}. {time}")],
             "hy": [(0, f"E2B փորձնական SMS {station}-ից։ {time}")]}
    return _all(lambda lang: parts[lang])


# ======================================================================= language choice

def compose(texts: dict[str, str] | str, lang: str) -> str:
    """Pick the text for one recipient: "en", "hy" or "both" (Armenian first, then English).
    A plain string (SMS_STYLE=template) goes to everyone as it is."""
    if isinstance(texts, str):
        return texts
    return texts.get(lang) or texts.get("en") or next(iter(texts.values()))

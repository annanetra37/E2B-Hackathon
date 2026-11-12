"""Tailored SMS texts (live/advice.py)."""
from live.advice import (MAX_CHARS, MAX_CHARS_BOTH, MAX_CHARS_HY, Episode, alert_sms, compose, duration,
                         lost_sms, recovery_sms, welcome_sms)
from live.notifier import is_gsm7

ST = "DigiTech Substation"


def ep_for(power, current, voltage=229.0, pf=0.99, usual=9.0):
    r = {"powerW": power, "currentA": current, "voltageV": voltage, "pf": pf}
    return Episode.start(r, started_ts=100.0, usual_w=usual, now_ts=102.0)


def check(texts):
    """Every language variant within its SMS budget; returns the English text for the asserts."""
    assert set(texts) == {"en", "hy", "both"}
    assert len(texts["en"]) <= MAX_CHARS and is_gsm7(texts["en"]), texts["en"]
    assert len(texts["hy"]) <= MAX_CHARS_HY and "E2B" in texts["hy"], texts["hy"]
    assert len(texts["both"]) <= MAX_CHARS_BOTH and texts["both"].startswith(texts["hy"].split(" ")[0])
    assert any("\u0531" <= c <= "\u058f" for c in texts["hy"])          # Armenian script
    return texts["en"]


def test_alert_normal_heater():
    t = check(alert_sms(ep_for(2134, 9.3), station=ST, time="14:02:11", rated_a=16))
    assert "2,134 W (usual 9 W)" in t and "9.3 A = 58% of the 16 A socket" in t
    assert "hair dryer" in t and "Fix:" in t and "2.1 kWh per hour" in t


def test_alert_over_socket_limit_is_urgent():
    t = check(alert_sms(ep_for(3900, 17.6, voltage=221), station=ST, time="14:02:11", rated_a=16))
    assert "OVER the 16 A socket limit" in t and "unplug it NOW" in t


def test_alert_small_device_and_low_voltage_and_pf():
    t = check(alert_sms(ep_for(75, 0.5, voltage=199, pf=0.6), station=ST, time="14:02:11", rated_a=16))
    assert "small extra device" in t and "sagged to 199 V" in t


def test_alert_with_tariff_still_fits():
    t = check(alert_sms(ep_for(3900, 17.6, voltage=199, pf=0.6), station=ST, time="14:02:11", rated_a=16,
                  tariff=48))
    assert t.startswith("E2B ALERT") and "Fix:" in t      # least important parts dropped first


def test_recovery_reports_duration_peak_energy():
    ep = ep_for(2100, 9.2)
    for ts in range(103, 164):
        ep.add({"powerW": 2100 + (50 if ts == 130 else 0), "currentA": 9.2, "voltageV": 228}, ts)
    t = check(recovery_sms(ep, station=ST, time="14:03:15", now_w=9, ended_ts=164, rated_a=16))
    assert "back to normal, 9 W now" in t and "lasted 1 min 4 s" in t and "peak 2,150 W" in t
    assert "Wh extra" in t and "No action needed" in t
    assert 34 <= ep.extra_wh <= 39                          # ~2,091 W extra for ~64 s


def test_recovery_after_over_limit_says_check_breaker():
    ep = ep_for(3900, 17.6)
    t = check(recovery_sms(ep, station=ST, time="14:03:15", now_w=9, ended_ts=130, rated_a=16))
    assert "check the breaker" in t


def test_lost_data_warning():
    t = check(lost_sms(ep_for(2100, 9.2), station=ST, time="14:03:15"))
    assert "no data during the overload" in t


def test_duration():
    assert duration(5) == "5 s" and duration(64) == "1 min 4 s" and duration(120) == "2 min"
    assert duration(3725) == "1 h 2 min"


def test_armenian_alert_has_numbers_and_fix():
    hy = alert_sms(ep_for(2134, 9.3), station=ST, time="14:02:11", rated_a=16)["hy"]
    assert "2,134 Վտ" in hy and "Ինչ անել" in hy


def test_armenian_over_limit_and_recovery():
    hy = alert_sms(ep_for(3900, 17.6), station=ST, time="14:02:11", rated_a=16)["hy"]
    assert "ԳԵՐԱԶԱՆՑՈՒՄ" in hy and "ԱՆՄԻՋԱՊԵՍ" in hy
    ep = ep_for(2100, 9.2)
    hy = recovery_sms(ep, station=ST, time="14:03:15", now_w=9, ended_ts=164, rated_a=16)["hy"]
    assert "1 րոպե 4 վրկ" in hy and "ամեն ինչ նորմալ է" in hy


def test_compose_picks_language():
    t = welcome_sms(station=ST, unsub_url="https://x.test/u/abc")
    assert compose(t, "hy").startswith("Բարի գալուստ") and compose(t, "en").startswith("Welcome")
    both = compose(t, "both")
    assert both.index("Բարի") < both.index("Welcome") and both.count("https://x.test/u/abc") == 2
    assert compose("plain template", "hy") == "plain template"

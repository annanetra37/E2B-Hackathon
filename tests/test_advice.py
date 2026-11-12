"""Tailored SMS texts (live/advice.py)."""
from live.advice import MAX_CHARS, Episode, alert_sms, duration, lost_sms, recovery_sms
from live.notifier import is_gsm7

ST = "DigiTech Substation"


def ep_for(power, current, voltage=229.0, pf=0.99, usual=9.0):
    r = {"powerW": power, "currentA": current, "voltageV": voltage, "pf": pf}
    return Episode.start(r, started_ts=100.0, usual_w=usual, now_ts=102.0)


def check(text):
    assert len(text) <= MAX_CHARS and is_gsm7(text), text


def test_alert_normal_heater():
    t = alert_sms(ep_for(2134, 9.3), station=ST, time="14:02:11", rated_a=16)
    check(t)
    assert "2,134 W (usual 9 W)" in t and "9.3 A = 58% of the 16 A socket" in t
    assert "hair dryer" in t and "Fix:" in t and "2.1 kWh per hour" in t


def test_alert_over_socket_limit_is_urgent():
    t = alert_sms(ep_for(3900, 17.6, voltage=221), station=ST, time="14:02:11", rated_a=16)
    check(t)
    assert "OVER the 16 A socket limit" in t and "unplug it NOW" in t


def test_alert_small_device_and_low_voltage_and_pf():
    t = alert_sms(ep_for(75, 0.5, voltage=199, pf=0.6), station=ST, time="14:02:11", rated_a=16)
    check(t)
    assert "small extra device" in t and "sagged to 199 V" in t


def test_alert_with_tariff_still_fits():
    t = alert_sms(ep_for(3900, 17.6, voltage=199, pf=0.6), station=ST, time="14:02:11", rated_a=16,
                  tariff=48)
    check(t)
    assert t.startswith("E2B ALERT") and "Fix:" in t      # least important parts dropped first


def test_recovery_reports_duration_peak_energy():
    ep = ep_for(2100, 9.2)
    for ts in range(103, 164):
        ep.add({"powerW": 2100 + (50 if ts == 130 else 0), "currentA": 9.2, "voltageV": 228}, ts)
    t = recovery_sms(ep, station=ST, time="14:03:15", now_w=9, ended_ts=164, rated_a=16)
    check(t)
    assert "back to normal, 9 W now" in t and "lasted 1 min 4 s" in t and "peak 2,150 W" in t
    assert "Wh extra" in t and "No action needed" in t
    assert 34 <= ep.extra_wh <= 39                          # ~2,091 W extra for ~64 s


def test_recovery_after_over_limit_says_check_breaker():
    ep = ep_for(3900, 17.6)
    t = recovery_sms(ep, station=ST, time="14:03:15", now_w=9, ended_ts=130, rated_a=16)
    check(t)
    assert "check the breaker" in t


def test_lost_data_warning():
    t = lost_sms(ep_for(2100, 9.2), station=ST, time="14:03:15")
    check(t)
    assert "no data during the overload" in t


def test_duration():
    assert duration(5) == "5 s" and duration(64) == "1 min 4 s" and duration(120) == "2 min"
    assert duration(3725) == "1 h 2 min"

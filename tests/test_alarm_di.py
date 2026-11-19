"""T4 / T8: the on/off (DI) alarm engine."""
from live.alarm_di import GREEN, GREY, RED, DiAlarmEngine
from live.settings import Settings


def eng(**kw):
    s = Settings(di_off_hold_s=3, di_on_hold_s=2, stale_after_s=10, **kw)
    return DiAlarmEngine(s), s


def feed(e, start, seconds, on, step=1.0):
    out, t = [], start
    while t < start + seconds:
        out += e.on_reading(t, on, t)
        t += step
    return out, t


def kinds(trs):
    return [t.kind for t in trs]


def test_first_on_goes_green_without_sms():
    e, _ = eng()
    trs, _ = feed(e, 0, 5, True)
    assert e.state == GREEN and kinds(trs) == ["display"]


def test_short_off_bounce_changes_nothing():
    e, _ = eng()
    feed(e, 0, 5, True)
    trs = e.on_reading(5, False, 5) + e.on_reading(6, True, 6) + e.on_reading(7, False, 7) + e.on_reading(8, True, 8)
    trs += feed(e, 9, 10, True)[0]
    assert trs == [] and e.state == GREEN


def test_held_off_gives_one_outage_then_one_restored_with_duration():
    e, _ = eng()
    feed(e, 0, 5, True)
    trs, _ = feed(e, 10, 20, False)
    assert kinds(trs) == ["outage"] and e.state == RED and e.supply_on is False
    assert trs[0].changed_at == 10                         # outage time = when the input went to 0
    trs, _ = feed(e, 30, 10, True)
    assert kinds(trs) == ["restored"] and e.state == GREEN
    assert trs[0].duration_s == 20                         # 10 -> 30 s
    assert trs[0].changed_at == 30


def test_on_bounce_during_outage_is_ignored():
    e, _ = eng()
    feed(e, 0, 5, True)
    feed(e, 5, 10, False)
    trs = e.on_reading(15, True, 15) + e.on_reading(16, False, 16)
    trs += feed(e, 17, 5, False)[0]
    assert trs == [] and e.state == RED


def test_silence_goes_grey_without_outage():
    e, _ = eng()
    feed(e, 0, 5, True)
    assert e.tick(10) == []
    trs = e.tick(15)
    assert kinds(trs) == ["display"] and e.state == GREY
    assert e.confirmed == GREEN                            # grey is not an outage
    trs = e.on_reading(20, True, 20)
    assert e.state == GREEN and kinds(trs) == ["display"]  # data back, same state: no SMS


def test_change_during_silence_reported_after_hold():
    e, _ = eng()
    feed(e, 0, 5, True)
    e.tick(20)
    assert e.state == GREY
    trs = e.on_reading(30, False, 30)
    assert trs == [] and e.state == GREY                   # hold not reached yet
    trs, _ = feed(e, 31, 3, False)
    assert kinds(trs) == ["outage"] and e.state == RED


def test_restart_with_persisted_red_and_retained_off_sends_nothing():
    s = Settings(di_off_hold_s=3, di_on_hold_s=2, stale_after_s=10)
    e = DiAlarmEngine(s, confirmed=RED, confirmed_since=100, outage_started=100)
    trs, _ = feed(e, 500, 10, False)
    assert e.state == RED and kinds(trs) == ["display"]    # back to red at once, no second outage SMS
    trs, _ = feed(e, 510, 3, True)
    assert kinds(trs) == ["restored"] and trs[0].duration_s == 410


def test_late_readings_ignored():
    e, _ = eng()
    feed(e, 0, 5, True)
    assert e.on_reading(6, False, 6, late=True) == []
    assert e.on_reading(20, False, 20, late=True) == []


def test_no_data_sms_once_when_enabled():
    e, _ = eng(sms_on_no_data=True, no_data_sms_after_s=120)
    feed(e, 0, 5, True)
    e.tick(20)
    assert e.tick(100) == []
    trs = e.tick(141)
    assert kinds(trs) == ["nodata"]
    assert e.tick(300) == []                               # only once per grey period


def test_no_data_sms_off_by_default():
    e, _ = eng()
    feed(e, 0, 5, True)
    e.tick(20)
    assert e.tick(1000) == []


def test_reset_forgets_outage():
    e, _ = eng()
    feed(e, 0, 5, True)
    feed(e, 5, 5, False)
    tr = e.reset(20)
    assert tr.kind == "display" and e.state == GREEN and e.outage_started is None


def test_holds_read_live_from_settings():
    e, s = eng()
    feed(e, 0, 5, True)
    s.di_off_hold_s = 10
    trs, _ = feed(e, 5, 6, False)
    assert trs == [] and e.state == GREEN

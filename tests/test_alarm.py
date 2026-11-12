"""ALM-1 / ALM-2 / ALM-4: the alarm state machine (§6) and test plan T1-T5."""
import pytest

from live.alarm import AMBER, GREEN, GREY, RED, AlarmEngine
from live.settings import Thresholds


def engine(**kw):
    return AlarmEngine(Thresholds(**kw), stale_after_s=15)


def feed(e, start, seconds, power, step=1.0, late=False):
    trs = []
    t = start
    while t < start + seconds:
        trs += e.on_reading(t, power, t, late)
        t += step
    return trs, t


def test_first_reading_leaves_grey():
    e = engine()
    trs = e.on_reading(0, 40, 0)
    assert e.state == GREEN
    assert trs[0].from_state == GREY and trs[0].to_state == GREEN


def test_lamp_only_stays_green_t1():
    e = engine()
    trs, _ = feed(e, 0, 3600, 40)
    assert e.state == GREEN
    assert not any(t.to_state == RED for t in trs)


def test_red_needs_hold_time_t3():
    e = engine()
    feed(e, 0, 30, 40)
    e.on_reading(30, 2100, 30)
    assert e.state == AMBER           # above warn, hold not reached yet
    e.on_reading(31, 2100, 31)
    assert e.state == AMBER
    trs = e.on_reading(32, 2100, 32)  # 2 s held
    assert e.state == RED
    assert trs[-1].entered_red
    assert trs[-1].power_w == 2100


def test_red_clears_after_clear_time_with_peak():
    e = engine()
    feed(e, 0, 30, 40)
    feed(e, 30, 10, 2100)
    e.on_reading(40, 2300, 40)
    assert e.state == RED
    for t in range(41, 46):
        e.on_reading(t, 40, t)
        assert e.state == RED         # below clear level, but not for 5 s yet
    trs = e.on_reading(46, 40, 46)
    assert e.state == GREEN
    assert trs[-1].left_red and trs[-1].peak_w == 2300


def test_red_stays_while_extra_load_remains_unusual():
    e = engine(clear_w=700)
    feed(e, 0, 30, 40)
    feed(e, 30, 5, 2100)
    assert e.state == RED
    feed(e, 35, 30, 650)              # still far above the usual 40 W: stays red, no flapping
    assert e.state == RED


def test_red_clears_to_amber_when_above_warn_without_history():
    e = engine(clear_w=700)
    e.on_reading(0, 2100, 0)          # no history: only the absolute limit applies
    feed(e, 1, 4, 2100)
    assert e.state == RED
    feed(e, 5, 7, 650)                # below min(700, 0.8 x 1000)
    assert e.state == AMBER


def test_flicker_does_not_toggle_red_t4():
    """Hair dryer on/off every second for 20 s: no RED (hold is never reached)."""
    e = engine()
    feed(e, 0, 30, 40)
    reds = 0
    for i in range(20):
        p = 2100 if i % 2 == 0 else 40
        reds += sum(t.entered_red for t in e.on_reading(30 + i, p, 30 + i))
    assert reds == 0


def test_flicker_while_red_stays_one_episode():
    e = engine()
    feed(e, 0, 30, 40)
    feed(e, 30, 5, 2100)
    assert e.state == RED
    trs = []
    for i in range(20):
        p = 2100 if i % 2 == 0 else 40
        trs += e.on_reading(35 + i, p, 35 + i)
    assert e.state == RED
    assert not any(t.left_red or t.entered_red for t in trs)


def test_led_switching_on_and_off_stays_green():
    e = engine()
    reds = 0
    for t in range(300):
        power = 9.0 if (t // 5) % 2 == 0 else 0.0     # LED on 5 s, off 5 s
        reds += sum(x.to_state in (RED, AMBER) for x in e.on_reading(t, power, t))
    assert reds == 0 and e.state == GREEN


def test_hair_dryer_low_with_led_on_is_red():
    e = engine()
    feed(e, 0, 60, 9)
    trs, _ = feed(e, 60, 5, 900)
    assert e.state == RED
    red = next(t for t in trs if t.entered_red)
    assert red.usual_w == 9


def test_hair_dryer_low_with_led_off_is_red():
    """Usual level 0 W must not switch the 'sudden jump' rule off."""
    e = engine()
    feed(e, 0, 60, 0.0)
    feed(e, 60, 5, 900)
    assert e.state == RED


@pytest.mark.parametrize("extra", [60, 150, 400, 900, 2100])
def test_any_extra_device_is_red_while_on_then_clears(extra):
    """Not only a hair dryer: any device that makes power jump above the LED is unusual."""
    e = engine()
    feed(e, 0, 60, 9)
    trs, _ = feed(e, 60, 120, 9 + extra)
    assert e.state == RED
    assert sum(t.entered_red for t in trs) == 1          # one episode, one SMS, no flapping
    feed(e, 180, 10, 9)
    assert e.state == GREEN


def test_small_device_below_floor_is_ignored():
    e = engine()
    feed(e, 0, 60, 9)
    trs, _ = feed(e, 60, 120, 9 + 20)                    # phone charger
    assert e.state == GREEN and not trs


def test_load_already_on_at_start_is_not_a_jump():
    e = engine()
    trs, _ = feed(e, 0, 30, 120)                         # e.g. a 120 W lamp as the normal load
    assert e.state == GREEN and not any(t.to_state == RED for t in trs)


def test_hair_dryer_low_amber_only_if_floor_raised():
    e = engine(min_alarm_w=1000)      # floor at the alarm level = only the absolute limit applies
    feed(e, 0, 60, 9)
    trs, _ = feed(e, 60, 20, 900)
    assert e.state == AMBER
    assert not any(t.to_state == RED for t in trs)


def test_baseline_rule_alarms_above_usual_level_alm2():
    e = engine(alarm_w=5000, warn_w=4000, min_alarm_w=300, baseline_multiplier=3)
    feed(e, 0, 60, 100)               # usual level 100 W
    feed(e, 60, 3, 350)               # >= 3x baseline and >= floor, below absolute alarm
    assert e.state == RED


def test_baseline_rule_respects_floor():
    e = engine(alarm_w=5000, warn_w=4000, min_alarm_w=300, baseline_multiplier=3)
    feed(e, 0, 60, 40)
    feed(e, 60, 10, 200)              # 5x baseline but below the 300 W floor
    assert e.state == GREEN


def test_late_readings_never_change_state_t5():
    e = engine()
    feed(e, 0, 10, 40)
    trs, _ = feed(e, 10, 20, 2100, late=True)
    assert trs == [] and e.state == GREEN


def test_stale_goes_grey_and_recovers_alm4():
    e = engine()
    feed(e, 0, 10, 40)
    assert e.tick(20) is None
    tr = e.tick(25)                   # last fresh reading at t=9, 16 s ago
    assert tr and tr.to_state == GREY and e.state == GREY
    trs = e.on_reading(30, 40, 30)
    assert e.state == GREEN and trs[0].from_state == GREY


def test_grey_recovery_does_not_jump_straight_to_red():
    e = engine()
    feed(e, 0, 10, 40)
    e.tick(30)
    assert e.state == GREY
    e.on_reading(31, 2100, 31)
    assert e.state == AMBER           # hold timer starts fresh after GREY
    e.on_reading(33, 2100, 33)
    assert e.state == RED


def test_grey_from_red_reports_peak():
    e = engine()
    feed(e, 0, 30, 40)
    feed(e, 30, 5, 2100)
    tr = e.tick(60)
    assert tr.from_state == RED and tr.to_state == GREY and tr.peak_w == 2100


def test_reset_forces_green():
    e = engine()
    feed(e, 0, 30, 40)
    feed(e, 30, 5, 2100)
    tr = e.reset(40)
    assert e.state == GREEN and tr.left_red


def test_runtime_threshold_override_is_used():
    t = Thresholds()
    e = AlarmEngine(t)
    feed(e, 0, 10, 40)
    t.alarm_w = 50
    t.min_alarm_w = 50
    feed(e, 10, 3, 60)
    assert e.state == RED

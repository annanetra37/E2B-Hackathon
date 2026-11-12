"""ING-2 / ING-3: parsers and normalisation (§5)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live.parser import normalize, parse

FIELDS = {"voltageV": "voltage", "currentA": "current", "powerW": "power", "pf": "pf",
          "freqHz": "freq", "energyKwh": "energy", "ts": "ts"}
NOW = datetime(2026, 11, 20, 10, 2, 11, 410000, tzinfo=timezone.utc)
SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "payload-samples"


def p(doc, fmt="fourfaith", fields=FIELDS, **kw):
    return parse(json.dumps(doc) if not isinstance(doc, (str, bytes)) else doc, fmt=fmt, fields=fields,
                 station_id="YVN-DEMO-01", received_at=NOW, **kw)


def test_illustrative_shape():
    r = p({"ts": int(NOW.timestamp()), "device": "demo_meter",
           "values": {"voltage": 229.8, "current": 9.21, "power": 2104.5, "pf": 0.99, "freq": 50.0, "energy": 12.413}})
    assert r["powerW"] == 2104.5 and r["voltageV"] == 229.8 and r["energyKwh"] == 12.413
    assert r["stationId"] == "YVN-DEMO-01" and r["late"] is False and r["source"] == "live"
    assert r["receivedAt"] == "2026-11-20T10:02:11.410Z"


def test_name_value_list_and_string_numbers():
    r = p({"sn": "IWR405-1", "time": "2026-11-20 10:02:10",
           "data": [{"name": "demo_meter_voltage", "value": "230.1"},
                    {"name": "demo_meter_power", "value": "41.5"},
                    {"name": "demo_meter_current", "value": "0.19"}]})
    assert r["voltageV"] == 230.1 and r["powerW"] == 41.5 and r["currentA"] == 0.19
    assert r["ts"] == "2026-11-20T10:02:10.000Z"


def test_generic_json_dotted_paths():
    fields = {**FIELDS, "powerW": "m.p", "voltageV": "m.v"}
    r = p({"m": {"p": 900, "v": 231}}, fmt="generic_json", fields=fields)
    assert r["powerW"] == 900 and r["voltageV"] == 231


def test_power_computed_when_missing():
    r = p({"voltage": 230, "current": 4, "pf": 0.9})
    assert r["powerW"] == pytest.approx(828.0)


def test_out_of_range_field_dropped_not_crashing():
    r = p({"voltage": 999, "power": 50, "current": "abc"})
    assert r["voltageV"] is None and r["currentA"] is None and r["powerW"] == 50


def test_garbage_returns_none():
    assert p("not json at all") is None
    assert p(b"\xff\xfe") is None
    assert p({"hello": "world"}) is None
    assert p([1, 2, 3]) is None


def test_late_flag_for_cached_backfill():
    old = NOW - timedelta(seconds=30)
    r = p({"ts": old.timestamp(), "power": 2000}, late_after_s=10)
    assert r["late"] is True


def test_millisecond_epoch():
    r = p({"ts": int(NOW.timestamp() * 1000) - 500, "power": 40})
    assert r["late"] is False and r["ts"].startswith("2026-11-20T10:02:10")


def test_gateway_clock_ahead_falls_back_to_receive_time():
    r = p({"ts": (NOW + timedelta(hours=1)).timestamp(), "power": 40})
    assert r["ts"] == r["receivedAt"]


def test_device_time_off_uses_receive_time():
    old = NOW - timedelta(seconds=60)
    r = p({"ts": old.timestamp(), "power": 40}, device_time=False)
    assert r["late"] is False and r["ts"] == r["receivedAt"]


def test_normalize_all_fields_missing():
    assert normalize({}, station_id="x", received_at=NOW, late_after_s=10) is None


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.json")))
def test_committed_samples_parse(path):
    """Every captured sample in docs/payload-samples/ must yield a power value (HW-5 -> ING-2)."""
    doc = json.loads(path.read_text())
    payloads = doc["messages"] if isinstance(doc, dict) and "messages" in doc else [doc]
    for msg in payloads:
        body = msg.get("payload", msg) if isinstance(msg, dict) else msg
        r = p(body)
        assert r is not None and r["powerW"] is not None, f"{path.name}: {body}"

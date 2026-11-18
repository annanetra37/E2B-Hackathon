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


@pytest.mark.parametrize("path", sorted(p for p in SAMPLES.glob("*.json") if not p.name.startswith("di-")))
def test_committed_samples_parse(path):
    """Every captured sample in docs/payload-samples/ must yield a power value (HW-5 -> ING-2)."""
    doc = json.loads(path.read_text())
    payloads = doc["messages"] if isinstance(doc, dict) and "messages" in doc else [doc]
    for msg in payloads:
        body = msg.get("payload", msg) if isinstance(msg, dict) else msg
        r = p(body)
        assert r is not None and r["powerW"] is not None, f"{path.name}: {body}"



# ---------------------------------------------------------------- on/off (DI) mode (T3)
from live.parser import parse_di, parse_status  # noqa: E402


def d(doc, **kw):
    return parse_di(json.dumps(doc) if not isinstance(doc, (str, bytes)) else doc, field="di1",
                    station_id="YVN-DEMO-01", received_at=NOW, **kw)


@pytest.mark.parametrize("value,on", [(1, True), (0, False), ("1", True), ("0", False), (True, True),
                                      (False, False), ("on", True), ("off", False), ("ON", True), ("Off", False),
                                      ("true", True), ("false", False)])
def test_di_accepted_values(value, on):
    r = d({"station": "YVN-DEMO-01", "di1": value, "ts": NOW.isoformat()})
    assert r["supplyOn"] is on and r["powerW"] is None and r["voltageV"] is None


@pytest.mark.parametrize("doc", [{"di1": 2}, {"di1": "maybe"}, {"di1": None}, {"power": 40}, {"di1": [1]},
                                 [1, 0], "not json", {"station": "OTHER-01", "di1": 1}])
def test_di_rejected(doc):
    assert d(doc) is None


def test_di_message_without_power_is_kept():
    r = d({"di1": 1})
    assert r is not None and r["supplyOn"] is True and r["ts"] == r["receivedAt"]


def test_di_contract_fields_and_late():
    old = (NOW - timedelta(seconds=30)).isoformat()
    r = d({"station": "YVN-DEMO-01", "di1": 0, "level": "HIGH", "ts": old, "seq": 1842, "src": "di-bridge"})
    assert r["supplyOn"] is False and r["level"] == "HIGH" and r["seq"] == 1842 and r["late"] is True


def test_di_custom_field_path():
    r = parse_di(json.dumps({"inputs": {"d1": "on"}}), field="inputs.d1", station_id="X", received_at=NOW)
    assert r["supplyOn"] is True


def test_status_messages():
    assert parse_status('{"station":"YVN-DEMO-01","bridge":"online"}') is True
    assert parse_status(b'{"bridge":"offline"}') is False
    assert parse_status('{"bridge":"?"}') is None and parse_status("garbage") is None


def test_di_samples_file():
    doc = json.loads((SAMPLES / "di-switch.json").read_text())
    values = [d(m["payload"])["supplyOn"] for m in doc["messages"]]
    assert values.count(True) >= 10 and values.count(False) >= 10
    for m in doc["status_messages"]:
        assert parse_status(json.dumps(m["payload"])) is not None

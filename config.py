"""
Shared settings: reads substations.yaml once, so the simulator, the gateway and
the web portal all use the SAME phone book.
"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
CONFIG_FILE = Path(os.environ.get("SUBSTATIONS_FILE", ROOT / "substations.yaml"))


def load() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


def db_path(cfg: dict) -> Path:
    """Where the database lives. On Railway set GATEWAY_DB=/data/gateway.db (a volume)."""
    return Path(os.environ.get("GATEWAY_DB") or ROOT / cfg.get("database", "data/gateway.db"))


def modbus_devices(cfg: dict, sub: dict) -> list[dict]:
    """All Modbus devices inside one substation, built from its standard layout."""
    tpl = cfg["templates"]
    if sub["type"] == "primary":
        lay = cfg["layout"]["primary"]
        devices = [
            {"key": "line_meter", "unit_id": lay["line_meter_unit"], "points": tpl["meter"]},
            {"key": "transformer", "unit_id": lay["transformer_unit"], "points": tpl["transformer"]},
            {"key": "bus_meter", "unit_id": lay["bus_meter_unit"], "points": tpl["meter"]},
        ]
        for i, _ in enumerate(sub["feeders"]):
            devices.append({"key": f"feeder_{i + 1}", "unit_id": lay["feeder_meter_unit_start"] + i,
                            "points": tpl["meter"]})
        return devices

    lay = cfg["layout"]["distribution"]
    devices = [
        {"key": "lv_meter", "unit_id": lay["lv_meter_unit"], "points": tpl["lv_meter"]},
        {"key": "temp_sensor", "unit_id": lay["temp_sensor_unit"], "points": tpl["temp_sensor"]},
        {"key": "di_module", "unit_id": lay["di_module_unit"], "points": tpl["di_module"]},
    ]
    for i, line in enumerate(sub["lines"]):
        if line.get("measured"):
            devices.append({"key": f"ct_{i + 1}", "unit_id": lay["ct_meter_unit_start"] + i,
                            "points": tpl["ct_meter"]})
    return devices


def iec104_points(cfg: dict, sub: dict) -> list[dict]:
    """All IEC 104 points of a primary substation's RTU (distribution stations have none)."""
    if sub["type"] != "primary" or "iec104" not in sub:
        return []
    lay = cfg["layout"]["primary"]
    pts = [
        {"key": "frequency", "ioa": lay["ioa_frequency"], "type": "float"},
        {"key": "Q0", "ioa": lay["ioa_line_breaker"], "type": "single"},
        {"key": "Q1", "ioa": lay["ioa_transformer_breaker"], "type": "single"},
    ]
    for i, _ in enumerate(sub["feeders"]):
        pts.append({"key": f"QF{i + 1}", "ioa": lay["ioa_feeder_breaker_start"] + i, "type": "single"})
    return pts

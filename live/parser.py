"""
Payload parsing and normalisation (ING-2, ING-3).

The IWR405 manual does not document the MQTT topic or the JSON layout, so the
parsers are deliberately tolerant. Both turn a raw MQTT message into the
normalised reading of the data contract (§5.2):

  {stationId, ts, receivedAt, voltageV, currentA, powerW, pf, freqHz, energyKwh, late, source}

PAYLOAD_FORMAT selects the parser:
  generic_json  FIELD_* are exact JSON keys or dotted paths ("values.power", "data.0.value").
  fourfaith     walks the whole document and collects every name -> value pair it can find:
                flat keys, nested objects, and lists of {"name"/"id"/"key"/"flag": ..., "value"/"val": ...}.
                Names are matched to FIELD_* case-insensitively, also when the gateway prefixes
                them with the child-device name ("demo_meter_power", "demo_meter.power").

Malformed input never raises: parse() returns None and logs a warning.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("live.parser")

FIELDS = ("voltageV", "currentA", "powerW", "pf", "freqHz", "energyKwh")
RANGES = {"voltageV": (0, 300), "currentA": (0, 60), "powerW": (0, 15000),
          "pf": (-1, 1), "freqHz": (40, 70), "energyKwh": (0, 1e9)}
NAME_KEYS = ("name", "id", "key", "flag", "tag", "variable", "var", "field")
VALUE_KEYS = ("value", "val", "v", "data", "reading")


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO string with milliseconds, e.g. 2026-11-20T10:02:11.120Z."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip().replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def parse_ts(v: Any) -> datetime | None:
    """Epoch seconds / milliseconds, or an ISO / 'YYYY-MM-DD HH:MM:SS' string. Naive strings = UTC."""
    n = to_number(v)
    if n is not None:
        if n > 1e14:          # microseconds
            n /= 1e6
        elif n > 1e11:        # milliseconds
            n /= 1e3
        if n < 1e9:           # not a plausible epoch (before 2001)
            return None
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v.strip():
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


# ------------------------------------------------------------------ flattening helpers

def _walk(obj: Any, prefix: str, out: dict[str, Any]) -> None:
    """Collect name -> value pairs from any nesting of dicts and lists."""
    if isinstance(obj, dict):
        name = next((obj[k] for k in NAME_KEYS if isinstance(obj.get(k), str)), None)
        value_key = next((k for k in VALUE_KEYS if k in obj and not isinstance(obj[k], (dict, list))), None)
        if name is not None and value_key is not None:
            out.setdefault(name, obj[value_key])
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                _walk(v, path, out)
            else:
                out.setdefault(path, v)
                out.setdefault(str(k), v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{prefix}.{i}" if prefix else str(i), out)


def flatten(doc: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _walk(doc, "", out)
    return out


def _dotted(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _lookup_fuzzy(flat: dict[str, Any], wanted: str) -> Any:
    w = wanted.lower()
    lowered = {k.lower(): v for k, v in flat.items()}
    if w in lowered:
        return lowered[w]
    for k, v in lowered.items():          # device-prefixed: demo_meter_power, demo_meter.power, meter/power
        if k.endswith(("_" + w, "." + w, "/" + w, "-" + w, ":" + w)):
            return v
    return None


# ------------------------------------------------------------------ parsers -> raw field dict

def parse_generic_json(doc: Any, fields: dict[str, str]) -> dict[str, Any]:
    out = {}
    for key, path in fields.items():
        if not path:
            continue
        v = _dotted(doc, path)
        if v is None and isinstance(doc, dict):
            v = doc.get(path)
        out[key] = v
    return out


def parse_fourfaith(doc: Any, fields: dict[str, str]) -> dict[str, Any]:
    flat = flatten(doc)
    out = {}
    for key, name in fields.items():
        if not name:
            continue
        v = _dotted(doc, name) if "." in name else None
        if v is None or isinstance(v, (dict, list)):
            v = _lookup_fuzzy(flat, name)
        if key == "ts" and v is None:
            for alt in ("timestamp", "time", "ts", "datetime", "date"):
                v = _lookup_fuzzy(flat, alt)
                if v is not None:
                    break
        out[key] = v
    return out


PARSERS: dict[str, Callable[[Any, dict], dict]] = {
    "generic_json": parse_generic_json,
    "fourfaith": parse_fourfaith,
}


# ------------------------------------------------------------------ normalisation

def normalize(raw: dict[str, Any], *, station_id: str, received_at: datetime, late_after_s: float,
              device_time: bool = True, source: str = "live") -> dict[str, Any] | None:
    """Validate ranges, compute power if missing, set ts / late. Returns None if nothing usable."""
    r: dict[str, Any] = {"stationId": station_id}
    for key in FIELDS:
        if key not in raw or raw[key] is None:
            r[key] = None
            continue
        n = to_number(raw[key])
        lo, hi = RANGES[key]
        if n is None or not (lo <= n <= hi):
            log.warning("dropping %s=%r (non-numeric or outside %s..%s)", key, raw[key], lo, hi)
            r[key] = None
        else:
            r[key] = n
    if r["powerW"] is None and None not in (r["voltageV"], r["currentA"], r["pf"]):
        p = r["voltageV"] * r["currentA"] * abs(r["pf"])
        r["powerW"] = round(p, 1) if 0 <= p <= RANGES["powerW"][1] else None
    if all(r[k] is None for k in FIELDS):
        return None

    ts = parse_ts(raw.get("ts")) if device_time else None
    if ts is None or (ts - received_at).total_seconds() > 5:
        ts = received_at                 # missing, or gateway clock ahead of ours: trust receive time
    r["ts"] = iso(ts)
    r["receivedAt"] = iso(received_at)
    r["late"] = (received_at - ts).total_seconds() > late_after_s
    r["source"] = source
    return r


def parse(payload: bytes | str, *, fmt: str, fields: dict[str, str], station_id: str,
          received_at: datetime | None = None, late_after_s: float = 10,
          device_time: bool = True) -> dict[str, Any] | None:
    """Raw MQTT payload -> normalised reading, or None (logged) if unusable."""
    received_at = received_at or datetime.now(timezone.utc)
    try:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else payload
        doc = json.loads(text)
    except (ValueError, TypeError) as e:
        log.warning("payload is not JSON (%s): %.200r", e, payload)
        return None
    parser = PARSERS.get(fmt)
    if parser is None:
        log.warning("unknown PAYLOAD_FORMAT %r, using fourfaith", fmt)
        parser = parse_fourfaith
    try:
        raw = parser(doc, fields)
        reading = normalize(raw, station_id=station_id, received_at=received_at,
                            late_after_s=late_after_s, device_time=device_time)
    except Exception:                    # never crash the ingestor on a strange payload
        log.exception("parser failed on payload %.200r", text)
        return None
    if reading is None:
        log.warning("no known fields in payload %.200r (check PAYLOAD_FORMAT / FIELD_*)", text)
    return reading

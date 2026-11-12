"""
Storage (ING-4, §5.3): readings, events, demo_subscribers, sms_log.

Postgres when DATABASE_URL is set (Railway: ${{Postgres.DATABASE_URL}}), otherwise a
SQLite file (local development, or Railway without Postgres). The SQL is shared; the
only differences are the DDL and the placeholder style, handled here.

All methods are blocking; the service calls them through asyncio.to_thread.
Timestamps go in and come out as aware UTC datetimes.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .parser import iso

log = logging.getLogger("live.store")

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id BIGSERIAL PRIMARY KEY, station_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  voltage_v REAL, current_a REAL, power_w REAL, pf REAL, freq_hz REAL,
  energy_kwh DOUBLE PRECISION, late BOOLEAN DEFAULT false,
  source TEXT DEFAULT 'live', raw JSONB);
CREATE INDEX IF NOT EXISTS readings_station_ts ON readings (station_id, ts DESC);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY, station_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  from_state TEXT, to_state TEXT, peak_power_w REAL, message TEXT, sms_sent INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_station_ts ON events (station_id, ts DESC);
CREATE TABLE IF NOT EXISTS demo_subscribers (
  id BIGSERIAL PRIMARY KEY, phone_e164 TEXT UNIQUE NOT NULL, lang TEXT DEFAULT 'en',
  consent_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
  unsubscribe_token TEXT UNIQUE NOT NULL, sms_count INT DEFAULT 0,
  last_sms_at TIMESTAMPTZ, unsubscribed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS sms_log (
  id BIGSERIAL PRIMARY KEY, subscriber_id BIGINT, event_id BIGINT, provider TEXT,
  status TEXT, provider_msg_id TEXT, error TEXT, ts TIMESTAMPTZ DEFAULT now());
ALTER TABLE sms_log ADD COLUMN IF NOT EXISTS phone_hash TEXT;
ALTER TABLE sms_log ADD COLUMN IF NOT EXISTS kind TEXT;
CREATE INDEX IF NOT EXISTS sms_log_phone_ts ON sms_log (phone_hash, ts);
CREATE INDEX IF NOT EXISTS sms_log_ts ON sms_log (ts);
"""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, ts TEXT NOT NULL,
  received_at TEXT NOT NULL, voltage_v REAL, current_a REAL, power_w REAL, pf REAL, freq_hz REAL,
  energy_kwh REAL, late INTEGER DEFAULT 0, source TEXT DEFAULT 'live', raw TEXT);
CREATE INDEX IF NOT EXISTS readings_station_ts ON readings (station_id, ts DESC);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, ts TEXT NOT NULL,
  from_state TEXT, to_state TEXT, peak_power_w REAL, message TEXT, sms_sent INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_station_ts ON events (station_id, ts DESC);
CREATE TABLE IF NOT EXISTS demo_subscribers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, phone_e164 TEXT UNIQUE NOT NULL, lang TEXT DEFAULT 'en',
  consent_at TEXT NOT NULL, expires_at TEXT NOT NULL, unsubscribe_token TEXT UNIQUE NOT NULL,
  sms_count INTEGER DEFAULT 0, last_sms_at TEXT, unsubscribed_at TEXT);
CREATE TABLE IF NOT EXISTS sms_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id INTEGER, event_id INTEGER, provider TEXT,
  status TEXT, provider_msg_id TEXT, error TEXT, ts TEXT, phone_hash TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS sms_log_phone_ts ON sms_log (phone_hash, ts);
CREATE INDEX IF NOT EXISTS sms_log_ts ON sms_log (ts);
"""

TS_COLS = {"ts", "received_at", "consent_at", "expires_at", "last_sms_at", "unsubscribed_at"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_db(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v.astimezone(timezone.utc) if isinstance(v, datetime) else None
    s = str(v).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, database_url: str = "", sqlite_path: str = "data/live.db"):
        self.lock = threading.Lock()
        url = database_url.strip()
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg                       # only needed with Postgres
            self.kind = "postgres"
            self._url = url
            self.conn = psycopg.connect(url, autocommit=True)
        else:
            self.kind = "sqlite"
            self._url = ""
            if sqlite_path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)), exist_ok=True)
            self.conn = sqlite3.connect(sqlite_path, check_same_thread=False, isolation_level=None, timeout=10)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.row_factory = sqlite3.Row
        self.migrate()

    # ------------------------------------------------------------------ low level
    def _p(self, v: Any) -> Any:
        if isinstance(v, datetime):
            return v if self.kind == "postgres" else iso(v)
        if isinstance(v, bool) and self.kind == "sqlite":
            return int(v)
        return v

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def _reconnect(self) -> None:
        if self.kind == "postgres":
            import psycopg
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = psycopg.connect(self._url, autocommit=True)

    def execute(self, sql: str, params: tuple = (), fetch: str | None = None) -> Any:
        params = tuple(self._p(p) for p in params)
        with self.lock:
            for attempt in (1, 2):
                try:
                    cur = self.conn.cursor()
                    cur.execute(self._sql(sql), params)
                    if fetch == "all":
                        return self._rows(cur, cur.fetchall())
                    if fetch == "one":
                        row = cur.fetchone()
                        return self._rows(cur, [row])[0] if row is not None else None
                    return cur.rowcount
                except Exception as e:
                    if self.kind == "postgres" and attempt == 1 and "closed" in str(e).lower():
                        self._reconnect()
                        continue
                    raise

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        rows = [tuple(self._p(p) for p in r) for r in rows]
        with self.lock:
            cur = self.conn.cursor()
            cur.executemany(self._sql(sql), rows)

    def _rows(self, cur, rows) -> list[dict]:
        cols = [d[0] for d in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, tuple(r)))
            for c in TS_COLS & d.keys():
                d[c] = _from_db(d[c])
            if "late" in d and d["late"] is not None:
                d["late"] = bool(d["late"])
            out.append(d)
        return out

    def migrate(self) -> None:
        schema = PG_SCHEMA if self.kind == "postgres" else SQLITE_SCHEMA
        with self.lock:
            cur = self.conn.cursor()
            for stmt in (s.strip() for s in schema.split(";")):
                if stmt:
                    cur.execute(stmt)

    def ping(self) -> bool:
        try:
            self.execute("SELECT 1", fetch="one")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ readings
    def insert_readings(self, readings: list[dict]) -> None:
        self.executemany(
            "INSERT INTO readings (station_id, ts, received_at, voltage_v, current_a, power_w, pf, freq_hz,"
            " energy_kwh, late, source, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["stationId"], _from_db(r["ts"]), _from_db(r["receivedAt"]), r.get("voltageV"), r.get("currentA"),
              r.get("powerW"), r.get("pf"), r.get("freqHz"), r.get("energyKwh"), bool(r.get("late")),
              r.get("source", "live"), json.dumps(r.get("raw")) if r.get("raw") is not None else None)
             for r in readings])

    def history(self, station_id: str, seconds: float) -> list[dict]:
        since = utcnow() - timedelta(seconds=seconds)
        return self.execute(
            "SELECT ts, voltage_v, current_a, power_w, pf, freq_hz, energy_kwh, late, source FROM readings"
            " WHERE station_id=? AND ts>=? ORDER BY ts", (station_id, since), fetch="all")

    def last_reading(self, station_id: str) -> dict | None:
        return self.execute(
            "SELECT ts, received_at, voltage_v, current_a, power_w, pf, freq_hz, energy_kwh, late, source"
            " FROM readings WHERE station_id=? AND late=? ORDER BY ts DESC LIMIT 1",
            (station_id, False), fetch="one")

    def delete_old_readings(self, days: float) -> int:
        return self.execute("DELETE FROM readings WHERE ts < ?", (utcnow() - timedelta(days=days),))

    # ------------------------------------------------------------------ events
    def insert_event(self, station_id: str, ts: datetime, from_state: str, to_state: str,
                     message: str, peak: float | None = None) -> int:
        row = self.execute(
            "INSERT INTO events (station_id, ts, from_state, to_state, peak_power_w, message, sms_sent)"
            " VALUES (?,?,?,?,?,?,0) RETURNING id", (station_id, ts, from_state, to_state, peak, message), fetch="one")
        return int(row["id"])

    def update_event(self, event_id: int, *, peak: float | None = None, sms_sent: int | None = None) -> None:
        if peak is not None:
            self.execute("UPDATE events SET peak_power_w=? WHERE id=?", (peak, event_id))
        if sms_sent is not None:
            self.execute("UPDATE events SET sms_sent=? WHERE id=?", (sms_sent, event_id))

    def events(self, station_id: str, limit: int = 10) -> list[dict]:
        return self.execute(
            "SELECT id, ts, from_state, to_state, peak_power_w, message, sms_sent FROM events"
            " WHERE station_id=? ORDER BY ts DESC, id DESC LIMIT ?", (station_id, limit), fetch="all")

    def get_event(self, event_id: int) -> dict | None:
        return self.execute("SELECT id, ts, from_state, to_state, peak_power_w, message, sms_sent FROM events"
                            " WHERE id=?", (event_id,), fetch="one")

    # ------------------------------------------------------------------ subscribers
    def upsert_subscriber(self, phone: str, lang: str, ttl_hours: float, token: str) -> tuple[dict, bool]:
        """Create, or refresh consent + expiry for an existing number. Returns (row, created)."""
        now = utcnow()
        exp = now + timedelta(hours=ttl_hours)
        existing = self.execute("SELECT * FROM demo_subscribers WHERE phone_e164=?", (phone,), fetch="one")
        if existing:
            self.execute("UPDATE demo_subscribers SET lang=?, consent_at=?, expires_at=?, unsubscribed_at=NULL"
                         " WHERE id=?", (lang, now, exp, existing["id"]))
            row = self.execute("SELECT * FROM demo_subscribers WHERE id=?", (existing["id"],), fetch="one")
            return row, False
        row = self.execute(
            "INSERT INTO demo_subscribers (phone_e164, lang, consent_at, expires_at, unsubscribe_token)"
            " VALUES (?,?,?,?,?) RETURNING *", (phone, lang, now, exp, token), fetch="one")
        return row, True

    def active_subscribers(self) -> list[dict]:
        return self.execute("SELECT * FROM demo_subscribers WHERE unsubscribed_at IS NULL AND expires_at > ?",
                            (utcnow(),), fetch="all")

    def count_active_subscribers(self) -> int:
        row = self.execute("SELECT COUNT(*) AS n FROM demo_subscribers WHERE unsubscribed_at IS NULL"
                           " AND expires_at > ?", (utcnow(),), fetch="one")
        return int(row["n"])

    def unsubscribe(self, token: str) -> bool:
        return self.execute("UPDATE demo_subscribers SET unsubscribed_at=? WHERE unsubscribe_token=?"
                            " AND unsubscribed_at IS NULL", (utcnow(), token)) > 0

    def mark_sms(self, subscriber_id: int) -> None:
        self.execute("UPDATE demo_subscribers SET sms_count=sms_count+1, last_sms_at=? WHERE id=?",
                     (utcnow(), subscriber_id))

    def delete_expired_subscribers(self) -> int:
        """TTL job: numbers are deleted (not just hidden) after SUBSCRIBER_TTL_HOURS, and
        unsubscribed numbers are deleted right away."""
        return self.execute("DELETE FROM demo_subscribers WHERE expires_at <= ? OR unsubscribed_at IS NOT NULL",
                            (utcnow(),))

    def delete_all_subscribers(self) -> int:
        return self.execute("DELETE FROM demo_subscribers")

    # ------------------------------------------------------------------ sms log
    def log_sms(self, *, subscriber_id: int | None, event_id: int | None, provider: str, status: str,
                provider_msg_id: str | None, error: str | None, phone_hash: str, kind: str) -> None:
        self.execute("INSERT INTO sms_log (subscriber_id, event_id, provider, status, provider_msg_id, error, ts,"
                     " phone_hash, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                     (subscriber_id, event_id, provider, status, provider_msg_id, error, utcnow(), phone_hash, kind))

    def sms_sent_since(self, since: datetime, phone_hash: str | None = None) -> int:
        if phone_hash is None:
            row = self.execute("SELECT COUNT(*) AS n FROM sms_log WHERE status='sent' AND ts>=?", (since,), fetch="one")
        else:
            row = self.execute("SELECT COUNT(*) AS n FROM sms_log WHERE status='sent' AND ts>=? AND phone_hash=?",
                               (since, phone_hash), fetch="one")
        return int(row["n"])

    def last_sms_to(self, phone_hash: str, kind: str | None = None) -> datetime | None:
        if kind:
            row = self.execute("SELECT MAX(ts) AS t FROM sms_log WHERE status='sent' AND phone_hash=? AND kind=?",
                               (phone_hash, kind), fetch="one")
        else:
            row = self.execute("SELECT MAX(ts) AS t FROM sms_log WHERE status='sent' AND phone_hash=?",
                               (phone_hash,), fetch="one")
        return _from_db(row["t"]) if row and row["t"] else None

    def recent_sms(self, limit: int = 20) -> list[dict]:
        return self.execute("SELECT id, event_id, provider, status, error, ts, kind FROM sms_log"
                            " ORDER BY id DESC LIMIT ?", (limit,), fetch="all")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

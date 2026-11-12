"""
Live demo settings: everything comes from environment variables (Railway "Variables" tab).
Nothing that appears in the variable table of the developer-tasks doc (§12.3) is hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


@dataclass
class Thresholds:
    """Alarm thresholds. Read from env at boot; the admin page can override them in memory."""
    warn_w: float = 600
    alarm_w: float = 1000
    min_alarm_w: float = 300
    hold_s: float = 2
    clear_w: float = 400
    clear_s: float = 5
    baseline_window_s: float = 60
    baseline_multiplier: float = 3

    @classmethod
    def from_env(cls) -> "Thresholds":
        return cls(
            warn_w=_float("POWER_WARN_W", 600),
            alarm_w=_float("POWER_ALARM_W", 1000),
            min_alarm_w=_float("POWER_MIN_ALARM_W", 300),
            hold_s=_float("ALARM_HOLD_SECONDS", 2),
            clear_w=_float("ALARM_CLEAR_W", 400),
            clear_s=_float("ALARM_CLEAR_SECONDS", 5),
            baseline_window_s=_float("BASELINE_WINDOW_SECONDS", 60),
            baseline_multiplier=_float("BASELINE_MULTIPLIER", 3),
        )

    def public(self) -> dict:
        return {"warnW": self.warn_w, "alarmW": self.alarm_w, "minAlarmW": self.min_alarm_w,
                "holdS": self.hold_s, "clearW": self.clear_w, "clearS": self.clear_s,
                "baselineWindowS": self.baseline_window_s, "baselineMultiplier": self.baseline_multiplier}


@dataclass
class Settings:
    enabled: bool = False
    source: str = "live"                       # live | simulator
    station_id: str = "YVN-DEMO-01"
    station_name: str = "Yerevan demo mini-substation"
    lat: float = 40.1777
    lng: float = 44.5126
    tz: str = "Asia/Yerevan"

    database_url: str = ""                     # postgres://... ; empty = SQLite file below
    sqlite_path: str = "data/live.db"

    mqtt_url: str = ""
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "e2b-railway-ingestor"
    mqtt_topic: str = "#"
    payload_format: str = "fourfaith"          # fourfaith | generic_json
    fields: dict = field(default_factory=dict)
    device_time: bool = True                   # use the gateway's timestamp when present

    thresholds: Thresholds = field(default_factory=Thresholds)
    stale_after_s: float = 15
    late_after_s: float = 10
    retention_days: float = 7

    sms_enabled: bool = True
    sms_provider: str = "console"              # console | twilio | android_gateway
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_from: str = ""
    smsgate_url: str = ""
    smsgate_username: str = ""
    smsgate_password: str = ""
    sms_template: str = "E2B ALERT: Sudden power spike at {station}: {powerW} W (usual {usualW} W, limit {alarmW} W). {time}"
    sms_welcome_template: str = "Welcome to the E2b live demo. Stop: {unsubUrl}"
    sms_recovery_template: str = "E2B: {station} back to normal ({powerW} W). {time}"
    sms_on_recovery: bool = False
    sms_always_notify: list = field(default_factory=list)
    sms_cooldown_s: float = 60
    sms_max_per_number: int = 5
    sms_max_per_hour: int = 200
    subscriber_ttl_hours: float = 24

    public_base_url: str = ""
    admin_token: str = ""
    live_public: bool = True                   # /live, /demo/join reachable without DASHBOARD_PASSWORD

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz or "UTC")
        except Exception:
            return ZoneInfo("UTC")

    @classmethod
    def from_env(cls) -> "Settings":
        root_db = os.environ.get("GATEWAY_DB")
        default_sqlite = (os.path.join(os.path.dirname(root_db), "live.db") if root_db else "data/live.db")
        return cls(
            enabled=_bool("DEMO_ENABLED", False),
            source=_str("DEMO_SOURCE", "live").lower() or "live",
            station_id=_str("DEMO_STATION_ID", "YVN-DEMO-01"),
            station_name=_str("DEMO_STATION_NAME", "Yerevan demo mini-substation"),
            lat=_float("DEMO_LAT", 40.1777),
            lng=_float("DEMO_LNG", 44.5126),
            tz=_str("TZ", "Asia/Yerevan") or "Asia/Yerevan",
            database_url=_str("DATABASE_URL"),
            sqlite_path=_str("LIVE_DB", default_sqlite),
            mqtt_url=_str("MQTT_URL"),
            mqtt_username=_str("MQTT_USERNAME"),
            mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
            mqtt_client_id=_str("MQTT_CLIENT_ID", "e2b-railway-ingestor"),
            mqtt_topic=_str("MQTT_TOPIC", "#") or "#",
            payload_format=_str("PAYLOAD_FORMAT", "fourfaith").lower(),
            fields={
                "voltageV": _str("FIELD_VOLTAGE", "voltage"),
                "currentA": _str("FIELD_CURRENT", "current"),
                "powerW": _str("FIELD_POWER", "power"),
                "pf": _str("FIELD_PF", "pf"),
                "freqHz": _str("FIELD_FREQ", "freq"),
                "energyKwh": _str("FIELD_ENERGY", "energy"),
                "ts": _str("FIELD_TS", "ts"),
            },
            device_time=_bool("DEVICE_TIME", True),
            thresholds=Thresholds.from_env(),
            stale_after_s=_float("STALE_AFTER_SECONDS", 15),
            late_after_s=_float("LATE_AFTER_SECONDS", 10),
            retention_days=_float("READINGS_RETENTION_DAYS", 7),
            sms_enabled=_bool("SMS_ENABLED", True),
            sms_provider=_str("SMS_PROVIDER", "console").lower(),
            twilio_account_sid=_str("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
            twilio_messaging_service_sid=_str("TWILIO_MESSAGING_SERVICE_SID"),
            twilio_from=_str("TWILIO_FROM"),
            smsgate_url=_str("SMSGATE_URL").rstrip("/"),
            smsgate_username=_str("SMSGATE_USERNAME"),
            smsgate_password=os.environ.get("SMSGATE_PASSWORD", ""),
            sms_template=_str("SMS_TEMPLATE", cls.sms_template) or cls.sms_template,
            sms_welcome_template=_str("SMS_WELCOME_TEMPLATE", cls.sms_welcome_template) or cls.sms_welcome_template,
            sms_recovery_template=_str("SMS_RECOVERY_TEMPLATE", cls.sms_recovery_template) or cls.sms_recovery_template,
            sms_on_recovery=_bool("SMS_ON_RECOVERY", False),
            sms_always_notify=[p.strip() for p in _str("SMS_ALWAYS_NOTIFY").split(",") if p.strip()],
            sms_cooldown_s=_float("SMS_COOLDOWN_SECONDS", 60),
            sms_max_per_number=_int("SMS_MAX_PER_NUMBER", 5),
            sms_max_per_hour=_int("SMS_MAX_PER_HOUR", 200),
            subscriber_ttl_hours=_float("SUBSCRIBER_TTL_HOURS", 24),
            public_base_url=_str("PUBLIC_BASE_URL").rstrip("/"),
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            live_public=_bool("LIVE_PUBLIC", True),
        )

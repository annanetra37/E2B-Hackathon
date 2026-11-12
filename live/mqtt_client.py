"""
Resilient MQTT subscriber (ING-1). paho-mqtt runs its own network thread with automatic
reconnect + backoff; every message is handed to the asyncio loop of the web app.

MQTT_URL:  mqtts://host:8883  (TLS, system CA bundle)   mqtt://host:1883  (no TLS, local tests only)
           wss://host:8884/mqtt (MQTT over WebSockets, TLS)
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Callable
from urllib.parse import unquote, urlparse

import paho.mqtt.client as mqtt

log = logging.getLogger("live.mqtt")


class MqttIngestor:
    def __init__(self, url: str, topic: str, client_id: str, username: str = "", password: str = "",
                 on_message: Callable[[str, bytes], None] | None = None,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.url, self.topic, self.client_id = url, topic, client_id
        self.username, self.password = username, password
        self.on_message_cb = on_message
        self.loop = loop
        self.connected = False
        self.connected_since: float | None = None
        self.last_message_at: float | None = None
        self.last_topic: str | None = None
        self.messages = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.client: mqtt.Client | None = None

    def start(self) -> None:
        u = urlparse(self.url)
        scheme = (u.scheme or "mqtts").lower()
        tls = scheme in ("mqtts", "ssl", "tls", "wss")
        transport = "websockets" if scheme in ("ws", "wss") else "tcp"
        port = u.port or {"mqtts": 8883, "ssl": 8883, "tls": 8883, "wss": 443, "ws": 80}.get(scheme, 1883)
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, transport=transport,
                        protocol=mqtt.MQTTv311, clean_session=True)
        user = self.username or (unquote(u.username) if u.username else "")
        pw = self.password or (unquote(u.password) if u.password else "")
        if user:
            c.username_pw_set(user, pw)
        if tls:
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if transport == "websockets":
            c.ws_set_options(path=u.path or "/mqtt")
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c
        log.info("connecting to MQTT %s://%s:%s topic=%s", scheme, u.hostname, port, self.topic)
        c.connect_async(u.hostname, port, keepalive=30)
        c.loop_start()

    def stop(self) -> None:
        if self.client:
            try:
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass

    # -------------------------------------------------- paho callbacks (network thread)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self.last_error = f"connect refused: {reason_code}"
            log.warning("MQTT %s", self.last_error)
            return
        self.connected = True
        self.connected_since = time.time()
        self.last_error = None
        client.subscribe(self.topic, qos=0)
        log.info("MQTT connected, subscribed to %s", self.topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if self.connected:
            self.reconnects += 1
        self.connected = False
        if reason_code is not None and getattr(reason_code, "is_failure", False):
            self.last_error = f"disconnected: {reason_code}"
            log.warning("MQTT %s (will reconnect)", self.last_error)

    def _on_message(self, client, userdata, msg):
        self.last_message_at = time.time()
        self.last_topic = msg.topic
        self.messages += 1
        if self.on_message_cb is None:
            return
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.on_message_cb, msg.topic, bytes(msg.payload))
        else:
            self.on_message_cb(msg.topic, bytes(msg.payload))

    def diag(self) -> dict:
        now = time.time()
        return {"configured": True, "connected": self.connected, "topic": self.topic,
                "lastTopic": self.last_topic, "messages": self.messages, "reconnects": self.reconnects,
                "lastMessageAgeS": round(now - self.last_message_at, 1) if self.last_message_at else None,
                "lastError": self.last_error}

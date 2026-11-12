"""SMS-1 / SMS-3 / SMS-4 / SMS-5: phone validation, caps, cooldown, providers, TTL."""
import asyncio
from datetime import timedelta

import httpx
import pytest

from live.notifier import (AndroidGatewayProvider, ConsoleProvider, Notifier, TwilioProvider, mask_phone,
                           normalize_phone)
from live.settings import Settings
from live.store import Store, utcnow


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup(tmp_path, db_url):
    s = Settings(sms_cooldown_s=60, sms_max_per_number=5, sms_max_per_hour=200, station_name="Demo")
    store = Store(db_url, str(tmp_path / "t.db"))
    prov = ConsoleProvider(s)
    return s, store, prov, Notifier(s, store, prov)


@pytest.mark.parametrize("raw,ok", [
    ("+374 77 123456", "+37477123456"), ("0037477123456", "+37477123456"), ("077123456", "+37477123456"),
    ("77123456", "+37477123456"), ("+1 (415) 555-0100", "+14155550100"),
    ("12", None), ("+374abc", None), ("", None), ("+0123456789", None),
])
def test_normalize_phone(raw, ok):
    assert normalize_phone(raw) == ok


def test_mask():
    assert mask_phone("+37477123456") == "+374 •• ••• 456"


def test_alert_fans_out_with_cooldown_t4(setup):
    s, store, prov, n = setup
    for i in range(3):
        store.upsert_subscriber(f"+3747712345{i}", "en", 24, f"tok{i}")
    assert run(n.alert(2134, event_id=1)) == 3
    assert "Sudden power spike at Demo: 2,134 W (usual 0 W, limit 1,000 W)" in prov.sent[0][1]
    assert len(prov.sent[0][1]) <= 160
    assert run(n.alert(2200, event_id=2)) == 0           # cooldown: one SMS per number
    assert len(prov.sent) == 3
    assert store.sms_sent_since(utcnow() - timedelta(hours=1)) == 3


def test_alert_text_has_usual_level(setup):
    s, store, prov, n = setup
    s.station_name = "Yerevan demo mini-substation"
    text = n.alert_text(2134.4, None, 9.2)
    assert "2,134 W (usual 9 W, limit 1,000 W)" in text and len(text) <= 160


def test_per_number_cap(setup):
    s, store, prov, n = setup
    s.sms_cooldown_s = 0
    s.sms_max_per_number = 2
    store.upsert_subscriber("+37477123450", "en", 24, "tok")
    sent = sum(run(n.alert(2000, event_id=i)) for i in range(4))
    assert sent == 2


def test_global_hourly_cap(setup):
    s, store, prov, n = setup
    s.sms_max_per_hour = 2
    for i in range(5):
        store.upsert_subscriber(f"+3747712345{i}", "en", 24, f"tok{i}")
    assert run(n.alert(2000, event_id=1)) == 2


def test_always_notify_and_disabled(setup):
    s, store, prov, n = setup
    s.sms_always_notify = ["+374 99 000111", "junk"]
    assert run(n.alert(2000, None)) == 1
    s.sms_enabled = False
    s.sms_always_notify = ["+374 99 000222"]
    assert run(n.alert(2000, None)) == 0


def test_expired_and_unsubscribed_not_notified(setup):
    s, store, prov, n = setup
    row, _ = store.upsert_subscriber("+37477123450", "en", 24, "tokA")
    store.upsert_subscriber("+37477123451", "en", -1, "tokB")     # already expired
    assert store.unsubscribe("tokA") is True
    assert store.unsubscribe("tokA") is False
    assert run(n.alert(2000, None)) == 0
    assert store.delete_expired_subscribers() == 2
    assert store.count_active_subscribers() == 0


def test_duplicate_signup_updates_expiry(setup):
    s, store, prov, n = setup
    a, created_a = store.upsert_subscriber("+37477123450", "en", 1, "tok1")
    b, created_b = store.upsert_subscriber("+37477123450", "hy", 24, "tok2")
    assert created_a and not created_b
    assert b["id"] == a["id"] and b["expires_at"] > a["expires_at"] and b["lang"] == "hy"


def test_failing_provider_is_logged_not_raised(setup):
    s, store, _, _ = setup

    class Boom:
        name = "boom"

        async def send(self, to, text):
            raise RuntimeError("provider down")

        def configured(self):
            return True, ""

    n = Notifier(s, store, Boom())
    store.upsert_subscriber("+37477123450", "en", 24, "tok")
    assert run(n.alert(2000, None)) == 0
    assert store.recent_sms(1)[0]["status"] == "failed"


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_twilio_adapter():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), req.content.decode()
        return httpx.Response(201, json={"sid": "SM123"})

    s = Settings(twilio_account_sid="AC1", twilio_auth_token="x", twilio_messaging_service_sid="MG1")
    res = run(TwilioProvider(s, _mock_client(handler)).send("+37477123456", "hi"))
    assert res.ok and res.provider_msg_id == "SM123"
    assert seen["url"].endswith("/Accounts/AC1/Messages.json") and "MessagingServiceSid=MG1" in seen["body"]


def test_twilio_error_and_missing_config():
    s = Settings(twilio_account_sid="AC1", twilio_auth_token="x", twilio_messaging_service_sid="MG1")
    res = run(TwilioProvider(s, _mock_client(lambda r: httpx.Response(400, json={"message": "bad To"}))).send("+1", "x"))
    assert not res.ok and "bad To" in res.error
    assert not run(TwilioProvider(Settings()).send("+1", "x")).ok


def test_android_gateway_adapter():
    seen = {}

    def handler(req):
        seen["url"], seen["json"], seen["auth"] = str(req.url), req.content, req.headers.get("authorization")
        return httpx.Response(202, json={"id": "abc"})

    s = Settings(smsgate_url="http://10.0.0.5:8080", smsgate_username="u", smsgate_password="p")
    res = run(AndroidGatewayProvider(s, _mock_client(handler)).send("+37477123456", "hi"))
    assert res.ok and res.provider_msg_id == "abc"
    assert seen["url"] == "http://10.0.0.5:8080/message" and b'"phoneNumbers"' in seen["json"] and seen["auth"]

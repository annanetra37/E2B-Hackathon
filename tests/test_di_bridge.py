"""T1: the DI bridge's router client (router faked with httpx.MockTransport)."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("di_bridge", Path(__file__).resolve().parent.parent / "tools" / "di_bridge.py")
di_bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(di_bridge)


def cfg(**kw):
    c = dict(di_bridge.DEFAULTS)
    c.update(ROUTER_PASSWORD="pw", **kw)
    return c


class FakeRouter:
    def __init__(self, reply_value, needs_login=True):
        self.value = reply_value
        self.needs_login = needs_login
        self.token = "tok-1"
        self.logins = 0
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.calls.append(body)
        if body["method"] == "login":
            self.logins += 1
            assert body["params"] == {"username": "admin", "password": "pw"}
            return httpx.Response(200, json={"result": {"token": self.token}})
        if body["method"] == "do_di_status_get":
            if self.needs_login and req.headers.get("authorization") != f"Bearer {self.token}":
                return httpx.Response(401)
            return httpx.Response(200, json={"result": {"di": [{"name": "di1", "status": self.value}]}})
        return httpx.Response(404)


def router(fake, **kw):
    return di_bridge.Router(cfg(ROUTER_HEADERS='{"Authorization": "Bearer {token}"}', **kw),
                            transport=httpx.MockTransport(fake))


def test_private_router_skips_certificate_check_only_on_lan():
    assert di_bridge.Router(cfg()).verify is False                      # 10.18.249.233
    assert di_bridge.Router(cfg(ROUTER_URL="https://router.example.com")).verify is True


@pytest.mark.parametrize("value,level", [("LOW", "LOW"), ("HIGH", "HIGH"), (0, "LOW"), (1, "HIGH"), ("low", "LOW")])
def test_reads_level(value, level):
    fake = FakeRouter(value)
    r = router(fake)
    got, raw = r.read_level()
    assert got == level and fake.logins == 1


def test_relogin_when_session_expires():
    fake = FakeRouter("LOW")
    r = router(fake)
    r.read_level()
    fake.token = "tok-2"                                                # router forgot the session
    assert r.read_level()[0] == "LOW" and fake.logins == 2


def test_unreadable_reply_raises():
    fake = FakeRouter("maybe")
    with pytest.raises(di_bridge.RouterError):
        router(fake).read_level()


def test_timeout_raises_router_error():
    def boom(req):
        raise httpx.ConnectTimeout("timed out")
    with pytest.raises(di_bridge.RouterError):
        di_bridge.Router(cfg(), transport=httpx.MockTransport(boom)).read_level()


def test_low_means_on_mapping_and_dry_run(capsys):
    pub = di_bridge.Publisher(cfg(), dry_run=True)
    pub.publish_di(1, "LOW")
    pub.publish_di(0, "HIGH")
    lines = capsys.readouterr().out.strip().splitlines()
    msgs = [json.loads(l.split("  ", 1)[1]) for l in lines]
    assert [m["di1"] for m in msgs] == [1, 0] and [m["seq"] for m in msgs] == [0, 1]
    assert msgs[0]["station"] == "YVN-DEMO-01" and msgs[0]["src"] == "di-bridge" and msgs[0]["ts"].endswith("Z")


def test_env_file(tmp_path, monkeypatch):
    p = tmp_path / "b.env"
    p.write_text("ROUTER_URL=https://192.168.1.1\nPOLL_SECONDS='2'\n# comment\nDI_LOW_MEANS_ON=false\n")
    monkeypatch.setenv("POLL_SECONDS", "3")
    c = di_bridge.load_env(p)
    assert c["ROUTER_URL"] == "https://192.168.1.1" and c["POLL_SECONDS"] == "3" and c["DI_LOW_MEANS_ON"] == "false"

from __future__ import annotations

import pytest

from conftest import AUTH, TOKEN
from mywanip.cli import check_bind, resolve_token

CF = {"hostname": "home.example.com", "zone": "example.com", "secret": "cf-token-123456"}


def test_public_endpoints_open_and_ip_unknown_until_first_check(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/ip").status_code == 503
    assert client.post("/api/check", headers=AUTH).json()["ip"] == "8.8.8.8"
    assert client.get("/ip").text == "8.8.8.8\n"
    assert client.get("/ip.json").json()["ip"] == "8.8.8.8"
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}])
def test_admin_api_needs_the_token(client, headers):
    assert client.get("/api/dns-records", headers=headers).status_code == 401
    assert client.post("/api/check", headers=headers).status_code == 401


def test_secret_is_never_returned_and_empty_patch_keeps_it(client, svc):
    client.post("/api/check", headers=AUTH)
    r = client.post("/api/dns-records", json=CF, headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["secret_hint"] == "••••3456" and "secret" not in body
    assert body["last_status"] == "ok"  # synced immediately
    assert "cf-token-123456" not in client.get("/api/dns-records", headers=AUTH).text

    client.patch(f"/api/dns-records/{body['id']}", json={"secret": "", "ttl": 120}, headers=AUTH)
    row = svc.db.row("dns_records", body["id"])
    assert (row["secret"], row["ttl"]) == ("cf-token-123456", 120)


def test_short_secret_hint_hides_everything(client):
    r = client.post("/api/webhooks", json={"url": "http://hook.test/x", "secret": "abcd1234"}, headers=AUTH)
    assert r.json()["secret_hint"] == "••••"


def test_webhook_crud_and_test_delivery(client):
    client.post("/api/check", headers=AUTH)
    r = client.post("/api/webhooks", json={"url": "http://hook.test/x"}, headers=AUTH).json()
    assert r["last_ip"] == "8.8.8.8" and r["secret_hint"] == ""
    assert client.post(f"/api/webhooks/{r['id']}/test", headers=AUTH).json()["last_status"] == "ok"
    assert client.delete(f"/api/webhooks/{r['id']}", headers=AUTH).status_code == 204
    assert client.delete(f"/api/webhooks/{r['id']}", headers=AUTH).status_code == 404


def test_validation(client):
    assert client.post("/api/webhooks", json={"url": "ftp://x"}, headers=AUTH).status_code == 422
    assert client.post("/api/dns-records", json={**CF, "provider": "nope"}, headers=AUTH).status_code == 422
    assert client.patch("/api/settings", json={"poll_interval": 5}, headers=AUTH).status_code == 422


def test_settings_roundtrip(client):
    r = client.patch("/api/settings", json={"poll_interval": 60}, headers=AUTH)
    assert r.json()["poll_interval"] == 60


# -- cli helpers -------------------------------------------------------------


def test_token_is_generated_once_then_reused(svc, monkeypatch):
    monkeypatch.delenv("MYWANIP_ADMIN_TOKEN", raising=False)
    first, generated = resolve_token(svc.db)
    assert generated and len(first) >= 32
    assert resolve_token(svc.db) == (first, False)
    monkeypatch.setenv("MYWANIP_ADMIN_TOKEN", "from-env")
    assert resolve_token(svc.db) == ("from-env", False)


def test_wide_bind_needs_a_long_token():
    check_bind("127.0.0.1", "short")
    check_bind("0.0.0.0", "x" * 32)
    with pytest.raises(SystemExit):
        check_bind("0.0.0.0", "short")

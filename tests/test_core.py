from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from mywanip.detect import DetectError, detect


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- detection ---------------------------------------------------------------


def test_detect_needs_two_sources_to_agree():
    answers = {"a": "1.2.3.4", "b": "1.2.3.4", "c": "5.6.7.8"}
    http = client_for(lambda r: httpx.Response(200, text=answers[r.url.host]))
    assert run(detect(http, ["https://a", "https://b", "https://c"])) == "1.2.3.4"


def test_detect_refuses_disagreement():
    answers = {"a": "1.2.3.4", "b": "5.6.7.8"}
    http = client_for(lambda r: httpx.Response(200, text=answers[r.url.host]))
    with pytest.raises(DetectError):
        run(detect(http, ["https://a", "https://b"]))


def test_detect_rejects_private_and_garbage():
    http = client_for(lambda r: httpx.Response(200, text="192.168.1.1" if r.url.host == "a" else "oops"))
    with pytest.raises(DetectError):
        run(detect(http, ["https://a", "https://b"]))


def test_detect_single_source_is_enough_when_only_one_configured():
    http = client_for(lambda r: httpx.Response(200, text="1.2.3.4"))
    assert run(detect(http, ["https://a"])) == "1.2.3.4"


# -- check cycle -------------------------------------------------------------


def add_record(svc):
    return svc.db.insert(
        "dns_records", provider="cloudflare", hostname="home.example.com", zone="example.com",
        secret="cf-token-123456", ttl=300, proxied=0, enabled=1,
    )


def test_first_check_records_ip_and_updates_dns(svc, world):
    id = add_record(svc)
    state = run(svc.check())
    assert state["ip"] == "8.8.8.8"
    rec = svc.db.row("dns_records", id)
    assert (rec["last_ip"], rec["last_status"]) == ("8.8.8.8", "ok")
    posts = [c for c in world.calls if c.method == "POST"]
    assert json.loads(posts[0].content)["content"] == "8.8.8.8"


def test_unchanged_ip_does_not_touch_dns_again(svc, world):
    add_record(svc)
    run(svc.check())
    world.calls.clear()
    run(svc.check())
    assert not [c for c in world.calls if c.url.host == "api.cloudflare.com"]


def test_failed_dns_update_is_retried_next_check(svc, world):
    id = svc.db.insert(
        "dns_records", provider="cloudflare", hostname="home.example.com", zone="example.com",
        secret="wrong-token-0000", ttl=300, proxied=0, enabled=1,
    )
    run(svc.check())
    rec = svc.db.row("dns_records", id)
    assert rec["last_ip"] is None and "bad token" in rec["last_status"]
    svc.db.update("dns_records", id, secret="cf-token-123456")
    run(svc.check())
    assert svc.db.row("dns_records", id)["last_ip"] == "8.8.8.8"


def test_webhook_gets_signed_change_and_failure_retries(svc, world):
    run(svc.check())  # IP known before the webhook exists
    id = svc.db.insert("webhooks", url="http://hook.test/x", secret="s3cret", last_ip="8.8.8.8")
    run(svc.check())
    assert not [c for c in world.calls if c.url.host == "hook.test"]  # no news, no call

    world.ip, world.hook_status = "9.9.9.9", 500
    run(svc.check())
    assert svc.db.row("webhooks", id)["last_ip"] == "8.8.8.8"  # not delivered

    world.hook_status = 200
    world.calls.clear()
    run(svc.check())
    (call,) = [c for c in world.calls if c.url.host == "hook.test"]
    body = json.loads(call.content)
    assert (body["old"], body["new"], body["event"]) == ("8.8.8.8", "9.9.9.9", "ip_changed")
    sig = hmac.new(b"s3cret", call.content, hashlib.sha256).hexdigest()
    assert call.headers["X-MyWanIP-Signature"] == f"sha256={sig}"
    assert svc.db.row("webhooks", id)["last_ip"] == "9.9.9.9"


def test_subscribers_see_changes(svc, world):
    q = svc.subscribe()
    run(svc.check())
    assert q.get_nowait()["ip"] == "8.8.8.8"
    run(svc.check())
    assert q.empty()  # no change, no event
    world.ip = "9.9.9.9"
    run(svc.check())
    ev = q.get_nowait()
    assert (ev["ip"], ev["previous"]) == ("9.9.9.9", "8.8.8.8")

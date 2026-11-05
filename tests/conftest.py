from __future__ import annotations

import httpx
import pytest

from mywanip.app import create_app
from mywanip.core import Service
from mywanip.db import DB

TOKEN = "t" * 40


class World:
    """A fake internet: echo sources, Cloudflare and webhook receivers, with call recording."""

    def __init__(self):
        self.ip = "8.8.8.8"
        self.calls: list[httpx.Request] = []
        self.hook_status = 200
        self.cf_records: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host.startswith("echo"):
            return httpx.Response(200, text=self.ip + "\n")
        if host == "api.cloudflare.com":
            return self.cloudflare(req, path)
        if host == "hook.test":
            return httpx.Response(self.hook_status)
        return httpx.Response(404)

    def cloudflare(self, req: httpx.Request, path: str) -> httpx.Response:
        ok = lambda result: httpx.Response(200, json={"success": True, "result": result})
        if req.headers["Authorization"] != "Bearer cf-token-123456":
            return httpx.Response(403, json={"success": False, "errors": [{"message": "bad token"}]})
        if path == "/client/v4/zones":
            return ok([{"id": "z1"}] if req.url.params["name"] == "example.com" else [])
        if req.method == "GET":
            return ok(self.cf_records)
        if req.method == "POST":
            self.cf_records.append({"id": "r1"})
        return ok({})


@pytest.fixture
def world():
    return World()


@pytest.fixture
def svc(world):
    db = DB(":memory:")
    db.set("sources", ["https://echo1.test", "https://echo2.test"])
    return Service(db, httpx.AsyncClient(transport=httpx.MockTransport(world.handler)))


@pytest.fixture
def client(svc):
    from fastapi.testclient import TestClient

    return TestClient(create_app(svc.db, TOKEN, svc=svc, poll=False))


AUTH = {"Authorization": f"Bearer {TOKEN}"}

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from .db import DB, now
from .detect import detect
from .providers import PROVIDERS

log = logging.getLogger("mywanip")


class Service:
    def __init__(self, db: DB, http: httpx.AsyncClient | None = None):
        self.db = db
        self.http = http or httpx.AsyncClient(timeout=10)
        self.subs: set[asyncio.Queue] = set()
        self.lock = asyncio.Lock()

    def current(self) -> dict:
        return {k: self.db.get(k) for k in ("ip", "changed_at", "previous", "checked_at")}

    # -- SSE ---------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.subs.add(q)
        return q

    def _publish(self) -> None:
        for q in self.subs:
            try:
                q.put_nowait(self.current())
            except asyncio.QueueFull:
                pass  # a stalled client just misses events; it gets the current IP on reconnect

    # -- the one cycle -------------------------------------------------------
    async def check(self) -> dict:
        """Detect the IP, record a change, then push to everything that is behind."""
        async with self.lock:
            ip = await detect(self.http, self.db.get("sources"))
            self.db.set("checked_at", now())
            old = self.db.get("ip")
            if ip != old:
                log.info("WAN IP changed: %s -> %s", old, ip)
                self.db.set("previous", old)
                self.db.set("ip", ip)
                self.db.set("changed_at", now())
                self._publish()
            await self._reconcile(ip)
            return self.current()

    async def _reconcile(self, ip: str) -> None:
        # Anything whose last_ip lags is retried every tick, so failures and
        # restarts self-heal. This is the only retry mechanism.
        for r in self.db.rows("dns_records"):
            if r["enabled"] and r["last_ip"] != ip:
                await self.sync_record(r, ip)
        for w in self.db.rows("webhooks"):
            if w["enabled"] and w["last_ip"] != ip:
                await self.deliver(w, "ip_changed", w["last_ip"] or self.db.get("previous"), ip)

    # -- DNS ---------------------------------------------------------------
    async def sync_record(self, rec: dict, ip: str) -> None:
        try:
            await PROVIDERS[rec["provider"]](self.http, rec, ip)
            fields = {"last_ip": ip, "last_status": "ok"}
        except Exception as e:  # noqa: BLE001 - any failure is reported, not fatal
            log.warning("DNS update for %s failed: %s", rec["hostname"], e)
            fields = {"last_status": f"error: {e}"}
        self.db.update("dns_records", rec["id"], last_at=now(), **fields)

    # -- webhooks ----------------------------------------------------------
    async def deliver(self, w: dict, event: str, old: str | None, new: str) -> None:
        body = json.dumps({"event": event, "old": old, "new": new, "at": now()}).encode()
        headers = {"Content-Type": "application/json"}
        if w["secret"]:
            sig = hmac.new(w["secret"].encode(), body, hashlib.sha256).hexdigest()
            headers["X-MyWanIP-Signature"] = f"sha256={sig}"
        try:
            r = await self.http.post(w["url"], content=body, headers=headers)
            r.raise_for_status()
            fields = {"last_status": "ok"}
            if event == "ip_changed":
                fields["last_ip"] = new
        except Exception as e:  # noqa: BLE001
            log.warning("webhook %s failed: %s", w["url"], e)
            fields = {"last_status": f"error: {e}"}
        self.db.update("webhooks", w["id"], last_at=now(), **fields)

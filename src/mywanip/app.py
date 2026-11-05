from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import AfterValidator, BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import Service
from .db import DB
from .detect import DetectError
from .providers import PROVIDERS

log = logging.getLogger("mywanip")
INDEX = Path(__file__).parent / "static" / "index.html"


def _http_url(v: str) -> str:
    if not v.startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    return v


Url = Annotated[str, AfterValidator(_http_url)]


class DnsCreate(BaseModel):
    provider: str = "cloudflare"
    hostname: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    secret: str = Field(min_length=1)
    ttl: int = Field(300, ge=1, le=86400)
    proxied: bool = False
    enabled: bool = True


class DnsPatch(BaseModel):
    hostname: str | None = None
    zone: str | None = None
    secret: str | None = None
    ttl: int | None = Field(None, ge=1, le=86400)
    proxied: bool | None = None
    enabled: bool | None = None


class HookCreate(BaseModel):
    url: Url
    secret: str = ""
    enabled: bool = True


class HookPatch(BaseModel):
    url: Url | None = None
    secret: str | None = None
    enabled: bool | None = None


class SettingsPatch(BaseModel):
    poll_interval: int | None = Field(None, ge=30)
    sources: list[Url] | None = Field(None, min_length=1)


def view(row: dict) -> dict:
    """Row for API output. Secrets never leave the server; only a short hint does."""
    out = dict(row)
    s = out.pop("secret", "")
    # Only reveal the tail of a long secret; a short one would be mostly exposed.
    out["secret_hint"] = ("••••" + s[-4:]) if len(s) >= 12 else ("••••" if s else "")
    for k in ("enabled", "proxied"):
        if k in out:
            out[k] = bool(out[k])
    return out


def changes(model: BaseModel) -> dict:
    """Patch fields that were actually given. Empty/null never overwrites (so a secret is kept)."""
    return {k: v for k, v in model.model_dump(exclude_unset=True).items() if v is not None and v != ""}


def sse(data: dict) -> str:
    return f"event: ip\ndata: {json.dumps(data)}\n\n"


def admin(request: Request, authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.encode(), request.app.state.token.encode()
    ):
        raise HTTPException(401, "admin token required", headers={"WWW-Authenticate": "Bearer"})


def create_app(
    db: DB, token: str, svc: Service | None = None, poll: bool = True, allowed_hosts: list[str] | None = None
) -> FastAPI:
    svc = svc or Service(db)

    async def loop() -> None:
        while True:
            try:
                await svc.check()
            except Exception as e:  # noqa: BLE001 - keep polling whatever goes wrong
                log.warning("check failed: %s", e)
            await asyncio.sleep(db.get("poll_interval"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop()) if poll else None
        yield
        if task:
            task.cancel()
        await svc.http.aclose()

    app = FastAPI(title="mywanip", lifespan=lifespan)
    app.state.token = token
    if allowed_hosts:
        # Always allow loopback so the container health check keeps working.
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=[*allowed_hosts, "127.0.0.1", "localhost"]
        )

    # -- public ----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(INDEX)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/ip", response_class=PlainTextResponse)
    async def ip():
        if not db.get("ip"):
            raise HTTPException(503, "IP not detected yet")
        return db.get("ip") + "\n"

    @app.get("/ip.json")
    async def ip_json():
        if not db.get("ip"):
            raise HTTPException(503, "IP not detected yet")
        return svc.current()

    @app.get("/events")
    async def events():
        q = svc.subscribe()

        async def gen():
            try:
                if db.get("ip"):
                    yield sse(svc.current())
                while True:
                    try:
                        yield sse(await asyncio.wait_for(q.get(), 15))
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                svc.subs.discard(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- admin -----------------------------------------------------------------
    api = APIRouter(prefix="/api", dependencies=[Depends(admin)])

    @api.post("/check")
    async def check():
        try:
            return await svc.check()
        except DetectError as e:
            raise HTTPException(502, str(e)) from None

    @api.get("/settings")
    async def get_settings():
        return {"poll_interval": db.get("poll_interval"), "sources": db.get("sources")}

    @api.patch("/settings")
    async def patch_settings(body: SettingsPatch):
        for k, v in changes(body).items():
            db.set(k, v)
        return await get_settings()

    def found(table: str, id: int) -> dict:
        row = db.row(table, id)
        if not row:
            raise HTTPException(404, "not found")
        return row

    async def sync_now(id: int) -> dict:
        row = db.row("dns_records", id)
        if row["enabled"] and db.get("ip"):
            await svc.sync_record(row, db.get("ip"))
        return view(db.row("dns_records", id))

    @api.get("/dns-records")
    async def list_dns():
        return [view(r) for r in db.rows("dns_records")]

    @api.post("/dns-records", status_code=201)
    async def create_dns(body: DnsCreate):
        if body.provider not in PROVIDERS:
            raise HTTPException(422, f"unknown provider; available: {sorted(PROVIDERS)}")
        return await sync_now(db.insert("dns_records", **body.model_dump()))

    @api.patch("/dns-records/{id}")
    async def patch_dns(id: int, body: DnsPatch):
        found("dns_records", id)
        db.update("dns_records", id, last_ip=None, **changes(body))  # re-push after any edit
        return await sync_now(id)

    @api.delete("/dns-records/{id}", status_code=204)
    async def delete_dns(id: int):
        if not db.delete("dns_records", id):
            raise HTTPException(404, "not found")

    @api.post("/dns-records/{id}/sync")
    async def sync_dns(id: int):
        found("dns_records", id)
        if not db.get("ip"):
            raise HTTPException(409, "IP not detected yet")
        db.update("dns_records", id, enabled=1)
        return await sync_now(id)

    @api.get("/webhooks")
    async def list_hooks():
        return [view(r) for r in db.rows("webhooks")]

    @api.post("/webhooks", status_code=201)
    async def create_hook(body: HookCreate):
        # Start in sync with the current IP: only future changes notify.
        id = db.insert("webhooks", **body.model_dump(), last_ip=db.get("ip"))
        return view(db.row("webhooks", id))

    @api.patch("/webhooks/{id}")
    async def patch_hook(id: int, body: HookPatch):
        found("webhooks", id)
        db.update("webhooks", id, **changes(body))
        return view(db.row("webhooks", id))

    @api.delete("/webhooks/{id}", status_code=204)
    async def delete_hook(id: int):
        if not db.delete("webhooks", id):
            raise HTTPException(404, "not found")

    @api.post("/webhooks/{id}/test")
    async def test_hook(id: int):
        row = found("webhooks", id)
        await svc.deliver(row, "test", None, db.get("ip") or "0.0.0.0")
        return view(db.row("webhooks", id))

    app.include_router(api)
    return app

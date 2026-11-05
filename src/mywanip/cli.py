from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

import httpx
import uvicorn

from .app import create_app
from .db import DB
from .detect import detect

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def open_db() -> DB:
    data = Path(os.environ.get("MYWANIP_DATA") or Path.home() / ".local/share/mywanip")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    return DB(str(data / "mywanip.db"))


def resolve_token(db: DB) -> tuple[str, bool]:
    """Admin token: env var, else the one stored earlier, else a new one. Returns (token, generated)."""
    token = os.environ.get("MYWANIP_ADMIN_TOKEN") or db.get("admin_token")
    if token:
        return token, False
    token = secrets.token_urlsafe(32)
    db.set("admin_token", token)
    return token, True


def check_bind(host: str, token: str) -> None:
    if host not in LOOPBACK and len(token) < 32:
        sys.exit("Refusing to listen beyond loopback with an admin token under 32 characters "
                 "(use: openssl rand -hex 32).")


def run_check(db: DB) -> int:
    async def go() -> str:
        async with httpx.AsyncClient(timeout=10) as http:
            return await detect(http, db.get("sources"))

    try:
        ip = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"OK: WAN IP is {ip}")
    print(f"{len(db.rows('dns_records'))} DNS record(s), {len(db.rows('webhooks'))} webhook(s) configured")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="mywanip", description="Dynamic WAN IP resolver and announcer.")
    p.add_argument("--host", default=os.environ.get("MYWANIP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MYWANIP_PORT", "8787")))
    p.add_argument("--lan", action="store_true", help="listen on all interfaces (same as --host 0.0.0.0)")
    p.add_argument("--check", action="store_true", help="detect the WAN IP, print it and exit")
    p.add_argument("--show-token", action="store_true", help="print the admin token and exit")
    args = p.parse_args()

    db = open_db()
    if args.check:
        sys.exit(run_check(db))
    if args.show_token:
        token = os.environ.get("MYWANIP_ADMIN_TOKEN") or db.get("admin_token")
        if not token:
            sys.exit("No admin token yet; start the service once to generate one.")
        print(token)  # stdout, so `$(mywanip --show-token)` works
        sys.exit(0)

    host = "0.0.0.0" if args.lan else args.host
    token, generated = resolve_token(db)
    check_bind(host, token)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if generated:
        logging.getLogger("mywanip").warning(
            "Generated admin token (also see `mywanip --show-token`): %s", token
        )
    hosts = [h.rsplit(":", 1)[0] if h.rsplit(":", 1)[-1].isdigit() else h
             for h in os.environ.get("MYWANIP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    uvicorn.run(create_app(db, token, allowed_hosts=hosts), host=host, port=args.port, log_level="info")

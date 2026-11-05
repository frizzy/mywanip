from __future__ import annotations

import asyncio
import ipaddress
from collections import Counter

import httpx


class DetectError(Exception):
    pass


async def _one(http: httpx.AsyncClient, url: str) -> str:
    r = await http.get(url)
    r.raise_for_status()
    ip = ipaddress.IPv4Address(r.text.strip())
    if not ip.is_global:  # a misbehaving source must not point DNS at a LAN address
        raise ValueError(f"{ip} is not a public address")
    return str(ip)


async def detect(http: httpx.AsyncClient, sources: list[str]) -> str:
    """Public IPv4 address that at least two sources agree on (one if only one is configured)."""
    results = await asyncio.gather(*(_one(http, u) for u in sources), return_exceptions=True)
    ips = Counter(r for r in results if isinstance(r, str))
    if ips:
        ip, n = ips.most_common(1)[0]
        if n >= min(2, len(sources)):
            return ip
    errors = [f"{u}: {r}" for u, r in zip(sources, results) if isinstance(r, Exception)]
    raise DetectError(f"sources disagree or failed: {dict(ips)} {errors}")

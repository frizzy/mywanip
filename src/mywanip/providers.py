from __future__ import annotations

import httpx


class ProviderError(Exception):
    pass


CF = "https://api.cloudflare.com/client/v4"


async def cloudflare(http: httpx.AsyncClient, rec: dict, ip: str) -> None:
    """Upsert the A record `rec['hostname']` in zone `rec['zone']` to `ip`."""
    headers = {"Authorization": f"Bearer {rec['secret']}"}

    async def call(method: str, path: str, **kw):
        r = await http.request(method, CF + path, headers=headers, **kw)
        try:
            data = r.json()
        except ValueError:
            raise ProviderError(f"Cloudflare returned HTTP {r.status_code}") from None
        if not data.get("success"):
            msgs = "; ".join(e.get("message", "?") for e in data.get("errors", []))
            raise ProviderError(msgs or f"Cloudflare returned HTTP {r.status_code}")
        return data["result"]

    zones = await call("GET", "/zones", params={"name": rec["zone"]})
    if not zones:
        raise ProviderError(f"zone {rec['zone']} not found (check the token's zone access)")
    base = f"/zones/{zones[0]['id']}/dns_records"
    body = {
        "type": "A",
        "name": rec["hostname"],
        "content": ip,
        "ttl": rec["ttl"],
        "proxied": bool(rec["proxied"]),
    }
    existing = await call("GET", base, params={"type": "A", "name": rec["hostname"]})
    if existing:
        await call("PATCH", f"{base}/{existing[0]['id']}", json=body)
    else:
        await call("POST", base, json=body)


# Adding a provider = one async function with this signature, registered here.
PROVIDERS = {"cloudflare": cloudflare}

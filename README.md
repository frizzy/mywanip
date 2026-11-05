# mywanip

Finds your dynamic WAN IP, keeps a DNS record pointing at it, and tells everything
else when it changes.

- **DNS update:** Cloudflare today; other providers are one function each (`providers.py`).
- **REST:** `GET /ip` for the current address.
- **Announce:** webhooks (signed, retried) and a Server-Sent Events stream.
- **Manage it all** from a small web UI or the `/api`. Provider tokens and
  webhook secrets are write-only: once stored, only `••••abcd` is ever shown.

It has to run on a machine behind the connection whose IP you care about (a Raspberry
Pi, a NAS). Storage is a single SQLite file.

> **CGNAT check:** if your ISP puts you behind carrier-grade NAT, the IP this
> reports is not reachable from outside and DNS will point at the wrong place. If your
> router's WAN address differs from what `curl api.ipify.org` prints, ask your ISP for
> a public IP.

## How it works

Every `poll_interval` seconds (default 300) it asks the configured echo endpoints for
your public IPv4 address and accepts it only if at least two agree. When the address
changes it records it, pushes it to every enabled DNS record and webhook, and emits an
SSE event. Anything that fails keeps being retried every cycle until it succeeds, so
a flaky provider or a restart never loses an update.

## Endpoints

Public (no token; keep the service on a trusted network):

| | |
| --- | --- |
| `GET /ip` | Current IP as plain text |
| `GET /ip.json` | `{ip, changed_at, previous, checked_at}` |
| `GET /events` | SSE: an `ip` event with the current IP on connect, then on every change |
| `GET /healthz` | Liveness |
| `GET /` | The web UI |

Admin (header `Authorization: Bearer <admin token>`):

| | |
| --- | --- |
| `POST /api/check` | Detect now and push anything that is behind |
| `GET/PATCH /api/settings` | `poll_interval` (seconds, min 30) and `sources` (echo URLs) |
| `GET/POST /api/dns-records` | `{provider, hostname, zone, secret, ttl, proxied, enabled}` |
| `PATCH/DELETE /api/dns-records/{id}` | A blank `secret` keeps the stored one |
| `POST /api/dns-records/{id}/sync` | Push the current IP now |
| `GET/POST /api/webhooks` | `{url, secret?, enabled}` |
| `PATCH/DELETE /api/webhooks/{id}` | |
| `POST /api/webhooks/{id}/test` | Send a `test` event |

Responses carry `secret_hint` (e.g. `••••abcd`), never `secret`.

### Webhooks

On a change, `POST <url>` with JSON:

```json
{"event": "ip_changed", "old": "1.2.3.4", "new": "5.6.7.8", "at": "2026-11-04T16:46:31+00:00"}
```

If you set a secret, the request carries `X-MyWanIP-Signature: sha256=<hex HMAC-SHA256 of the raw body>`.
Any 2xx counts as delivered. A new webhook starts in sync with the current IP, so
it is only called for future changes.

### SSE

```bash
curl -N http://pi.local:8787/events
# event: ip
# data: {"ip": "1.2.3.4", "changed_at": "...", "previous": "...", "checked_at": "..."}
```

## Cloudflare setup

Create an API token (My Profile → API Tokens) with **Zone → DNS → Edit** on your zone,
then add a record in the UI: hostname `home.example.com`, zone `example.com`, the token.
The A record is created if it does not exist. Leave `proxied` off for anything that
is not plain HTTP(S), for example a game server: Cloudflare's proxy does not carry other traffic.

## Running with Docker

A multi-arch image (amd64 and arm64, so a Pi works) is published as
`ghcr.io/frizzy/mywanip`.

```bash
docker compose up -d
docker compose exec mywanip mywanip --show-token   # the generated admin token
```

The UI is at <http://127.0.0.1:8787>. State lives in the `mywanip-data` volume. To set the
token yourself, put `MYWANIP_ADMIN_TOKEN=$(openssl rand -hex 32)` in a `.env` next to the
compose file. To open it to your network, change the port mapping to `"8787:8787"`
(and set a token of 32+ characters). Update with
`docker compose pull && docker compose up -d`.

Without Compose:

```bash
docker run -d --name mywanip --restart unless-stopped -p 127.0.0.1:8787:8787 \
  -v mywanip-data:/data ghcr.io/frizzy/mywanip
```

## Running on a Raspberry Pi (systemd)

Needs 64-bit Raspberry Pi OS (or any systemd Linux) and [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/frizzy/mywanip.git && cd mywanip
bash deploy/install.sh          # add --lan to listen on your network
```

This installs dependencies, creates `/etc/mywanip.env` (root-only) with a generated
admin token, checks that the WAN IP can be detected, then installs and starts a hardened
`mywanip` service and prints the URL and token. Re-run it to update.

| Task | Command |
| --- | --- |
| Logs | `sudo journalctl -u mywanip -f` |
| Status | `systemctl status mywanip` |
| Change bind/port/token | `sudo nano /etc/mywanip.env && sudo systemctl restart mywanip` |
| Data | `/var/lib/mywanip/mywanip.db` |

Or run it by hand anywhere: `uvx --from git+https://github.com/frizzy/mywanip mywanip --lan`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MYWANIP_ADMIN_TOKEN` | generated | Admin token. Must be 32+ characters when listening beyond loopback |
| `MYWANIP_HOST` | `127.0.0.1` | Bind address (`--host`, or `--lan` for `0.0.0.0`) |
| `MYWANIP_PORT` | `8787` | Port (`--port`) |
| `MYWANIP_DATA` | `~/.local/share/mywanip` | Directory holding `mywanip.db` |
| `MYWANIP_ALLOWED_HOSTS` | any | Comma-separated `Host` allowlist (DNS-rebinding defence) |

`mywanip --check` detects the IP, prints it and exits. `mywanip --show-token` prints the admin token.

## Security notes

- The admin token travels over plain HTTP. Keep this on a network you trust or
  behind a reverse proxy or Tailscale. **Do not port-forward it.**
- `/ip`, `/ip.json` and `/events` are open to anyone who can reach the port.
- Provider tokens and webhook secrets are stored in plaintext in the SQLite file
  (mode 600). Use a token scoped to DNS edit on one zone.
- The server refuses to listen beyond loopback with a short admin token.

## Development

```bash
uv sync
uv run pytest
uv run mywanip --port 8787
```

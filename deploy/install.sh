#!/usr/bin/env bash
# Install or update mywanip as a systemd service. Run on the Pi, as the user
# the service should run as (not root); it uses sudo where it must.
#
#   bash deploy/install.sh [--lan]
#
# First run: creates /etc/mywanip.env with a generated admin token. Later runs
# keep that file and just update and restart the service.
#
# The service listens on 127.0.0.1 only. --lan makes a first install listen on
# all interfaces instead; to switch later, edit MYWANIP_HOST in the env file.
set -euo pipefail

SERVICE=mywanip
ENV_FILE=/etc/mywanip.env
UNIT_FILE=/etc/systemd/system/$SERVICE.service
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
BIND_HOST=127.0.0.1

while [ $# -gt 0 ]; do
  case "$1" in
    --lan) BIND_HOST=0.0.0.0; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$RUN_USER" != root ] || die "Run this as your normal user, not root; it calls sudo itself."
command -v systemctl >/dev/null || die "systemd is required."

UV="$(command -v uv || true)"
[ -n "$UV" ] || { [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"; }
[ -n "$UV" ] || die "uv is not installed. Install it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."

say "Installing dependencies into $APP_DIR/.venv"
cd "$APP_DIR"
# The unit forbids writes under /home, so compile bytecode now.
"$UV" sync --frozen --no-dev --compile-bytecode

if sudo test -f "$ENV_FILE"; then
  say "Keeping existing $ENV_FILE"
else
  say "Creating $ENV_FILE"
  TOKEN="$(openssl rand -hex 32 2>/dev/null || "$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
  TMP="$(mktemp)"
  chmod 600 "$TMP"
  {
    echo "MYWANIP_HOST=$BIND_HOST"
    echo "MYWANIP_PORT=8787"
    echo "MYWANIP_ADMIN_TOKEN=$TOKEN"
  } > "$TMP"
  sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
  rm -f "$TMP"
fi

say "Checking that the WAN IP can be detected"
# Throwaway data dir: the real one is created by the service.
MYWANIP_DATA="$(mktemp -d)" "$APP_DIR/.venv/bin/mywanip" --check \
  || die "Could not detect the WAN IP (is this machine online?)."

say "Installing $UNIT_FILE"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" \
  "$APP_DIR/deploy/$SERVICE.service" | sudo tee "$UNIT_FILE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

PORT="$(sudo sed -n 's/^MYWANIP_PORT=//p' "$ENV_FILE")"; PORT="${PORT:-8787}"
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
  || die "The service did not come up. See: sudo journalctl -u $SERVICE -n 50"

TOKEN="$(sudo sed -n 's/^MYWANIP_ADMIN_TOKEN=//p' "$ENV_FILE")"
LISTEN="$(sudo sed -n 's/^MYWANIP_HOST=//p' "$ENV_FILE")"
if [ "$LISTEN" = 127.0.0.1 ] || [ "$LISTEN" = localhost ]; then
  URL="http://127.0.0.1:$PORT"
else
  URL="http://$(hostname).local:$PORT"
fi
cat <<MSG

Running: $URL   (open it in a browser and unlock with the token)

  Admin token: $TOKEN
  Current IP:  curl $URL/ip

Logs:    sudo journalctl -u $SERVICE -f
Config:  sudo nano $ENV_FILE && sudo systemctl restart $SERVICE
MSG

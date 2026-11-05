#!/usr/bin/env bash
# Interactive Docker Compose installer for mywanip.
#
#   curl -fsSL https://raw.githubusercontent.com/frizzy/mywanip/main/install-docker.sh | bash
#
# Asks where to install, which address to listen on and (optionally) your
# Cloudflare details, starts the service, then prints the admin token.
# Safe to re-run: an existing .env (and so the token and data) is kept.
#
# Environment overrides: MYWANIP_REF (branch or tag, default main),
# MYWANIP_BASE (where to fetch docker-compose.yml from), MYWANIP_TTY (where to
# read answers from, default /dev/tty; for tests).
set -euo pipefail

BASE="${MYWANIP_BASE:-https://raw.githubusercontent.com/frizzy/mywanip/${MYWANIP_REF:-main}}"
TTY="${MYWANIP_TTY:-/dev/tty}"
PORT=8787

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ask VAR "Prompt" [default] [secret]. Reads from the terminal, not stdin,
# because under `curl | bash` stdin is this script.
ask() {
  local var=$1 prompt=$2 default=${3:-} secret=${4:-} reply hint=""
  [ -z "$default" ] || hint=" [$default]"
  if [ -n "$secret" ]; then
    read -rsp "$prompt$hint: " reply <&3; echo >&2
  else
    read -rp "$prompt$hint: " reply <&3
  fi
  printf -v "$var" '%s' "${reply:-$default}"
}

gen_token() {
  openssl rand -hex 32 2>/dev/null || { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
}

main() {
  # Test in a subshell: a bare `exec ... 2>/dev/null` would silence stderr for the whole script.
  ( : <"$TTY" ) 2>/dev/null || die "I need a terminal to ask questions. Run this from an interactive shell."
  exec 3<"$TTY"  # opened once, so answers can come from a file as well as a terminal
  command -v curl >/dev/null || die "curl is required."
  command -v docker >/dev/null || die "Docker is required: https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' command)."
  docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Is it running, and are you allowed to use it?"

  say "mywanip Docker setup"
  local dir
  ask dir "Install directory (relative to $(pwd))" "./mywanip"
  dir="${dir/#\~/$HOME}"
  mkdir -p "$dir" && dir="$(cd "$dir" && pwd)"
  dc() { (cd "$dir" && docker compose "$@"); }

  local existing=0 bind=127.0.0.1
  if [ -f "$dir/.env" ] && [ -f "$dir/docker-compose.yml" ]; then
    existing=1
    bind="$(sed -n 's/^MYWANIP_BIND=//p' "$dir/.env" | tail -n1)"; bind="${bind:-127.0.0.1}"
    say "Found an existing install in $dir: keeping .env, the token and your data"
  else
    echo
    echo "Which address should mywanip listen on?"
    echo "  127.0.0.1  this machine only (safest; reach the UI with an SSH tunnel)"
    echo "  0.0.0.0    every network interface"
    echo "  a LAN IP   e.g. 192.168.1.10, to serve your home network only"
    ask bind "Listen address" "127.0.0.1"
    [[ $bind =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "'$bind' is not an IPv4 address."
    ( umask 077
      printf '# Written by install-docker.sh. Keep private: the token controls your DNS.\nMYWANIP_ADMIN_TOKEN=%s\nMYWANIP_BIND=%s\n' \
        "$(gen_token)" "$bind" > "$dir/.env" )
  fi

  # Everything we need to ask is asked now, so the rest runs unattended.
  local do_dns=n host="" zone="" cf_token="" ans re='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'
  echo
  ask ans "Set up a Cloudflare DNS record now? (y/n)" "$([ $existing = 1 ] && echo n || echo y)"
  if [[ $ans =~ ^[Yy] ]]; then
    do_dns=y
    echo "Create the token at dash.cloudflare.com > My Profile > API Tokens, with Zone > DNS > Edit."
    ask host "Hostname to keep updated (e.g. home.example.com)" ""
    [[ $host =~ $re ]] || die "'$host' is not a valid hostname."
    ask zone "Cloudflare zone (the domain)" "${host#*.}"
    [[ $zone =~ $re ]] || die "'$zone' is not a valid zone."
    ask cf_token "Cloudflare API token (input hidden)" "" secret
    [[ $cf_token =~ ^[A-Za-z0-9_-]{10,}$ ]] || die "That does not look like a Cloudflare API token."
  fi

  say "Downloading docker-compose.yml"
  curl -fsSL "$BASE/docker-compose.yml" -o "$dir/docker-compose.yml" || die "Could not download $BASE/docker-compose.yml"

  say "Pulling the image and starting mywanip"
  if ! dc pull; then
    # Offline re-runs can still use the copy already on this machine.
    docker image inspect "$(dc config --images | head -n1)" >/dev/null 2>&1 \
      || die "Could not pull the image (see the error above). 'unauthorized' means the package is private: run 'docker login ghcr.io' or ask the owner to make it public."
    echo "WARNING: pull failed; using the image already on this machine." >&2
  fi
  dc up -d --no-build

  local api_host=$bind
  [ "$api_host" != 0.0.0.0 ] || api_host=127.0.0.1
  local url="http://$api_host:$PORT" i
  for i in $(seq 1 60); do
    curl -fsS "$url/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS "$url/healthz" >/dev/null 2>&1 || { dc logs --tail 30; die "mywanip did not come up (logs above)."; }

  local token
  token="$(dc exec -T mywanip mywanip --show-token)"

  # api METHOD PATH [json]: the token and body go to curl on stdin, never on its command line.
  api() {
    {
      printf 'silent\nshow-error\nurl = "%s/api%s"\nrequest = "%s"\nheader = "Authorization: Bearer %s"\n' "$url" "$2" "$1" "$token"
      [ -z "${3:-}" ] || printf 'header = "Content-Type: application/json"\ndata = "%s"\n' "${3//\"/\\\"}"
    } | curl -K -
  }
  status_of() { api GET /dns-records | tr '}' '\n' | grep "\"hostname\":\"$host\"" | sed -n 's/.*"last_status":"\([^"]*\)".*/\1/p' | head -1; }

  local dns_note=""
  if [ "$do_dns" = y ]; then
    say "Adding $host (Cloudflare zone $zone)"
    if api GET /dns-records | grep -q "\"hostname\":\"$host\""; then
      dns_note="$host is already configured; left as it is."
    else
      api POST /dns-records "{\"provider\":\"cloudflare\",\"hostname\":\"$host\",\"zone\":\"$zone\",\"secret\":\"$cf_token\"}" >/dev/null
      api POST /check >/dev/null || true  # detect now and push, instead of waiting for the next poll
      local st; st="$(status_of)"
      if [ "$st" = ok ]; then
        dns_note="$host now points at your WAN IP."
      else
        dns_note="$host was added but the update failed: ${st:-no status yet}. Fix it in the UI (use 'New token' on the record)."
      fi
    fi
    cf_token=""
  fi

  local ip where
  ip="$(curl -fsS "$url/ip" 2>/dev/null || echo unknown)"
  case "$bind" in
    127.0.0.1) where="http://127.0.0.1:$PORT   (this machine only; from another machine: ssh -L $PORT:127.0.0.1:$PORT $(hostname))" ;;
    0.0.0.0)   where="http://$(hostname):$PORT" ;;
    *)         where="http://$bind:$PORT" ;;
  esac

  cat <<EOF

mywanip is running.

  UI / API:      $where
  Current IP:    $ip
EOF
  [ -z "$dns_note" ] || echo "  DNS:           $dns_note"
  cat <<EOF

  Access token:  $token

Use the token to unlock the UI, or as  Authorization: Bearer <token>  on /api/*.
It is saved in $dir/.env (private to you). /ip, /ip.json and /events need no token.

Manage it:
  cd $dir
  docker compose logs -f                      logs
  docker compose pull && docker compose up -d update
  docker compose down                         stop (data kept)
  docker compose down -v                      stop and DELETE all data
EOF
  if [ "$bind" != 127.0.0.1 ]; then
    echo
    echo "NOTE: this is plain HTTP on your network. Do not port-forward it to the internet."
  fi
}

# Everything runs from main, called last, so a truncated download runs nothing.
main "$@"

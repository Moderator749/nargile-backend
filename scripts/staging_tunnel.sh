#!/usr/bin/env bash
# Staging through a Cloudflare quick tunnel: this machine's API, Vercel's storefront.
#
#   cd saas_backend
#   scripts/staging_tunnel.sh            # tunnel + checks + Vercel env + deploy
#   scripts/staging_tunnel.sh --no-deploy  # tunnel + checks only
#
# What it does, in order:
#   1. refuses to run while the seed admin (admin@nargile.com / Admin123!, written
#      in scripts/seed_initial_data.py) can still log in — the tunnel address ends
#      up in the storefront's public JavaScript, and that account reads every
#      customer's data;
#   2. starts `cloudflared tunnel --url http://127.0.0.1:8000` and reads the
#      https://….trycloudflare.com address it is given;
#   3. checks /health, a product's photo URL and the photo itself through the
#      tunnel, and the CORS answer for https://test.nargilemalzemesi.com;
#   4. sets NEXT_PUBLIC_API_URL / NEXT_PUBLIC_SITE_URL on the Vercel project
#      (production) and runs `vercel --prod` from ../saas_frontend;
#   5. keeps the tunnel open until Ctrl+C.
#
# A quick tunnel gets a NEW address every time it starts, and NEXT_PUBLIC_*
# values are compiled into the build — so every run redeploys. The API must
# already be running on 127.0.0.1:8000.

set -euo pipefail

API_LOCAL="http://127.0.0.1:8000"
SITE_URL="https://test.nargilemalzemesi.com"
FRONTEND_DIR="$(cd "$(dirname "$0")/../../saas_frontend" && pwd)"
LOG="$(mktemp -t cloudflared).log"
DEPLOY=1
[[ "${1:-}" == "--no-deploy" ]] && DEPLOY=0

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# GET <base>/health — never HEAD (-I): the route accepts GET only and answers
# HEAD with 405. Healthy = HTTP 200 and a body of {"status":"ok"}. Prints the
# HTTP code it got (000 = no answer at all: DNS, connection, timeout).
health() {
  local body code
  body=$(mktemp)
  code=$(curl -sS -X GET -o "$body" -w '%{http_code}' --max-time 10 "$1/health" 2>/dev/null) || code="${code:-000}"
  if [[ "$code" == "200" ]] && grep -q '"status":"ok"' "$body"; then
    rm -f "$body"; echo "200"; return 0
  fi
  rm -f "$body"; echo "$code"; return 1
}

command -v cloudflared >/dev/null || fail "cloudflared is not installed: brew install cloudflare/cloudflare/cloudflared"
local_health=$(health "$API_LOCAL") || fail "GET $API_LOCAL/health answered $local_health, not 200 {\"status\":\"ok\"} — start uvicorn first."

say "1/5 Safety check"
seed_login=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API_LOCAL/api/v1/auth/login" \
  -H 'Content-Type: application/json' -d '{"email":"admin@nargile.com","password":"Admin123!"}')
if [[ "$seed_login" == "200" ]]; then
  fail "The seed admin admin@nargile.com still logs in with Admin123!. Deactivate it or change its password before exposing the API."
fi
echo "Seed admin password does not work (HTTP $seed_login). OK."

say "2/5 Starting the tunnel"
cloudflared tunnel --no-autoupdate --url "$API_LOCAL" >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill $TUNNEL_PID 2>/dev/null; echo; echo "Tunnel closed."' EXIT
TUNNEL=""
for _ in $(seq 1 60); do
  TUNNEL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [[ -n "$TUNNEL" ]] && break
  sleep 1
done
[[ -n "$TUNNEL" ]] || fail "No tunnel address after 60 s; see $LOG"
echo "Tunnel: $TUNNEL"

say "3/5 Checking the API through the tunnel"
# A new quick tunnel's hostname takes a while to resolve everywhere: keep
# asking for up to two minutes before giving up.
tunnel_health="000"
for attempt in $(seq 1 40); do
  if tunnel_health=$(health "$TUNNEL"); then break; fi
  echo "  waiting for the tunnel (attempt $attempt/40, HTTP $tunnel_health)…"
  sleep 3
done
[[ "$tunnel_health" == "200" ]] || fail "GET $TUNNEL/health answered HTTP $tunnel_health after 2 minutes; see $LOG"
echo "health: GET $TUNNEL/health → 200 {\"status\":\"ok\"}"
image=$(curl -sf "$TUNNEL/api/v1/products/?limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["image_url"] or "")')
[[ "$image" == "$TUNNEL/media/"* ]] || fail "Product photo URL is not on the tunnel: $image"
echo "photo URL: $image"
image_status=$(curl -s -o /dev/null -w '%{http_code} %{content_type}' "$image")
[[ "$image_status" == 200\ image/* ]] || fail "Photo did not load through the tunnel: $image_status"
echo "photo: $image_status"
cors=$(curl -s -o /dev/null -D - -X OPTIONS "$TUNNEL/api/v1/products/" \
  -H "Origin: $SITE_URL" -H 'Access-Control-Request-Method: GET' | tr -d '\r' | grep -i '^access-control-allow-origin:' || true)
[[ "$cors" == *"$SITE_URL"* ]] || fail "CORS does not allow $SITE_URL — add it to CORS_ORIGINS in saas_backend/.env and restart uvicorn."
echo "CORS: $cors"

if [[ "$DEPLOY" == "1" ]]; then
  say "4/5 Vercel environment and production deploy"
  cd "$FRONTEND_DIR"
  for name in NEXT_PUBLIC_API_URL NEXT_PUBLIC_SITE_URL; do
    npx vercel env rm "$name" production --yes >/dev/null 2>&1 || true
  done
  printf '%s' "$TUNNEL/api/v1" | npx vercel env add NEXT_PUBLIC_API_URL production >/dev/null
  printf '%s' "$SITE_URL" | npx vercel env add NEXT_PUBLIC_SITE_URL production >/dev/null
  echo "NEXT_PUBLIC_API_URL=$TUNNEL/api/v1"
  echo "NEXT_PUBLIC_SITE_URL=$SITE_URL"
  npx vercel --prod --yes
fi

say "5/5 Tunnel is open: $TUNNEL  (Ctrl+C to close)"
wait "$TUNNEL_PID"

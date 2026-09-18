#!/bin/sh
set -e
cd /opt/gms-worker

if [ -e docker-compose.yml ]; then
  docker compose down
fi

PROXY_LIST_URL="https://proxy.webshare.io/api/v2/proxy/list/download/rrwjcxkqniigvnppgpytbsrvarhysisibeqyniax/-/any/username/direct/-/?plan_id=14299704"

# Webshare download format per line: ip:port:username:password
# tr strips any stray \r (Windows-style line endings) before awk splits fields,
# otherwise a trailing \r on the password field shows up as a phantom space before @
PROXIES="$(curl -fsSL "$PROXY_LIST_URL" \
  | tr -d '\r' \
  | awk -F: 'NF>=4 {printf "socks5://%s:%s@%s:%s,", $3, $4, $1, $2}' \
  | sed 's/,$//')"

if [ -z "$PROXIES" ]; then
  echo "No proxies fetched, aborting deploy" >&2
  exit 1
fi

cat > docker-compose.yml << COMPOSE_EOF
services:
  worker:
    image: ghcr.io/ghcr.io/gosom/google-maps-scraper-saas:latest
    restart: unless-stopped
    env_file:
      - .env
    command: ["worker", "-proxies", "$PROXIES"]
COMPOSE_EOF

docker compose up -d

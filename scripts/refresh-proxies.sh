#!/bin/sh
# Refresh worker proxy list from Webshare.
# Reads PROXY_LIST_URL from .env (required).
# Stops the worker container, fetches the latest proxy list, rewrites docker-compose.yml,
# and restarts the worker.
#
# Meant to be run on the worker host via ssh.
#
set -e

PROXY_LIST_URL="${PROXY_LIST_URL:?PROXY_LIST_URL env var is required}"

echo "Stopping worker container..."
docker compose -f /opt/gms-worker/docker-compose.yml down

echo "Fetching proxy list from $PROXY_LIST_URL..."
# Webshare download format per line: ip:port:username:password
# tr strips any stray \r (Windows-style line endings) before awk splits fields,
# otherwise a trailing \r on the password field shows up as a phantom space before @
RAW="$(curl -fsSL "$PROXY_LIST_URL")"

if [ -z "$RAW" ]; then
  echo "Proxy list fetch returned empty response" >&2
  exit 1
fi

# Convert each line to socks5://username:password@ip:port and join with commas
PROXIES="$(echo "$RAW" | tr -d '\r' | awk -F':' 'NF>=4 {printf "socks5://%s:%s@%s:%s,", $3, $4, $1, $2}' | sed 's/,$//')"

if [ -z "$PROXIES" ]; then
  echo "No proxies parsed from list" >&2
  exit 1
fi

PROXY_COUNT=$(echo "$PROXIES" | tr ',' '\n' | wc -l)
echo "Fetched $PROXY_COUNT proxies; updating docker-compose.yml..."

# Rewrite docker-compose.yml with new proxy list
cat > /opt/gms-worker/docker-compose.yml << COMPOSE_EOF
services:
  worker:
    image: ghcr.io/ghcr.io/gosom/google-maps-scraper-saas:latest
    restart: unless-stopped
    env_file:
      - .env
    command: ["worker", "-proxies", "$PROXIES"]
COMPOSE_EOF

echo "Starting worker container..."
docker compose -f /opt/gms-worker/docker-compose.yml up -d

echo "Proxy refresh complete."

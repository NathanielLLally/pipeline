#!/bin/sh
set -e

PROXY_LIST_URL="${PROXY_LIST_URL:?PROXY_LIST_URL env var is required}"

# Webshare download format per line: ip:port:username:password
RAW="$(curl -fsSL "$PROXY_LIST_URL")"

if [ -z "$RAW" ]; then
  echo "Proxy list fetch returned empty response" >&2
  exit 1
fi

# Convert each line to socks5://username:password@ip:port and join with commas
export PROXIES="$(echo "$RAW" | awk -F':' 'NF>=4 {printf "socks5://%s:%s@%s:%s,", $3, $4, $1, $2}' | sed 's/,$//')"

if [ -z "$PROXIES" ]; then
  echo "No proxies parsed from list" >&2
  exit 1
fi

echo "Fetched $(echo "$PROXIES" | tr ',' '\n' | wc -l) proxies"


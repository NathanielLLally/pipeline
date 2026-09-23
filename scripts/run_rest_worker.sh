#!/bin/bash
# Deploys the scraping worker to every host in the fleet.
#
# bash, not sh: SCRAPER_SSH_HOSTS in .env is a bash array literal `(h1 h2 h3)`, which a
# POSIX shell cannot parse at all -- sourcing .env under dash is a syntax error, not a
# degraded read. Same form scripts/run_rest_worker_ssh.sh and scripts/worker-watchdog.sh
# already consume, so the fleet is defined in exactly one place.

set -uo pipefail

# Resolve the repo root from this script's own location. The previous version used $PWD,
# which silently scp'd nothing when invoked from anywhere but the repo root.
TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

set -a && source "$TOP/.env" && set +a

#  when the job is done, the worker vps should be destroyed
#  the database server should also be destroyed, and the pgsql/data should be on a persistent volume
#
#  this means that provision is responsible for spinning up a container based on an image or snapshot or whatever
#  then starting the docker serve from compose as root like gmapsaas provision does

#===== WORKER HOSTS =====
# Falls back to the singular SCRAPER_SSH_HOST for older .env files that predate the
# array, and tolerates a plain space- or comma-separated string.
if declare -p SCRAPER_SSH_HOSTS 2>/dev/null | grep -q '^declare -a'; then
  WORKER_HOSTS=("${SCRAPER_SSH_HOSTS[@]}")
elif [ -n "${SCRAPER_SSH_HOSTS:-}" ]; then
  read -r -a WORKER_HOSTS <<< "${SCRAPER_SSH_HOSTS//,/ }"
elif [ -n "${SCRAPER_SSH_HOST:-}" ]; then
  WORKER_HOSTS=("$SCRAPER_SSH_HOST")
else
  echo "error: neither SCRAPER_SSH_HOSTS nor SCRAPER_SSH_HOST is set in .env" >&2
  exit 1
fi

SSH_PORT="${SCRAPER_SSH_PORT:-22}"
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p "$SSH_PORT")

echo "deploying worker to ${#WORKER_HOSTS[@]} host(s): ${WORKER_HOSTS[*]}"

# One host failing must not abort the rest of the fleet -- a half-deployed fleet with a
# named failure is more useful than an abort at host 2 of 5 that says nothing about 3-5.
# Hence no `set -e`, and a per-host status collected for the summary.
failed=()

for host in "${WORKER_HOSTS[@]}"; do
  echo
  echo "=== $host ==="

  # ssh to vps host, run scraping worker process from docker container
  if ! ssh "${SSH_OPTS[@]}" "$SCRAPER_SSH_USER@$host" \
      "sudo sh -c 'if [ ! -d /opt/gms-worker ]; then mkdir /opt/gms-worker; cp /opt/gms-server/.env /opt/gms-worker; fi;'"; then
    echo "  FAILED: could not prepare /opt/gms-worker" >&2
    failed+=("$host (prepare)")
    continue
  fi

  # Push the refresh script up (avoids nested heredoc/escaping issues of embedding it inline)
  if ! scp -P "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      "$TOP/refresh-proxies.sh" "$SCRAPER_SSH_USER@$host:/tmp/refresh-proxies.sh"; then
    echo "  FAILED: could not copy refresh-proxies.sh" >&2
    failed+=("$host (scp)")
    continue
  fi

  if ! ssh "${SSH_OPTS[@]}" "$SCRAPER_SSH_USER@$host" \
      "sudo mv /tmp/refresh-proxies.sh /opt/gms-worker/refresh-proxies.sh && sudo chmod +x /opt/gms-worker/refresh-proxies.sh"; then
    echo "  FAILED: could not install refresh-proxies.sh" >&2
    failed+=("$host (install)")
    continue
  fi

  # Fetches the current proxy list, writes docker-compose.yml with -proxies baked in,
  # and runs docker compose up -d
  if ! ssh "${SSH_OPTS[@]}" "$SCRAPER_SSH_USER@$host" "sudo /opt/gms-worker/refresh-proxies.sh"; then
    echo "  FAILED: refresh-proxies.sh returned non-zero" >&2
    failed+=("$host (refresh)")
    continue
  fi

  echo "  ok"
done

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "all ${#WORKER_HOSTS[@]} host(s) deployed"
else
  echo "deployed $(( ${#WORKER_HOSTS[@]} - ${#failed[@]} ))/${#WORKER_HOSTS[@]}; failed: ${failed[*]}" >&2
  exit 1
fi

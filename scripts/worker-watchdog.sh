#!/usr/bin/env bash
# Worker watchdog: monitor the scraper worker and trigger restart/proxy-refresh on degradation.
#
# One tick per invocation (~60s via systemd timer). Collects metrics from the DB,
# probes the SSH host, decides on a verdict, and acts if conditions warrant.
#
# Usage:
#   scripts/worker-watchdog.sh [--dry-run] [--verbose]
#
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

# Load env vars
set -a
source .env
set +a

# Config
DRY_RUN=0
VERBOSE=0

while (($# > 0)); do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --verbose)  VERBOSE=1; shift ;;
    *)          echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Logging
log_msg() { echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog: $*" >&2; }
log_verbose() { [ $VERBOSE -eq 1 ] && log_msg "$@" || true; }

#===== COLLECT METRICS =====
collect_metrics() {
  log_verbose "Collecting metrics from $LEADS_DB_HOST..."
  /usr/bin/psql -X "$LEADS_DB_URL" -A -t -v ON_ERROR_STOP=1 << 'SQL'
WITH metrics AS (
  SELECT
    (SELECT count(*) FILTER (WHERE state IN ('available', 'scheduled') AND scheduled_at <= now())
      FROM river_job) AS backlog,
    COALESCE(
      GREATEST(
        (SELECT max(finalized_at) FROM river_job),
        (SELECT max(created_at) FROM scrape_results)
      ),
      now()
    ) AS last_progress,
    COALESCE(
      (SELECT extract(epoch FROM (now() - max(attempted_at))) FROM river_job WHERE state = 'running'),
      0
    )::bigint AS longest_running_sec,
    (SELECT count(*) FILTER (WHERE result_count = 0) FROM (
      SELECT result_count FROM scrape_results ORDER BY created_at DESC LIMIT 10
    ) sub)::float / 10.0 AS zero_yield_ratio,
    (SELECT count(*) FROM river_job
      WHERE state = 'discarded' AND finalized_at > now() - interval '20 minutes'
        AND errors::text LIKE '%job timed out with no results%') AS recent_timeouts
)
SELECT row_to_json(metrics) FROM metrics;
SQL
}

#===== PROBE HOST =====
probe_host() {
  log_verbose "Probing worker host..."
  ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$REST_SSH_PORT" "$REST_SSH_USER@$REST_SSH_HOST" \
    'docker inspect gms-worker-worker-1 --format "{{.State.Status}}"' 2>/dev/null || echo "unreachable"
}

#===== DECIDE VERDICT =====
decide() {
  local backlog=$1 last_progress=$2 longest_running_sec=$3 zero_yield_ratio=$4 recent_timeouts=$5 host_state=$6

  # Priority: HOST_UNREACHABLE > IDLE > ... > HEALTHY
  [ "$host_state" = "unreachable" ] && echo "HOST_UNREACHABLE" && return 0
  [ "$backlog" -eq 0 ] && echo "IDLE" && return 0

  # Check for degradation patterns
  if (( $(echo "$zero_yield_ratio >= 0.5" | bc -l) )) || [ "$recent_timeouts" -ge 3 ]; then
    echo "PROXY_DEGRADED"
    return 0
  fi

  if [ "$longest_running_sec" -gt 600 ]; then
    echo "WEDGED"
    return 0
  fi

  if [ "$longest_running_sec" -eq 0 ] && [ "$(date -d "$last_progress" +%s 2>/dev/null || echo 0)" -lt $(( $(date +%s) - 300 )) ]; then
    echo "STALLED"
    return 0
  fi

  echo "HEALTHY"
}

#===== MAIN =====
main() {
  log_verbose "Starting watchdog tick (DRY_RUN=$DRY_RUN)"

  # Collect metrics
  local metrics_json
  if ! metrics_json=$(collect_metrics); then
    log_msg "ERROR: Failed to collect metrics"
    exit 1
  fi

  log_verbose "Metrics: $metrics_json"

  # Extract metric values
  local backlog last_progress longest_running_sec zero_yield_ratio recent_timeouts
  backlog=$(echo "$metrics_json" | jq -r '.backlog // 0')
  last_progress=$(echo "$metrics_json" | jq -r '.last_progress')
  longest_running_sec=$(echo "$metrics_json" | jq -r '.longest_running_sec // 0')
  zero_yield_ratio=$(echo "$metrics_json" | jq -r '.zero_yield_ratio // 0')
  recent_timeouts=$(echo "$metrics_json" | jq -r '.recent_timeouts // 0')

  # Probe host
  local host_state
  host_state=$(probe_host) || host_state="unreachable"

  # Decide verdict
  local verdict
  verdict=$(decide "$backlog" "$last_progress" "$longest_running_sec" "$zero_yield_ratio" "$recent_timeouts" "$host_state")

  log_msg "Verdict: $verdict (backlog=$backlog, running=${longest_running_sec}s, zero_yield=$zero_yield_ratio, timeouts=$recent_timeouts)"

  # TODO: Implement action logic with hysteresis and cooldowns
  # For now, just record the verdict
  local action="null"
  local action_ok="false"
  local action_note=""

  # Record result
  /usr/bin/psql -X "$LEADS_DB_URL" -v ON_ERROR_STOP=1 << RECORD_SQL
INSERT INTO leads.worker_health_log (verdict, signals, action, action_ok, action_note)
VALUES (
  '$verdict',
  '$metrics_json'::jsonb,
  $( [ "$action" = "null" ] && echo "NULL" || echo "'$action'" ),
  $( [ "$action_ok" = "true" ] && echo "true" || echo "false" ),
  $( [ -n "$action_note" ] && echo "'$(echo "$action_note" | sed "s/'/''/g")'" || echo "NULL" )
);
RECORD_SQL

  log_verbose "Tick complete"
}

main

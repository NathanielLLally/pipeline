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

#===== WORKER HOSTS =====
# The scraping fleet, from SCRAPER_SSH_HOSTS in .env -- a bash array literal,
# `(host1 host2 host3)`, the same form scripts/run_rest_worker_ssh.sh consumes.
#
# REST_SSH_HOST runs the API/River job queue that collect_metrics() reads. On the
# current deployment it is *also* the first entry of SCRAPER_SSH_HOSTS -- one machine
# hosting both the queue and a scraper -- so the two are overlapping roles, not
# separate hosts. Do not assume otherwise: a restart aimed at the fleet does touch the
# queue host, and the queue being healthy still says nothing about whether the workers
# are draining it, which is exactly the failure phase2_ny.log recorded.
#
# Falls back to the single SCRAPER_SSH_HOST for older .env files that predate the
# array.
if declare -p SCRAPER_SSH_HOSTS 2>/dev/null | grep -q '^declare -a'; then
  WORKER_HOSTS=("${SCRAPER_SSH_HOSTS[@]}")
elif [ -n "${SCRAPER_SSH_HOSTS:-}" ]; then
  # Tolerate a plain string, space- or comma-separated.
  read -r -a WORKER_HOSTS <<< "${SCRAPER_SSH_HOSTS//,/ }"
elif [ -n "${SCRAPER_SSH_HOST:-}" ]; then
  WORKER_HOSTS=("$SCRAPER_SSH_HOST")
else
  WORKER_HOSTS=()
fi

# ssh with a hard timeout: a watchdog that blocks on a dead host is not a watchdog.
# BatchMode refuses password prompts rather than hanging for input.
worker_ssh() {
  local host=$1; shift
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      -p "${SCRAPER_SSH_PORT:-22}" "${SCRAPER_SSH_USER}@${host}" "$@"
}

#===== PROBE HOST =====
probe_host() {
  log_verbose "Probing worker host..."
  ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$REST_SSH_PORT" "$REST_SSH_USER@$REST_SSH_HOST" \
    'docker inspect gms-worker-worker-1 --format "{{.State.Status}}"' 2>/dev/null || echo "unreachable"
}

# Per-host container state across the whole fleet. Emits "host=state" per line so a
# partial outage is visible instead of being collapsed into one verdict.
probe_workers() {
  local host state
  for host in "${WORKER_HOSTS[@]}"; do
    state=$(worker_ssh "$host" 'docker inspect gms-worker-worker-1 --format "{{.State.Status}}"' 2>/dev/null) \
      || state="unreachable"
    echo "${host}=${state:-unreachable}"
  done
}

#===== DECIDE VERDICT =====
# Which hosts are not serving, as "host=state" lines. A container that is not
# `running` and a host that cannot be reached are both unavailable for work; they
# differ only in whether a restart can fix it, which choose_action sorts out.
degraded_hosts() {
  local states=$1
  printf '%s\n' "$states" | awk -F= 'NF==2 && $2 != "running" { print $1 }'
}

decide() {
  local backlog=$1 last_progress=$2 longest_running_sec=$3 zero_yield_ratio=$4 recent_timeouts=$5 host_state=$6
  local worker_states=${7:-}

  # Fleet health first, because the queue-level metrics cannot see it. A worker whose
  # container has died stops drawing jobs silently: the survivors keep draining the
  # queue, so backlog, progress and yield all still look fine. That is exactly how two
  # of three workers sat dead through a scraping run while every tick recorded IDLE or
  # HEALTHY and the run timed out job after job.
  local total down
  total=$(printf '%s\n' "$worker_states" | grep -c '=' || true)
  down=$(degraded_hosts "$worker_states" | grep -c . || true)

  if [ "$total" -gt 0 ] && [ "$down" -gt 0 ]; then
    # Everything down is a different problem from a partial outage: no restart loop is
    # going to help if the whole fleet vanished at once, and it usually means the
    # network or the provider rather than the containers.
    [ "$down" -ge "$total" ] && echo "FLEET_DOWN" && return 0
    echo "WORKER_DEGRADED"
    return 0
  fi

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

#===== ACTION POLICY =====
# Hysteresis and cooldowns, both derived from leads.worker_health_log rather than a
# local state file, so the policy survives a reinstall of the timer and is auditable
# after the fact.
#
#   Hysteresis: a verdict must persist across CONSECUTIVE_TICKS_REQUIRED ticks before
#   anything is done. One bad tick is usually a slow page, not a wedged worker, and a
#   watchdog that restarts on noise is worse than none -- it destroys in-flight jobs.
#
#   Cooldown: after acting, wait out the window before acting again. A restart takes
#   time to show up in the metrics; without this the watchdog would restart a
#   recovering worker repeatedly and never let it finish.
CONSECUTIVE_TICKS_REQUIRED=3
RESTART_COOLDOWN_MIN=15
PROXY_COOLDOWN_MIN=30

# How many of the most recent ticks (this one included) carry this verdict.
consecutive_verdict_count() {
  local verdict=$1
  /usr/bin/psql -X "$LEADS_DB_URL" -A -t -v ON_ERROR_STOP=1 -v v="$verdict" <<'SQL' 2>/dev/null || echo 0
SELECT count(*) FROM (
  SELECT verdict, row_number() OVER (ORDER BY checked_at DESC) AS rn
  FROM leads.worker_health_log
  ORDER BY checked_at DESC LIMIT 10
) t
WHERE rn <= (
  SELECT COALESCE(min(rn), 11) - 1 FROM (
    SELECT verdict, row_number() OVER (ORDER BY checked_at DESC) AS rn
    FROM leads.worker_health_log ORDER BY checked_at DESC LIMIT 10
  ) u WHERE u.verdict IS DISTINCT FROM :'v'
) AND verdict = :'v';
SQL
}

# Minutes since this action last succeeded; large sentinel when it never has.
minutes_since_action() {
  local action=$1
  /usr/bin/psql -X "$LEADS_DB_URL" -A -t -v ON_ERROR_STOP=1 -v a="$action" <<'SQL' 2>/dev/null || echo 99999
SELECT COALESCE(
  round(extract(epoch FROM (now() - max(checked_at))) / 60)::bigint,
  99999)
FROM leads.worker_health_log
WHERE action = :'a' AND action_ok;
SQL
}

# Maps a verdict to an action, applying hysteresis and cooldown.
# Echoes: none | restart | proxy_refresh | suppressed:<reason>
choose_action() {
  local verdict=$1 want="" cooldown=0

  case "$verdict" in
    PROXY_DEGRADED)            want="proxy_refresh"; cooldown=$PROXY_COOLDOWN_MIN ;;
    WEDGED|STALLED)            want="restart";       cooldown=$RESTART_COOLDOWN_MIN ;;
    # A dead container is the one failure a restart reliably fixes, and it is repaired
    # per-host: the healthy workers are left alone rather than bounced along with it.
    WORKER_DEGRADED)           want="restart";       cooldown=$RESTART_COOLDOWN_MIN ;;
    # Whole fleet gone at once. A restart loop across every host is unlikely to help
    # and would hammer machines that may be having a network or provider problem.
    FLEET_DOWN)                echo "suppressed:entire fleet down; needs human attention"; return 0 ;;
    # An unreachable REST host is a network or provider problem; restarting workers
    # cannot fix it and the SSH needed to try is the very thing that is failing.
    HOST_UNREACHABLE)          echo "suppressed:host unreachable; needs human attention"; return 0 ;;
    *)                         echo "none"; return 0 ;;
  esac

  if [ "${#WORKER_HOSTS[@]}" -eq 0 ]; then
    echo "suppressed:no worker hosts configured (set SCRAPER_SSH_HOSTS)"
    return 0
  fi

  local streak; streak=$(consecutive_verdict_count "$verdict")
  streak=${streak:-0}
  if [ "$streak" -lt "$CONSECUTIVE_TICKS_REQUIRED" ]; then
    echo "suppressed:$verdict seen ${streak}/${CONSECUTIVE_TICKS_REQUIRED} consecutive ticks"
    return 0
  fi

  local since; since=$(minutes_since_action "$want")
  since=${since:-99999}
  if [ "$since" -lt "$cooldown" ]; then
    echo "suppressed:$want in cooldown (${since}m of ${cooldown}m)"
    return 0
  fi

  echo "$want"
}

#===== ACTIONS =====
# Both actions run on every worker host and report per-host outcomes. The command
# succeeds if at least one host recovered: a two-of-three fleet still drains the
# queue, and failing the whole action would suppress the log line that says so.
do_action() {
  local action=$1
  shift
  # Optional explicit target list. A WORKER_DEGRADED restart passes only the hosts whose
  # container is actually down, so a healthy worker mid-job is never bounced to repair
  # its neighbour. With no list, the action applies to the whole fleet.
  local -a targets=("$@")
  [ "${#targets[@]}" -eq 0 ] && targets=("${WORKER_HOSTS[@]}")
  local host rc=0 ok=0 fail=0 notes=""

  for host in "${targets[@]}"; do
    case "$action" in
      restart)
        # `docker compose up -d` after a down, rather than `restart`, so a container
        # that died with a stale config comes back on the current compose file.
        if worker_ssh "$host" 'sudo sh -c "cd /opt/gms-worker && docker compose down && docker compose up -d"' >/dev/null 2>&1; then
          ok=$((ok+1)); notes+="${host}:restarted "
        else
          fail=$((fail+1)); notes+="${host}:FAILED "
        fi ;;
      proxy_refresh)
        # refresh-proxies.sh re-fetches the Webshare list and rewrites the compose
        # file, so it subsumes a restart.
        if worker_ssh "$host" 'sudo /opt/gms-worker/refresh-proxies.sh' >/dev/null 2>&1; then
          ok=$((ok+1)); notes+="${host}:proxies-refreshed "
        else
          fail=$((fail+1)); notes+="${host}:FAILED "
        fi ;;
    esac
  done

  echo "${ok} ok, ${fail} failed -- ${notes}"
  [ "$ok" -gt 0 ] || rc=1
  return $rc
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

  # Probe the REST host (queue) and, separately, each worker in the fleet.
  local host_state
  host_state=$(probe_host) || host_state="unreachable"

  local worker_states worker_json
  worker_states=$(probe_workers || true)
  # "host=state" lines -> {"host":"state",...}, folded into the recorded signals so
  # the log shows which workers were up when a verdict was reached.
  worker_json=$(printf '%s\n' "$worker_states" | jq -R -s -c '
    split("\n") | map(select(length > 0) | split("=") | {(.[0]): .[1]}) | add // {}')
  metrics_json=$(jq -c --argjson w "${worker_json:-{\}}" --arg h "$host_state" \
    '. + {worker_states: $w, rest_host_state: $h}' <<< "$metrics_json")
  log_verbose "Worker states: $worker_json"

  # Decide verdict
  local verdict
  verdict=$(decide "$backlog" "$last_progress" "$longest_running_sec" "$zero_yield_ratio" \
                   "$recent_timeouts" "$host_state" "$worker_states")

  # A WORKER_DEGRADED restart is aimed only at the hosts that are actually down.
  local -a targets=()
  if [ "$verdict" = "WORKER_DEGRADED" ]; then
    mapfile -t targets < <(degraded_hosts "$worker_states")
  fi

  log_msg "Verdict: $verdict (backlog=$backlog, running=${longest_running_sec}s, zero_yield=$zero_yield_ratio, timeouts=$recent_timeouts${targets[0]:+, down=${targets[*]}})"

  # Decide and perform the action for this verdict.
  local action="null" action_ok="false" action_note=""
  local decision
  decision=$(choose_action "$verdict")

  case "$decision" in
    none)
      : ;;
    suppressed:*)
      action_note="${decision#suppressed:}"
      log_verbose "No action: $action_note" ;;
    restart|proxy_refresh)
      action="$decision"
      local -a scope=("${targets[@]}")
      [ "${#scope[@]}" -eq 0 ] && scope=("${WORKER_HOSTS[@]}")
      if [ "$DRY_RUN" -eq 1 ]; then
        action_ok="false"
        action_note="dry-run: would have run $action on ${#scope[@]} host(s): ${scope[*]}"
        log_msg "$action_note"
      else
        local out rc=0
        out=$(do_action "$action" "${scope[@]}" 2>&1) || rc=$?
        [ "$rc" -eq 0 ] && action_ok="true" || action_ok="false"
        action_note="$out"
        log_msg "Action $action -> ok=$action_ok: $out"
      fi ;;
  esac

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

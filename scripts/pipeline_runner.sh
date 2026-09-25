#!/usr/bin/env bash
# Pipeline Runner: runs the full service-category set (or one, via --service) across
# all metros in queries/metros.json.
#
# This is phase2-all-metros.sh generalized: instead of four hardcoded TRACK_A..D
# arrays with one is_completed() SQL query per (phase, service, metro) triple, the
# set of tracks is derived from queries/service_terms.json (optionally narrowed by
# --service), and coverage is read in bulk from `coverage-check.sh --tsv` rather than
# ad-hoc psql calls embedded in this script. coverage-check.sh is the single place
# that knows how to query leads.search_log for pipeline/phase status going forward.
#
# Every batch here runs with phase=adhoc; the prompt1/prompt2 distinction phase2-all-metros.sh
# hardcoded per track does not generalize to an arbitrary service list, and batches are
# always attributed to a phase label in leads.search_log/leads.businesses regardless.
#
# Usage:
#   ./scripts/pipeline_runner.sh [--sequential] [--force-all] [--workers N] [--service NAME]
#
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

# Load env
set -a
source $TOP/.env
set +a

# Config
SEQUENTIAL=0
FORCE_ALL=0
WORKERS=3
TARGET_SERVICE=""
readonly PHASE="adhoc"

while (($# > 0)); do
  case "$1" in
    --sequential)  SEQUENTIAL=1; shift ;;
    --parallel)    SEQUENTIAL=0; shift ;;
    --force-all)   FORCE_ALL=1; shift ;;
    --workers)
      WORKERS="${2:-3}"
      shift
      (($# > 0)) && shift
      ;;
    --service)
      TARGET_SERVICE="${2:-}"
      shift
      (($# > 0)) && shift
      ;;
    *)             echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Determine which services to run. --service narrows to one (and, matching
# phase2-all-metros.sh's prior behavior, forces sequential mode since there's only
# one track); with no --service, every service in queries/service_terms.json runs.
if [ -n "$TARGET_SERVICE" ]; then
  if jq -e ".[\"$TARGET_SERVICE\"]" queries/service_terms.json >/dev/null 2>&1; then
    echo "service: [$TARGET_SERVICE]"
    SERVICES=("$TARGET_SERVICE")
    SEQUENTIAL=1
  else
    echo "Invalid service: $TARGET_SERVICE"
    echo "Available services in queries/service_terms.json:"
    jq 'keys' queries/service_terms.json
    exit 1
  fi
else
  mapfile -t SERVICES < <(jq -r 'keys[]' queries/service_terms.json)
fi

# Logging
LOG_DIR="phase2-logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/pipeline-runner.log"

log_msg() {
  local msg="$(date '+%Y-%m-%d %H:%M:%S') — $*"
  echo "$msg" | tee -a "$MASTER_LOG"
}

# Read metros from JSON (alphabetical order)
read_metros() {
  cat queries/metros.json | jq -r '.metros[].metro' | sort
}

# Run a single pipeline
run_pipeline() {
  local phase=$1 service=$2 metro=$3 log_file=$4

  log_msg "[START] $phase/$service/$metro"

  if ./scripts/pipeline.sh \
    --phase "$phase" \
    --service-category "$service" \
    --metro "$metro" \
    --workers "$WORKERS" \
    >> "$log_file" 2>&1; then

    local result=$(tail -1 "$log_file" | grep -o "Batch [^ ]* complete" || echo "completed")
    log_msg "[DONE] $phase/$service/$metro — $result"
    return 0
  else
    log_msg "[FAIL] $phase/$service/$metro — see $log_file"
    return 1
  fi
}

#=============================================================================
# MAIN
#=============================================================================

log_msg "=========================================="
log_msg "Pipeline Runner: All Services x All Metros (Dynamic)"
log_msg "=========================================="

# Read metros from JSON
mapfile -t METROS < <(read_metros)
log_msg "Found ${#METROS[@]} metros from queries/metros.json"
log_msg "Services: ${SERVICES[*]}"
log_msg ""

# Pull coverage in one shot from coverage-check.sh instead of one ad-hoc psql query
# per (service, metro) pair. --tsv gives geo_target-LIKE-matched counts per
# (metro, service_category); gen-queries.mjs still does the fine-grained per-keyword
# dedup inside pipeline.sh, so this is only used to decide which metros are worth
# queuing a batch for at all.
log_msg "Checking coverage via coverage-check.sh --tsv..."
declare -A COVERAGE
while IFS=$'\t' read -r cov_metro cov_service cov_count; do
  [ -z "$cov_metro" ] && continue
  COVERAGE["$cov_metro|$cov_service"]="$cov_count"
done < <(./scripts/coverage-check.sh --tsv)

# Build one PENDING_<service> array per service, derived from COVERAGE rather than
# hardcoded TRACK_A..D arrays.
for service in "${SERVICES[@]}"; do
  declare -a "PENDING_${service}=()"
done

for metro in "${METROS[@]}"; do
  for service in "${SERVICES[@]}"; do
    count="${COVERAGE["$metro|$service"]:-0}"
    if [ $FORCE_ALL -eq 1 ] || [ "$count" -eq 0 ]; then
      declare -n pending="PENDING_${service}"
      pending+=("$metro")
      unset -n pending
    fi
  done
done

for service in "${SERVICES[@]}"; do
  declare -n pending="PENDING_${service}"
  log_msg "$service pending: ${#pending[@]} metros"
  unset -n pending
done
log_msg ""
log_msg "Mode: $([ $SEQUENTIAL -eq 1 ] && echo "SEQUENTIAL" || echo "PARALLEL")"
log_msg "Workers per batch: $WORKERS"
log_msg "Force all: $FORCE_ALL"
log_msg "Phase: $PHASE"
log_msg ""

if [ $SEQUENTIAL -eq 1 ]; then
  for service in "${SERVICES[@]}"; do
    declare -n pending="PENDING_${service}"
    log_msg "=== TRACK: $service ==="
    for metro in "${pending[@]}"; do
      run_pipeline "$PHASE" "$service" "$metro" "$LOG_DIR/track-${service}-$(echo "$metro" | tr ' ,' '-').log"
    done
    unset -n pending
    log_msg ""
  done

else
  # Parallel: interleave by metro, across every service's pending list.
  log_msg "Parallel execution: interleaved by metro"
  log_msg ""

  max_metros=0
  for service in "${SERVICES[@]}"; do
    declare -n pending="PENDING_${service}"
    (( ${#pending[@]} > max_metros )) && max_metros=${#pending[@]}
    unset -n pending
  done

  for ((i = 0; i < max_metros; i++)); do
    for service in "${SERVICES[@]}"; do
      declare -n pending="PENDING_${service}"
      if [ $i -lt ${#pending[@]} ]; then
        metro=${pending[$i]}
        run_pipeline "$PHASE" "$service" "$metro" "$LOG_DIR/track-${service}-$(echo "$metro" | tr ' ,' '-').log" &
        sleep 2  # Stagger submissions
      fi
      unset -n pending
    done
  done

  log_msg "All batches queued; waiting for completion..."
  wait

fi

log_msg ""
log_msg "=========================================="
log_msg "Pipeline Runner Complete"
log_msg "=========================================="
log_msg "Logs: $LOG_DIR/"
log_msg ""
log_msg "=== Final Coverage ==="
./scripts/coverage-check.sh --stats | tee -a "$MASTER_LOG"

log_msg ""
log_msg "Master log: $MASTER_LOG"

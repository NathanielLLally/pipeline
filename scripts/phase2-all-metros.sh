#!/usr/bin/env bash
# Phase 2 Pipeline: Dog Training Phase 2 + Daycare/Boarding/Grooming/Walking Phase 1 across all metros
#
# Dynamically reads metros from queries/metros.json
# Checks database to skip already-completed batches
# Executes all tracks in parallel
#
# Usage:
#   ./scripts/phase2-all-metros.sh [--sequential] [--force-all] [--workers N]
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

  if jq -e ".[\"$TARGET_SERVICE\"]" queries/service_terms.json >/dev/null 2>&1; then
    echo "service: [$TARGET_SERVICE]"
    # Valid service, set mode to sequential and only run this service
    SEQUENTIAL=1
    :
  else
    echo "Invalid service: $TARGET_SERVICE"
    echo "Available services in queries/service_terms.json:"
    jq 'keys' queries/service_terms.json
    exit 1
  fi

# Logging
LOG_DIR="phase2-logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/phase2-all.log"

log_msg() {
  local msg="$(date '+%Y-%m-%d %H:%M:%S') — $*"
  echo "$msg" | tee -a "$MASTER_LOG"
}

# Read metros from JSON (alphabetical order)
read_metros() {
  cat queries/metros.json | jq -r '.metros[].metro' | sort
}

# Check if batch already completed
is_completed() {
  local phase=$1 service=$2 metro=$3

  if [ $FORCE_ALL -eq 1 ]; then
    return 1  # Force all = nothing is completed
  fi

  # Query database: if this (phase, service, metro) exists in search_log, it's done
  local count=$(/usr/bin/psql -X "$LEADS_DB_URL" -t -A -c "
    SELECT COUNT(*) FROM leads.search_log
    WHERE phase = '$phase' AND service_category = '$service' AND geo_target LIKE '%${metro}%'
  " 2>/dev/null || echo "0")

  [ "$count" -gt 0 ]
}

# Run a single pipeline
run_pipeline() {
  local phase=$1 service=$2 metro=$3 log_file=$4

  if is_completed "$phase" "$service" "$metro"; then
    log_msg "[SKIP] $phase/$service/$metro (already in DB)"
    return 0
  fi

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
log_msg "Phase 2 Pipeline: All Metros (Dynamic)"
log_msg "=========================================="

# Read metros from JSON
mapfile -t METROS < <(read_metros)
log_msg "Found ${#METROS[@]} metros from queries/metros.json"
log_msg ""

# Determine which ones are NOT completed
PENDING_TRACK_A=()
PENDING_TRACK_B=()
PENDING_TRACK_C=()
PENDING_TRACK_D=()

log_msg "Checking database for completed batches..."
for metro in "${METROS[@]}"; do
  # Track A: prompt2/dog_training
  if [ -z "$TARGET_SERVICE" ] || [ "$TARGET_SERVICE" == "dog_training" ]; then
    if ! is_completed "prompt2" "dog_training" "$metro"; then
      PENDING_TRACK_A+=("$metro")
    fi
  fi

  # Track B: prompt1/daycare_boarding
  if [ -z "$TARGET_SERVICE" ] || [ "$TARGET_SERVICE" == "daycare_boarding" ]; then
    if ! is_completed "prompt1" "daycare_boarding" "$metro"; then
      PENDING_TRACK_B+=("$metro")
    fi
  fi

  # Track C: prompt1/grooming
  if [ -z "$TARGET_SERVICE" ] || [ "$TARGET_SERVICE" == "grooming" ]; then
    if ! is_completed "prompt1" "grooming" "$metro"; then
      PENDING_TRACK_C+=("$metro")
    fi
  fi

  # Track D: prompt1/dog_walking_petsitting
  if [ -z "$TARGET_SERVICE" ] || [ "$TARGET_SERVICE" == "dog_walking_petsitting" ]; then
    if ! is_completed "prompt1" "dog_walking_petsitting" "$metro"; then
      PENDING_TRACK_D+=("$metro")
    fi
  fi
done

log_msg "Track A pending: ${#PENDING_TRACK_A[@]} metros"
log_msg "Track B pending: ${#PENDING_TRACK_B[@]} metros"
log_msg "Track C pending: ${#PENDING_TRACK_C[@]} metros"
log_msg "Track D pending: ${#PENDING_TRACK_D[@]} metros"
log_msg ""
log_msg "Mode: $([ $SEQUENTIAL -eq 1 ] && echo "SEQUENTIAL" || echo "PARALLEL")"
log_msg "Workers per batch: $WORKERS"
log_msg "Force all: $FORCE_ALL"
log_msg ""

if [ $SEQUENTIAL -eq 1 ]; then
  log_msg "=== TRACK A: Dog Training Phase 2 ==="
  for metro in "${PENDING_TRACK_A[@]}"; do
    run_pipeline "prompt2" "dog_training" "$metro" "$LOG_DIR/track-a-$(echo "$metro" | tr ' ,' '-').log"
  done

  log_msg ""
  log_msg "=== TRACK B: Daycare + Boarding Phase 1 ==="
  for metro in "${PENDING_TRACK_B[@]}"; do
    run_pipeline "prompt1" "daycare_boarding" "$metro" "$LOG_DIR/track-b-$(echo "$metro" | tr ' ,' '-').log"
  done

  log_msg ""
  log_msg "=== TRACK C: Grooming Phase 1 ==="
  for metro in "${PENDING_TRACK_C[@]}"; do
    run_pipeline "prompt1" "grooming" "$metro" "$LOG_DIR/track-c-$(echo "$metro" | tr ' ,' '-').log"
  done

  log_msg ""
  log_msg "=== TRACK D: Dog Walking & Pet Sitting Phase 1 ==="
  for metro in "${PENDING_TRACK_D[@]}"; do
    run_pipeline "prompt1" "dog_walking_petsitting" "$metro" "$LOG_DIR/track-d-$(echo "$metro" | tr ' ,' '-').log"
  done

else
  # Parallel: interleave by metro
  log_msg "Parallel execution: interleaved by metro"
  log_msg ""

  max_metros=${#PENDING_TRACK_A[@]}
  [ ${#PENDING_TRACK_B[@]} -gt $max_metros ] && max_metros=${#PENDING_TRACK_B[@]}
  [ ${#PENDING_TRACK_C[@]} -gt $max_metros ] && max_metros=${#PENDING_TRACK_C[@]}
  [ ${#PENDING_TRACK_D[@]} -gt $max_metros ] && max_metros=${#PENDING_TRACK_D[@]}

  for ((i = 0; i < max_metros; i++)); do
    # Track A
    if [ $i -lt ${#PENDING_TRACK_A[@]} ]; then
      metro=${PENDING_TRACK_A[$i]}
      run_pipeline "prompt2" "dog_training" "$metro" "$LOG_DIR/track-a-$(echo "$metro" | tr ' ,' '-').log" &
      sleep 2  # Stagger submissions
    fi

    # Track B
    if [ $i -lt ${#PENDING_TRACK_B[@]} ]; then
      metro=${PENDING_TRACK_B[$i]}
      run_pipeline "prompt1" "daycare_boarding" "$metro" "$LOG_DIR/track-b-$(echo "$metro" | tr ' ,' '-').log" &
      sleep 2  # Stagger submissions
    fi

    # Track C
    if [ $i -lt ${#PENDING_TRACK_C[@]} ]; then
      metro=${PENDING_TRACK_C[$i]}
      run_pipeline "prompt1" "grooming" "$metro" "$LOG_DIR/track-c-$(echo "$metro" | tr ' ,' '-').log" &
      sleep 2  # Stagger submissions
    fi

    # Track D
    if [ $i -lt ${#PENDING_TRACK_D[@]} ]; then
      metro=${PENDING_TRACK_D[$i]}
      run_pipeline "prompt1" "dog_walking_petsitting" "$metro" "$LOG_DIR/track-d-$(echo "$metro" | tr ' ,' '-').log" &
      sleep 2  # Stagger submissions
    fi
  done

  log_msg "All batches queued; waiting for completion..."
  wait

fi

log_msg ""
log_msg "=========================================="
log_msg "Phase 2 Pipeline Complete"
log_msg "=========================================="
log_msg "Logs: $LOG_DIR/"
log_msg ""
log_msg "=== Final Summary ==="
/usr/bin/psql -X "$LEADS_DB_URL" -c "
SELECT
  service_category,
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE icp_tier = 'Tier 1') as tier1,
  ROUND(100.0 * COUNT(*) FILTER (WHERE icp_tier = 'Tier 1') / COUNT(*), 1) as pct_tier1
FROM leads.businesses
GROUP BY service_category
ORDER BY service_category;
" | tee -a "$MASTER_LOG"

log_msg ""
log_msg "Master log: $MASTER_LOG"

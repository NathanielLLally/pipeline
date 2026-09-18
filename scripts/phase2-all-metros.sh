#!/usr/bin/env bash
# Phase 2 Pipeline: Dog Training Phase 2 + Daycare/Boarding Phase 1 across all metros
#
# Executes both tracks in parallel across all 16 US metros.
# Track A: Dog Training Phase 2 (refined specialist searches)
# Track B: Daycare + Boarding Phase 1 (new service category)
#
# Usage:
#   ./scripts/phase2-all-metros.sh [--sequential|--parallel] [--skip-completed]
#
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

# Load env
set -a
source .env
set +a

# Config
SEQUENTIAL=0
SKIP_COMPLETED=0
WORKERS=3

while (($# > 0)); do
  case "$1" in
    --sequential)     SEQUENTIAL=1; shift ;;
    --parallel)       SEQUENTIAL=0; shift ;;
    --skip-completed) SKIP_COMPLETED=1; shift ;;
    --workers)        WORKERS=$2; shift 2 ;;
    *)                echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

# All 16 metros (priority order for each track)
METROS_TRACK_A=(
  "New York, NY"
  "Los Angeles, CA"
  "Chicago, IL"
  "Houston, TX"
  "Phoenix, AZ"
  "Philadelphia, PA"
  "San Antonio, TX"
  "San Diego, CA"
  "Dallas, TX"
  "San Francisco Bay Area, CA"
  "Boston, MA"
  "Washington, DC"
  "Seattle, WA"
  "Denver, CO"
  "Atlanta, GA"
  "Miami, FL"
)

METROS_TRACK_B=(
  "San Francisco Bay Area, CA"
  "Los Angeles, CA"
  "New York, NY"
  "Boston, MA"
  "Washington, DC"
  "Seattle, WA"
  "Denver, CO"
  "Chicago, IL"
  "San Diego, CA"
  "Philadelphia, PA"
  "Phoenix, AZ"
  "Dallas, TX"
  "Houston, TX"
  "Atlanta, GA"
  "Miami, FL"
  "San Antonio, TX"
)

# Logging
LOG_DIR="phase2-logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/phase2-all.log"

log_msg() {
  local msg="$(date '+%Y-%m-%d %H:%M:%S') — $*"
  echo "$msg" | tee -a "$MASTER_LOG"
}

# Check if batch already completed
is_completed() {
  local phase=$1 service=$2 metro=$3
  local batch_dir="map-outputs/${phase}-${service}-"*

  if [ $SKIP_COMPLETED -eq 0 ]; then
    return 1  # Not skipping; always return false
  fi

  # Check if any batch exists for this combo
  for d in $batch_dir; do
    if [[ "$d" == *"$metro"* ]]; then
      return 0  # Completed
    fi
  done
  return 1  # Not completed
}

# Run a single pipeline
run_pipeline() {
  local phase=$1 service=$2 metro=$3 log_file=$4

  if is_completed "$phase" "$service" "$metro"; then
    log_msg "[SKIP] $phase/$service/$metro (already completed)"
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
log_msg "Phase 2 Pipeline: All Metros"
log_msg "=========================================="
log_msg "Mode: $([ $SEQUENTIAL -eq 1 ] && echo "SEQUENTIAL" || echo "PARALLEL")"
log_msg "Workers per batch: $WORKERS"
log_msg "Skip completed: $SKIP_COMPLETED"
log_msg ""
log_msg "Track A (dog_training/prompt2): ${#METROS_TRACK_A[@]} metros"
log_msg "Track B (daycare_boarding/prompt1): ${#METROS_TRACK_B[@]} metros"
log_msg ""

if [ $SEQUENTIAL -eq 1 ]; then
  log_msg "Sequential execution: all Track A, then all Track B"
  log_msg ""

  # Track A sequential
  log_msg "=== TRACK A: Dog Training Phase 2 ==="
  for metro in "${METROS_TRACK_A[@]}"; do
    run_pipeline "prompt2" "dog_training" "$metro" "$LOG_DIR/track-a-$(echo "$metro" | tr ' ,' '-').log"
  done

  log_msg ""
  log_msg "=== TRACK B: Daycare + Boarding Phase 1 ==="
  for metro in "${METROS_TRACK_B[@]}"; do
    run_pipeline "prompt1" "daycare_boarding" "$metro" "$LOG_DIR/track-b-$(echo "$metro" | tr ' ,' '-').log"
  done

else
  # Parallel execution: interleave both tracks
  log_msg "Parallel execution: alternating Track A & B"
  log_msg ""

  local max_metros=${#METROS_TRACK_A[@]}
  [ ${#METROS_TRACK_B[@]} -gt $max_metros ] && max_metros=${#METROS_TRACK_B[@]}

  for ((i = 0; i < max_metros; i++)); do
    # Track A
    if [ $i -lt ${#METROS_TRACK_A[@]} ]; then
      metro=${METROS_TRACK_A[$i]}
      run_pipeline "prompt2" "dog_training" "$metro" "$LOG_DIR/track-a-$(echo "$metro" | tr ' ,' '-').log" &
      sleep 2  # Stagger submissions
    fi

    # Track B
    if [ $i -lt ${#METROS_TRACK_B[@]} ]; then
      metro=${METROS_TRACK_B[$i]}
      run_pipeline "prompt1" "daycare_boarding" "$metro" "$LOG_DIR/track-b-$(echo "$metro" | tr ' ,' '-').log" &
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
log_msg "=== Summary ==="
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

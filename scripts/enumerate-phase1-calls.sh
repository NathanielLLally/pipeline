#!/usr/bin/env bash
# Enumerate all planned Phase 1 pipeline.sh calls for dog_training across all metros.
# Marks covered metros (via search_log) and pending ones.
#
# Usage:
#   ./scripts/enumerate-phase1-calls.sh [--covered|--pending|--all|--execute]
#   --covered   show only completed metros
#   --pending   show only uncovered metros (default)
#   --all       show all metros with coverage status
#   --execute   run all pending metros sequentially

set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

# Load metros from queries/metros.json
metros=$(jq -r '.metros[].metro' queries/metros.json)

# Query database for already-covered metros (service_category = dog_training)
covered_metros=$(
  set -a && source .env && set +a
  psql -X "$LEADS_DB_URL" -t -A -c \
    "SELECT DISTINCT geo_target FROM leads.search_log WHERE service_category = 'dog_training' AND geo_target IN ($(echo "$metros" | jq -R -s -c 'split("\n") | map(select(length > 0)) | map(@json) | join(",")'))" 2>/dev/null || echo ""
)

filter="${1:-pending}"

echo "=== PHASE 1 — dog_training PIPELINE SCHEDULE ==="
echo ""
echo "Total metros: $(echo "$metros" | wc -l)"
echo "Covered: $(echo "$covered_metros" | grep -c . || echo 0)"
echo ""

case "$filter" in
  covered)
    echo "COMPLETED METROS:"
    for m in $metros; do
      if echo "$covered_metros" | grep -q "^$m$"; then
        echo "  ✓ $m"
      fi
    done
    ;;
  pending)
    echo "PENDING METROS (in order):"
    n=0
    for m in $metros; do
      if ! echo "$covered_metros" | grep -q "^$m$"; then
        n=$((n + 1))
        echo "  $n. $m"
      fi
    done
    ;;
  all)
    echo "ALL METROS:"
    n=0
    for m in $metros; do
      n=$((n + 1))
      if echo "$covered_metros" | grep -q "^$m$"; then
        echo "  $n. ✓ $m (covered)"
      else
        echo "  $n. ◌ $m (pending)"
      fi
    done
    ;;
  execute)
    echo "EXECUTING PENDING METROS..."
    echo ""
    for m in $metros; do
      if ! echo "$covered_metros" | grep -q "^$m$"; then
        echo "→ Starting: $m"
        ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "$m" --workers 3
        echo "✓ Completed: $m"
        echo ""
      fi
    done
    echo "=== PHASE 1 COMPLETE ==="
    ;;
  *)
    echo "Usage: enumerate-phase1-calls.sh [--covered|--pending|--all|--execute]" >&2
    exit 2
    ;;
esac

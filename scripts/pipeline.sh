#!/usr/bin/env bash
# Orchestrates one full batch: gen-queries -> create_search_job.py -> transform-and-score -> upsert.
#
# Usage:
#   ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Los Angeles, CA" [--max-geo N] [--workers N]
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"
set -a; source .env; set +a

phase=""
service_category=""
metro=""
max_geo=""
workers=10

while (($# > 0)); do
  case "$1" in
    --phase) phase=$2; shift 2 ;;
    --service-category) service_category=$2; shift 2 ;;
    --metro) metro=$2; shift 2 ;;
    --max-geo) max_geo=$2; shift 2 ;;
    --workers) workers=$2; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$service_category" ]]; then
  echo "Usage: pipeline.sh --phase <name> --service-category <dog_training|daycare_boarding|grooming|dog_walking_petsitting> [--metro \"City, ST\"] [--max-geo N] [--workers N]" >&2
  exit 2
fi

batch_label="${phase:-adhoc}-${service_category}-$(date +%Y%m%d%H%M%S)"
batch_dir="map-outputs/$batch_label"
mkdir -p "$batch_dir/results" "$batch_dir/jobids"

# Generate queries with a hash/counter for deduplication and retry tracking
queries_hash=$(date +%s%N | md5sum | cut -c1-8)
queries_file="$batch_dir/queries-${queries_hash}.txt"

echo "== [1/4] gen-queries -> $queries_file =="
gen_args=(--service-category "$service_category" --out "$queries_file" --meta "$batch_dir/meta.json")
[[ -n "$phase" ]] && gen_args+=(--phase "$phase")
[[ -n "$metro" ]] && gen_args+=(--metro "$metro")
[[ -n "$max_geo" ]] && gen_args+=(--max-geo "$max_geo")
node scripts/gen-queries.mjs "${gen_args[@]}"

if [[ ! -s "$queries_file" ]]; then
  echo "No uncovered queries to run for this phase/service/metro (already covered). Nothing to do." >&2
  exit 0
fi

echo "== [2/4] create_search_job.py -> $batch_dir/results/ =="
python3 scripts/create_search_job.py --base-url "$BASE_URL" --api-key "$API_KEY" \
  -o "$batch_dir/results" --jobids-dir "$batch_dir/jobids" -w "$workers" < "$queries_file"

echo "== [3/4] transform-and-score -> $batch_dir/staging.csv =="
node scripts/transform-and-score.mjs --batch-dir "$batch_dir/results" --meta "$batch_dir/meta.json" --out "$batch_dir/staging.csv"

echo "== [4/4] load + upsert into leads.businesses =="
staging_columns="place_id, domain, phone_normalized, name_city_state_key, name, maps_url, website, phone, address, city, state, zip, latitude, longitude, rating, review_count, primary_category, additional_categories, description, hours, price_range, status, service_category, icp_score, icp_tier, qc_status, query, geo_target, phase, raw_scrape"
/usr/bin/psql -X "$LEADS_DB_URL" -v ON_ERROR_STOP=1 -c "TRUNCATE leads.staging_businesses;"
/usr/bin/psql -X "$LEADS_DB_URL" -v ON_ERROR_STOP=1 -c "\copy leads.staging_businesses ($staging_columns) FROM '$batch_dir/staging.csv' WITH (FORMAT csv, HEADER true)"
/usr/bin/psql -X "$LEADS_DB_URL" -v ON_ERROR_STOP=1 -v batch_label="$batch_label" -v phase="${phase:-adhoc}" -f db/upsert.sql

# Cleanup: remove queries file and jobids symlinks on success
rm -f "$queries_file"
rm -rf "$batch_dir/jobids"

echo "Batch $batch_label complete."

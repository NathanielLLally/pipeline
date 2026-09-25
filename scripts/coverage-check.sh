#!/usr/bin/env bash
# coverage-check.sh: Analyzes Google Maps search coverage across metros and services.

set -euo pipefail

# Load env
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "${LEADS_DB_URL:-}" ]; then
  echo "Error: LEADS_DB_URL is not set." >&2
  exit 1
fi

# Get dynamic list of services from service_terms.json
SERVICES=$(jq -r 'keys[]' queries/service_terms.json)

# Handle arguments
SHOW_STATS=0
SHOW_TSV=0
for arg in "$@"; do
  if [ "$arg" == "--stats" ]; then
    SHOW_STATS=1
  fi
  if [ "$arg" == "--tsv" ]; then
    SHOW_TSV=1
  fi
done

# The core coverage query
# Returns: geo_target, service_category, record_count
QUERY="WITH all_targets AS (
    SELECT DISTINCT geo_target FROM leads.search_log
),
all_categories AS (
    SELECT DISTINCT service_category FROM leads.search_log
),
grid AS (
    SELECT t.geo_target, c.service_category
    FROM all_targets t
    CROSS JOIN all_categories c
)
SELECT
    g.geo_target,
    g.service_category,
    COUNT(l.geo_target) as record_count
FROM grid g
LEFT JOIN leads.search_log l
    ON g.geo_target = l.geo_target
    AND g.service_category = l.service_category
GROUP BY g.geo_target, g.service_category
ORDER BY g.geo_target, record_count DESC;"

if [ $SHOW_TSV -eq 1 ]; then
  # Machine-readable long format for scripts: metro<TAB>service_category<TAB>record_count,
  # one row per (metro, service) pair from queries/metros.json x service_terms.json. Same
  # metro-level LIKE join as --stats (a metro's count includes all its suburb geo_targets),
  # no headers/borders, so callers can consume it directly without parsing psql's table output.
  METROS_LIST=$(jq -r '.metros[].metro' queries/metros.json | sort)

  VALUES_SQL=""
  while IFS= read -r m; do
    ESC_M=$(echo "$m" | sed "s/'/''/g")
    VALUES_SQL+="('$ESC_M'),"
  done <<< "$METROS_LIST"
  VALUES_SQL=${VALUES_SQL%,}

  SERVICES_LIST=""
  for s in $SERVICES; do
    ESC_S=$(echo "$s" | sed "s/'/''/g")
    SERVICES_LIST+="('$ESC_S'),"
  done
  SERVICES_LIST=${SERVICES_LIST%,}

  TSV_QUERY="SELECT m.metro || E'\t' || s.service || E'\t' || COUNT(l.geo_target)
    FROM (VALUES $VALUES_SQL) AS m(metro)
    CROSS JOIN (VALUES $SERVICES_LIST) AS s(service)
    LEFT JOIN leads.search_log l
      ON l.geo_target LIKE '%' || m.metro || '%'
      AND l.service_category = s.service
    GROUP BY m.metro, s.service
    ORDER BY m.metro, s.service;"

  /usr/bin/psql -X -t -A "$LEADS_DB_URL" -c "$TSV_QUERY"
  exit 0
fi

if [ $SHOW_STATS -eq 1 ]; then
  echo "--- Search Coverage Cross-Tab ---"
  # Use the metros from queries/metros.json as the primary axis
  METROS_LIST=$(jq -r '.metros[].metro' queries/metros.json | sort)

  # Build a query that joins the metros list against the search_log
  # We'll use a VALUES list for the metros to ensure all are present even if not in search_log
  VALUES_SQL=""
  # Use a while loop to read METROS_LIST to handle spaces correctly
  while IFS= read -r m; do
    ESC_M=$(echo "$m" | sed "s/'/''/g")
    VALUES_SQL+="('$ESC_M'),"
  done <<< "$METROS_LIST"
  # Remove trailing comma
  VALUES_SQL=${VALUES_SQL%,}


  COLUMNS_SQL=""
  for s in $SERVICES; do
    ESC_S=$(echo "$s" | sed "s/'/''/g")
    COLUMNS_SQL+=", COUNT(*) FILTER (WHERE l.service_category = '$ESC_S') as \"$s\""
  done

  STATS_QUERY="SELECT
    m.metro as \"Metro\"
    $COLUMNS_SQL
    FROM (VALUES $VALUES_SQL) AS m(metro)
    LEFT JOIN leads.search_log l ON l.geo_target LIKE '%' || m.metro || '%'
    GROUP BY m.metro
    ORDER BY m.metro;"

  /usr/bin/psql -X "$LEADS_DB_URL" -c "$STATS_QUERY"
else

  echo "--- Search Coverage Report ---"
  /usr/bin/psql -X "$LEADS_DB_URL" -c "$QUERY"
fi

echo ""
echo "--- Pipeline Completion Stats ---"

echo ""
echo "--- Pipeline Completion Stats ---"

# TODO stubs for other pipeline stages
echo "Website crawl completion: [implement me]"
echo "Email extraction completion: [implement me]"
echo "Extracted email validation: [implement me]"
echo "Advertising outlet searches: [implement me]"
echo "Contact form submissions: [implement me]"

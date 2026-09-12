#!/bin/sh
TOP=$PWD
if [ -z "$API_KEY" ]; then
  . $TOP/.env
fi
curl -sX 'GET'   "$BASE_URL/api/v1/health"   -H 'accept: application/json' -H "Authorization: Bearer $API_KEY"

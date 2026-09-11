#!/bin/sh
TOP=$PWD
if [ -z "$LEADS_DB_URL" ]; then
  . $TOP/.env
fi

INPUT=/tmp/queries.$$.txt
cp queries.txt $INPUT

#  just run dB seed query command locally to minimize number of commands and network chatter needed for the process
#
echo "seeding worker vps jobs dB with initial query"
$TOP/bin/google_maps_scraper -dsn $LEADS_DB_URL -lang en -c 8 -input $INPUT -produce


#ssh to vps host, run scraping worker process from docker container
ssh -p $SCRAPER_SSH_PORT "$SCRAPER_SSH_USER@$SCRAPER_SSH_HOST" docker run --network n8n_default -v gmaps-playwright-cache:/opt:z gosom/google-maps-scraper -dsn \'$LEADS_DB_URL\' -depth 10 -c 8 -browser-pool-size 2 -pages-per-browser 4 -exit-on-inactivity 3m


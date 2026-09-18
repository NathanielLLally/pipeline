#!/bin/sh
TOP=$PWD
. $TOP/.env


#  when the job is done, the worker vps should be destroyed
#  the database server should also be destroyed, and the pgsql/data should be on a persistent volume
#
#  this means that provision is responsible for spinning up a container based on an image or snapshot or whatever
#  then starting the docker serve from compose as root like gmapsaas provision does
#

#ssh to vps host, run scraping worker process from docker container

ssh -p $SCRAPER_SSH_PORT $SCRAPER_SSH_USER@$SCRAPER_SSH_HOST "sudo sh -c 'if [ ! -d /opt/gms-worker ]; then mkdir /opt/gms-worker; cp /opt/gms-server/.env /opt/gms-worker; fi;'"

# Push the refresh script up (avoids nested heredoc/escaping issues of embedding it inline)
scp -P $SCRAPER_SSH_PORT refresh-proxies.sh $SCRAPER_SSH_USER@$SCRAPER_SSH_HOST:/tmp/refresh-proxies.sh
ssh -p $SCRAPER_SSH_PORT $SCRAPER_SSH_USER@$SCRAPER_SSH_HOST "sudo mv /tmp/refresh-proxies.sh /opt/gms-worker/refresh-proxies.sh && sudo chmod +x /opt/gms-worker/refresh-proxies.sh"

# Fetches the current proxy list, writes docker-compose.yml with -proxies baked in, and runs docker compose up -d
ssh -p $SCRAPER_SSH_PORT $SCRAPER_SSH_USER@$SCRAPER_SSH_HOST "sudo /opt/gms-worker/refresh-proxies.sh"

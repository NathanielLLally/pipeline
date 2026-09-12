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
ssh -p $REST_SSH_PORT $REST_SSH_USER@$REST_SSH_HOST "sudo sh -c 'if [ ! -d /opt/gms-worker ]; then mkdir /opt/gms-worker; cp /opt/gms-server/.env /opt/gms-worker; fi;'"
ssh -p $REST_SSH_PORT $REST_SSH_USER@$REST_SSH_HOST "sudo tee /opt/gms-worker/docker-compose.yml << 'EOF' > /dev/null
services:
  worker:
    image: ghcr.io/ghcr.io/gosom/google-maps-scraper-saas:latest
    restart: unless-stopped
    env_file:
      - .env
    command: [\"worker\"]
EOF"

ssh -p $REST_SSH_PORT $REST_SSH_USER@$REST_SSH_HOST "sudo sh -c 'cd /opt/gms-worker; docker compose up -d;'"

#sudo mkdir /opt/gms-worker; cp -f /opt/gms-server/docker-compose.yml 
#docker compose -f /opt/gms-server/docker-compose.yml up -d

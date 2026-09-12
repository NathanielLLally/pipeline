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
ssh -p $REST_SSH_PORT "$REST_SSH_USER@$REST_SSH_HOST" sudo docker compose -f /opt/gms-server/docker-compose.yml up -d

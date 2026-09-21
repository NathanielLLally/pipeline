#!/bin/sh
TOP=$PWD
. $TOP/.env


#  when the job is done, the worker vps should be destroyed
#  the database server should also be destroyed, and the pgsql/data should be on a persistent volume
#
#  this means that provision is responsible for spinning up a container based on an image or snapshot or whatever
#  then starting the docker serve from compose as root like gmapsaas provision does
#

# --skip-restart: don't touch the container stack, just run admin create-user
skip_restart=0
for arg in "$@"; do
  case "$arg" in
    --skip-restart) skip_restart=1 ;;
  esac
done

if [ "$skip_restart" -eq 0 ]; then
  # If the server stack is already up, bring it down first for a clean restart
  ssh -p $REST_SSH_PORT "$REST_SSH_USER@$REST_SSH_HOST" "sudo sh -c 'if [ -n \"\$(docker compose -f /opt/gms-server/docker-compose.yml ps -q)\" ]; then docker compose -f /opt/gms-server/docker-compose.yml down; fi'"

  #ssh to vps host, run scraping worker process from docker container
  ssh -p $REST_SSH_PORT "$REST_SSH_USER@$REST_SSH_HOST" sudo docker compose -f /opt/gms-server/docker-compose.yml up -d
fi

# Create the admin user -- interactive, so allocate a TTY over ssh
ssh -t -p $REST_SSH_PORT "$REST_SSH_USER@$REST_SSH_HOST" sudo docker exec -it gms-server-server-1 /app/gmapssaas admin create-user

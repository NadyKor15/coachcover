#!/usr/bin/env bash
# Stop and remove the local test Postgres container (only this one).
set -euo pipefail

NAME=coachcover-pg-test

if [ "$(docker ps -a --filter "name=^${NAME}$" --format '{{.Names}}')" = "$NAME" ]; then
    docker stop "$NAME" >/dev/null
    docker rm "$NAME" >/dev/null
    echo "$NAME removed"
else
    echo "$NAME not present"
fi

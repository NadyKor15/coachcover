#!/usr/bin/env bash
# Start the local test Postgres (only this container): postgres:16-alpine on 127.0.0.1:55432.
# Throwaway data, no password (trust), reachable from this machine only.
set -euo pipefail

NAME=coachcover-pg-test
IMAGE=postgres:16-alpine
PORT=55432

if [ "$(docker ps --filter "name=^${NAME}$" --format '{{.Names}}')" = "$NAME" ]; then
    echo "$NAME already running"
elif [ "$(docker ps -a --filter "name=^${NAME}$" --format '{{.Names}}')" = "$NAME" ]; then
    docker start "$NAME" >/dev/null
    echo "$NAME started"
else
    docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:5432" \
        -e POSTGRES_HOST_AUTH_METHOD=trust "$IMAGE" >/dev/null
    echo "$NAME created"
fi

# TCP check: the first-boot init server listens on a socket only, so this waits for the real one.
for _ in $(seq 1 60); do
    if docker exec "$NAME" pg_isready -q -h 127.0.0.1 -U postgres; then
        echo "ready: postgresql://postgres@127.0.0.1:${PORT}/postgres"
        exit 0
    fi
    sleep 1
done
echo "$NAME did not become ready in 60 s" >&2
exit 1

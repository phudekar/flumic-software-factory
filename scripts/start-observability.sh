#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.observability ]; then
    (umask 077; cp observability/bootstrap.env.example .env.observability)
fi
mkdir -p data/observability-bootstrap
chmod 700 data/observability-bootstrap
compose() {
    if [ -f .env ]; then
        docker-compose --env-file .env --env-file .env.observability -f observability/pours/deployment/compose.yaml "$@"
    else
        docker-compose --env-file .env.observability -f observability/pours/deployment/compose.yaml "$@"
    fi
}
compose up -d "$@"
bootstrap_id=$(compose ps -a -q dashboard-bootstrap)
docker wait "$bootstrap_id" >/dev/null
bootstrap_exit=$(docker inspect --format '{{.State.ExitCode}}' "$bootstrap_id")
if [ "$bootstrap_exit" != 0 ]; then
    compose logs --tail=20 dashboard-bootstrap >&2
    exit 1
fi
scripts/start-resource-metrics.sh
printf '%s\n' 'SigNoz UI: http://localhost:3301' 'Factory dashboard provisioned automatically.' 'OTLP/HTTP: http://localhost:4318'

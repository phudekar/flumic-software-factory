#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${FLUMIC_RESOURCE_METRICS:-1}" in
    0|1) ;;
    *) echo 'FLUMIC_RESOURCE_METRICS must be 0 or 1.' >&2; exit 1 ;;
esac
if [ ! -f .env.observability ]; then
    (umask 077; cp observability/bootstrap.env.example .env.observability)
fi
mkdir -p data/observability-bootstrap
chmod 700 data/observability-bootstrap
./scripts/observability-compose.sh up -d "$@"
if [ ! -f data/observability-bootstrap/.provisioned ]; then
    if ! ./scripts/provision-dashboards.sh; then
        echo 'Monitoring started, but dashboard provisioning failed. Configure .env.observability, then run just dashboards-provision.' >&2
    fi
fi
if [ "${FLUMIC_RESOURCE_METRICS:-1}" = 1 ]; then ./scripts/start-resource-metrics.sh; fi
printf '%s\n' 'SigNoz UI: http://localhost:3301' 'OTLP/HTTP: http://localhost:4318'

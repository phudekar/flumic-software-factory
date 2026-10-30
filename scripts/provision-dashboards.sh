#!/bin/sh
# Explicit and repeatable; bootstrap itself reconciles the dashboard by identity.
set -eu
cd "$(dirname "$0")/.."
mkdir -p data/observability-bootstrap
chmod 700 data/observability-bootstrap
./scripts/observability-compose.sh --profile provisioning run --rm "$@" dashboard-bootstrap
touch data/observability-bootstrap/.provisioned

#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

exec docker-compose exec -T worker python /worker/push_sample_event.py "$@"

#!/bin/sh
# Build the image names expected by managed pools, independent of checkout name.
# This operation does not start workers or require provider/engine credentials.
set -eu
cd "$(dirname "$0")/.."
docker build -f worker/Dockerfile -t hatchet-test-worker:latest "$@" .
docker build -f worker/Dockerfile.developer -t hatchet-test-developer-worker:latest "$@" .

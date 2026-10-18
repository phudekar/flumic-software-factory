#!/bin/sh
set -eu

: "${HATCHET_CLIENT_TOKEN:?Load the development Hatchet profile before starting workers}"
: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in the root .env file}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN in the root .env file}"
: "${GITHUB_REPOSITORY:?Set GITHUB_REPOSITORY in the root .env file}"

exec python /worker/worker.py "$@"

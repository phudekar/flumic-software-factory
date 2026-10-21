#!/bin/sh
set -eu

# Runtime adapters validate their own credentials. A custom planner/coder must
# not inherit mandatory OpenAI/Anthropic credentials from another implementation.
exec python /worker/worker.py "$@"

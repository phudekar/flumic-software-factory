#!/bin/sh
set -eu

: "${HATCHET_CLIENT_TOKEN:?Load the development Hatchet profile before starting workers}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN in the root .env file}"
: "${GITHUB_REPOSITORY:?Set GITHUB_REPOSITORY in the root .env file}"

case "${WORKER_TYPE:-simple}" in
    simple)
        : "${OPENAI_API_KEY:?Set OPENAI_API_KEY for this worker}"
        ;;
    developer)
        case "${DEVELOPER_AGENT:-codex}" in
            codex|api)
                : "${OPENAI_API_KEY:?Set OPENAI_API_KEY for this worker}"
                ;;
            claude)
                : "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY for Claude Code}"
                ;;
        esac
        ;;
esac

exec python /worker/worker.py "$@"

#!/bin/sh
set -eu
tool=$(cat /tmp/factory/coding-tool.txt)
[ "$tool" != api ] || exit 0
. /etc/os-release
if ! command -v npm >/dev/null 2>&1; then
  case "$ID" in
    debian|ubuntu) apt-get update; apt-get install -y --no-install-recommends nodejs npm; rm -rf /var/lib/apt/lists/* ;;
    alpine) apk add --no-cache nodejs npm ;;
    *) exit 1 ;;
  esac
fi
case "$tool" in
  codex) npm install --global "@openai/codex@$(cat /tmp/factory/codex-version.txt)"; codex --version ;;
  claude) npm install --global "@anthropic-ai/claude-code@$(cat /tmp/factory/claude-version.txt)"; claude --version ;;
  *) echo "Unsupported coding tool: $tool" >&2; exit 1 ;;
esac

#!/bin/sh
set -eu
. /etc/os-release
case "$ID" in
  debian|ubuntu)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # Package names are validated before this file enters the build context.
    xargs apt-get install -y --no-install-recommends python3 python3-venv ca-certificates git curl util-linux < /tmp/factory/system-packages.txt
    rm -rf /var/lib/apt/lists/*
    if ! id agent >/dev/null 2>&1; then
      groupadd --gid 10001 agent
      useradd --uid 10001 --gid agent --create-home --shell /usr/sbin/nologin agent
    fi
    ;;
  alpine)
    xargs apk add --no-cache python3 py3-pip ca-certificates git curl util-linux build-base python3-dev libffi-dev openssl-dev cargo < /tmp/factory/system-packages.txt
    if ! id agent >/dev/null 2>&1; then
      addgroup -g 10001 agent
      adduser -D -u 10001 -G agent -h /home/agent agent
    fi
    ;;
  *) echo "Unsupported base distribution: $ID (use Debian, Ubuntu or Alpine)" >&2; exit 1 ;;
esac
[ "$(id -u agent)" = 10001 ] && [ "$(id -g agent)" = 10001 ] || { echo 'agent must have UID/GID 10001' >&2; exit 1; }
mkdir -p /home/agent/workspaces /home/agent/.codex
chown -R 10001:10001 /home/agent

#!/bin/sh
set -e

# Ensure /data exists with correct ownership
# (handles host-mounted volumes that may be owned by root)
mkdir -p /data
chown -R memory:memory /data 2>/dev/null || true

exec python3 server.py "$@"

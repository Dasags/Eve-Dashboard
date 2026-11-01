#!/bin/sh
set -e
mkdir -p "${DATA_DIR:-/data}"
chown eve "${DATA_DIR:-/data}" 2>/dev/null || true
exec setpriv --reuid=eve --regid=eve --clear-groups "$@"

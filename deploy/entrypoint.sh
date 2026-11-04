#!/bin/sh
# Run TradeZone as the unprivileged "tradezone" user.
# Hosting platforms (Render) mount the /data disk at runtime, often owned by root, so hand it to
# the app user first. If /data still isn't writable for that user, keep running as root rather
# than lose the login, trades and what the bots learned.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  chown -R tradezone:tradezone /data 2>/dev/null || true
  if setpriv --reuid=tradezone --regid=tradezone --init-groups test -w /data; then
    exec setpriv --reuid=tradezone --regid=tradezone --init-groups "$@"
  fi
  echo "WARNING: /data is not writable for the tradezone user; running as root" >&2
fi
exec "$@"

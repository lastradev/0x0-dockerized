#!/bin/sh
set -e

mkdir -p /app/up
chown -R app:app /app/up

exec "$@"

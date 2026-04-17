#!/usr/bin/with-contenv bashio
set -euo pipefail

export TZ="$(bashio::config 'TZ' || echo 'Australia/Sydney')"

exec python3 /app/main.py

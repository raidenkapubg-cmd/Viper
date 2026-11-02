#!/usr/bin/env bash
# Start Viper from any checkout location. Creates .venv on first run.
#   scripts/start.sh            -> run the server
#   scripts/start.sh --check    -> validate config + IP binding, then exit
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
fi
if [ requirements.txt -nt .venv/.installed ] || [ ! -f .venv/.installed ]; then
  .venv/bin/pip install -r requirements.txt
  touch .venv/.installed
fi
[ -f .env ] || { echo "No .env found. cp .env.example .env and fill it in." >&2; exit 1; }

if [ "${1:-}" = "--check" ]; then
  exec .venv/bin/python - <<'PY'
from viper.config import settings
from viper.net import binding
print(f"env={settings.env} host={settings.host}:{settings.port} feed={settings.feed_mode} live_orders={settings.live_trading_enabled}")
print(f"trading app configured={settings.trading.configured}  data app configured={settings.data.configured}")
routes = binding.configure(settings.enable_ip_binding, settings.trading.source_ip, settings.data.source_ip)
print("binding:", routes if settings.enable_ip_binding else "disabled (OS route)")
if settings.ui_password == "" and settings.host not in ("127.0.0.1", "localhost"):
    print("WARNING: VIPER_UI_PASSWORD is empty and the server listens on a non-loopback host")
print("OK")
PY
fi

exec .venv/bin/python -m viper

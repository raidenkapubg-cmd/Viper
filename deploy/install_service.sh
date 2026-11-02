#!/usr/bin/env bash
# Install Viper as a systemd service for the current checkout and user.
#   sudo deploy/install_service.sh [service-user]
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
USER_NAME="${1:-${SUDO_USER:-$(id -un)}}"
UNIT=/etc/systemd/system/viper.service
sed -e "s#__ROOT__#${ROOT}#g" -e "s#__USER__#${USER_NAME}#g" deploy/viper.service.template > "$UNIT"
chmod +x scripts/start.sh deploy/check_ips.sh
systemctl daemon-reload
systemctl enable viper
echo "Installed $UNIT for $ROOT (user $USER_NAME). Start with: sudo systemctl start viper ; logs: journalctl -u viper -f"

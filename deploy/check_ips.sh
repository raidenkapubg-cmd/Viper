#!/usr/bin/env bash
# Verify each source IP really egresses as itself: what FYERS's whitelist will see.
# Usage: deploy/check_ips.sh            (reads VPS_*_SOURCE_IP from .env)
#        deploy/check_ips.sh IP_A IP_B
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -ge 2 ]; then A="$1"; B="$2"; else
  A="$(grep -E '^VPS_TRADING_SOURCE_IP=' .env | cut -d= -f2- | tr -d '"'"'"' ')"
  B="$(grep -E '^VPS_DATA_SOURCE_IP=' .env | cut -d= -f2- | tr -d '"'"'"' ')"
fi
fail=0
for pair in "IP_A(trading):$A" "IP_B(data):$B"; do
  name="${pair%%:*}"; ip="${pair#*:}"
  if [ -z "$ip" ]; then echo "$name: not set"; fail=1; continue; fi
  if ! ip -4 addr | grep -qw "$ip"; then echo "$name $ip: NOT assigned to any interface here (ip -4 addr)"; fail=1; continue; fi
  seen="$(curl -4 -s --max-time 8 --interface "$ip" https://api.ipify.org || true)"
  if [ "$seen" = "$ip" ]; then echo "$name $ip: OK (egress seen as $seen)"
  else echo "$name $ip: MISMATCH, the internet sees '${seen:-no response}'. Add policy routing (see README)."; fail=1; fi
done
exit $fail

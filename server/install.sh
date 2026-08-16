#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/network-quality-tester"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

if ! id nqtester >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin nqtester
fi

install -d -o nqtester -g nqtester -m 0755 "$INSTALL_DIR"
install -o nqtester -g nqtester -m 0755 "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"
install -o nqtester -g nqtester -m 0644 "$SCRIPT_DIR/config.env.example" /etc/network-quality-tester.conf.example
if [[ ! -f /etc/network-quality-tester.conf ]]; then
  install -o root -g root -m 0644 "$SCRIPT_DIR/config.env.example" /etc/network-quality-tester.conf
fi
install -o root -g root -m 0644 "$SCRIPT_DIR/network-quality.service" /etc/systemd/system/network-quality.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 37820/tcp comment 'network quality control' || true
  ufw allow 37821/udp comment 'network quality UDP probes' || true
  ufw allow 37822/tcp comment 'network quality TCP probes' || true
fi

systemctl daemon-reload
systemctl enable --now network-quality.service
systemctl --no-pager --full status network-quality.service

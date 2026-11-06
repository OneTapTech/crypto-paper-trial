#!/usr/bin/env bash
set -euo pipefail
# Run as root on a dedicated Ubuntu 24.04 VM after copying files to /opt/crypto-paper.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 sqlite3
id -u paperbot >/dev/null 2>&1 || useradd --system --home /opt/crypto-paper --shell /usr/sbin/nologin paperbot
chown -R paperbot:paperbot /opt/crypto-paper
cat >/etc/systemd/system/crypto-paper-trader.service <<'EOF'
[Unit]
Description=Crypto paper-only trading simulation
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
User=paperbot
WorkingDirectory=/opt/crypto-paper
ExecStart=/usr/bin/python3 -u /opt/crypto-paper/trader.py --interval 300
Restart=always
RestartSec=15
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/crypto-paper
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/crypto-paper-dashboard.service <<'EOF'
[Unit]
Description=Localhost-only crypto paper dashboard
After=network.target
[Service]
Type=simple
User=paperbot
WorkingDirectory=/opt/crypto-paper
Environment=DASH_HOST=127.0.0.1
Environment=DASH_PORT=8080
ExecStart=/usr/bin/python3 -u /opt/crypto-paper/dashboard.py
Restart=always
RestartSec=15
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/crypto-paper
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now crypto-paper-trader crypto-paper-dashboard
systemctl --no-pager status crypto-paper-trader crypto-paper-dashboard || true

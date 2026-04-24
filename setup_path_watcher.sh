#!/bin/bash
# Install / refresh the pegaprox-patch.path systemd watcher so that ANY of
# the three PegaProx files we patch retrigger the orchestrator:
#   - web/src/dashboard.js  (sidebar DOCKER SWARM + 9 JSX patches)
#   - pegaprox/app.py       (CSP frame-ancestors + X-Frame-Options)
#   - pegaprox/api/vms.py   (VNC WebSocket subprotocol=['binary'])
#
# The original install.sh shipped a watcher that only monitored the first two.
# After PegaProx 0.9.7's VNC subprotocol regression we need vms.py coverage too,
# otherwise the next auto-update will silently break VM consoles again.
#
# Idempotent: safe to run any number of times; it rewrites the unit and reloads.

set -e

PATH_UNIT=/etc/systemd/system/pegaprox-patch.path
SERVICE_UNIT=/etc/systemd/system/pegaprox-patch.service

cat > "$PATH_UNIT" << 'EOF'
[Unit]
Description=Watch PegaProx files for changes and re-apply docker_swarm patches

[Path]
PathChanged=/opt/PegaProx/web/src/dashboard.js
PathChanged=/opt/PegaProx/pegaprox/app.py
PathChanged=/opt/PegaProx/pegaprox/api/vms.py
PathChanged=/opt/PegaProx/web/src/node_modals.js
Unit=pegaprox-patch.service

[Install]
WantedBy=multi-user.target
EOF

if [ ! -f "$SERVICE_UNIT" ]; then
    cat > "$SERVICE_UNIT" << 'EOF'
[Unit]
Description=PegaProx auto-patch (docker_swarm plugin)
After=pegaprox.service

[Service]
Type=oneshot
ExecStart=/opt/PegaProx/plugins/docker_swarm/auto-patch.sh
TimeoutStartSec=300
EOF
fi

systemctl daemon-reload
systemctl enable --now pegaprox-patch.path > /dev/null 2>&1 || true
systemctl restart pegaprox-patch.path

echo "pegaprox-patch.path watcher active — monitors dashboard.js + app.py + vms.py"

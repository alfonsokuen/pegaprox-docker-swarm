#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Plugin — Self-Healing Nginx Fix Watcher
# ============================================================================
# Installs a systemd path unit that re-applies our nginx fixes whenever the
# PegaProx nginx config is rewritten by any external process (install.sh
# re-run, apt upgrade nginx, manual edits, etc.).
#
# This is the self-healing layer on top of the sub_filter injection:
#   layer 1: nginx sub_filter serves the CSS rule on every request
#   layer 2: THIS watcher re-wires the include if the main config is rewritten
#
# Idempotent. Called by install.sh and patch-pegaprox.sh.
# ============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PLUGIN_DIR="/opt/PegaProx/plugins/docker_swarm"
NGINX_CONF="/etc/nginx/sites-available/pegaprox"
PATH_UNIT="/etc/systemd/system/pegaprox-nginx-fix.path"
SERVICE_UNIT="/etc/systemd/system/pegaprox-nginx-fix.service"

if [ ! -f "$NGINX_CONF" ]; then
    echo -e "${YELLOW}skip: $NGINX_CONF not present (PegaProx not behind nginx)${NC}"
    exit 0
fi

if [ ! -f "$PLUGIN_DIR/patch_nginx_fixes.sh" ]; then
    echo -e "${RED}ERROR: $PLUGIN_DIR/patch_nginx_fixes.sh not found${NC}"
    exit 1
fi

# Always overwrite unit files so any improvements ship with plugin updates.
cat > "$PATH_UNIT" << EOF
[Unit]
Description=Watch PegaProx nginx config for rewrites and re-apply DS fixes
Documentation=https://github.com/alfonsokuen/pegaprox-docker-swarm
After=nginx.service

[Path]
PathModified=$NGINX_CONF

[Install]
WantedBy=multi-user.target
EOF

cat > "$SERVICE_UNIT" << EOF
[Unit]
Description=Re-apply DS permanent nginx fixes after config rewrites
After=nginx.service

[Service]
Type=oneshot
ExecStart=/bin/bash $PLUGIN_DIR/patch_nginx_fixes.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pegaprox-nginx-fix
EOF

systemctl daemon-reload
systemctl enable pegaprox-nginx-fix.path > /dev/null 2>&1
systemctl restart pegaprox-nginx-fix.path

if systemctl is-active --quiet pegaprox-nginx-fix.path; then
    echo -e "${GREEN}self-healing watcher active: pegaprox-nginx-fix.path${NC}"
    echo "      monitors: $NGINX_CONF"
    echo "      re-runs : $PLUGIN_DIR/patch_nginx_fixes.sh"
else
    echo -e "${RED}ERROR: pegaprox-nginx-fix.path failed to start${NC}"
    systemctl status pegaprox-nginx-fix.path --no-pager -l | tail -10
    exit 1
fi

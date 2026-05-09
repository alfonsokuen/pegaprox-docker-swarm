#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Manager Plugin — Uninstaller (v1.16.0+)
# ============================================================================
# Usage: sudo bash /opt/PegaProx/plugins/docker_swarm/uninstall.sh
# ============================================================================

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

PEGAPROX_DIR="/opt/PegaProx"
PLUGIN_DIR="$PEGAPROX_DIR/plugins/docker_swarm"
DB="$PEGAPROX_DIR/config/pegaprox.db"

echo ""
echo "PegaProx Docker Swarm Plugin — Uninstaller"
echo "==========================================="
echo ""

read -p "This will remove the Docker Swarm plugin. Continue? [y/N]: " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy] ]]; then
    echo "Cancelled."
    exit 0
fi

# 1. Stop and remove legacy systemd watchers (no-op for v1.16.0+ fresh installs,
#    but cleans up old units left over from <=v1.15.x).
echo -n "[1/4] Removing legacy systemd watchers... "
for unit in pegaprox-swarm-patch.path pegaprox-swarm-patch.service pegaprox-nginx-fix.path pegaprox-nginx-fix.service; do
    systemctl stop "$unit" 2>/dev/null || true
    systemctl disable "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
echo -e "${GREEN}OK${NC}"

# 2. Disable plugin in DB
echo -n "[2/4] Disabling plugin... "
if [ -f "$DB" ] && command -v sqlite3 &>/dev/null; then
    sqlite3 "$DB" "DELETE FROM plugin_state WHERE plugin_id='docker_swarm'" 2>/dev/null
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}manual — disable via PegaProx UI${NC}"
fi

# 3. Remove plugin files
echo -n "[3/4] Removing plugin files... "
rm -rf "$PLUGIN_DIR"
echo -e "${GREEN}OK${NC}"

# 4. Restart PegaProx (no dashboard.js patches to revert under v1.16.0)
echo -n "[4/4] Restarting PegaProx... "
systemctl restart pegaprox && sleep 2
systemctl is-active --quiet pegaprox && echo -e "${GREEN}OK${NC}" || echo -e "${RED}FAILED${NC}"

echo ""
echo -e "${GREEN}Docker Swarm plugin uninstalled.${NC}"
echo ""
echo -e "${YELLOW}NOTE:${NC} Nginx VNC console fixes (CSS sub_filter) remain in place if installed."
echo "      They are harmless without the plugin; remove manually from"
echo "      /etc/nginx/sites-available/pegaprox if desired."
echo ""

#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Manager Plugin — Uninstaller
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

# 1. Stop and remove systemd watcher
echo -n "[1/5] Removing auto-patch watcher... "
systemctl stop pegaprox-swarm-patch.path 2>/dev/null || true
systemctl disable pegaprox-swarm-patch.path 2>/dev/null || true
rm -f /etc/systemd/system/pegaprox-swarm-patch.path
rm -f /etc/systemd/system/pegaprox-swarm-patch.service
systemctl daemon-reload
echo -e "${GREEN}OK${NC}"

# 2. Disable plugin in DB
echo -n "[2/5] Disabling plugin... "
if [ -f "$DB" ] && command -v sqlite3 &>/dev/null; then
    sqlite3 "$DB" "DELETE FROM plugin_state WHERE plugin_id='docker_swarm'" 2>/dev/null
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}manual — disable via PegaProx UI${NC}"
fi

# 3. Remove sidebar patches from dashboard.js
echo -n "[3/5] Removing dashboard patches... "
if [ -f "$PEGAPROX_DIR/web/src/dashboard.js.bak" ]; then
    cp "$PEGAPROX_DIR/web/src/dashboard.js.bak" "$PEGAPROX_DIR/web/src/dashboard.js"
    echo -e "${GREEN}restored from backup${NC}"
elif [ -f "$PEGAPROX_DIR/web/src/dashboard.js.bak2" ]; then
    cp "$PEGAPROX_DIR/web/src/dashboard.js.bak2" "$PEGAPROX_DIR/web/src/dashboard.js"
    echo -e "${GREEN}restored from backup${NC}"
else
    echo -e "${YELLOW}no backup found — run PegaProx update to get clean version${NC}"
fi

# 4. Restore CSP
echo -n "[4/5] Restoring CSP... "
if [ -f "$PEGAPROX_DIR/pegaprox/app.py" ]; then
    sed -i "s/frame-ancestors 'self'/frame-ancestors 'none'/" "$PEGAPROX_DIR/pegaprox/app.py"
    sed -i "s/X-Frame-Options'] = 'SAMEORIGIN'/X-Frame-Options'] = 'DENY'/" "$PEGAPROX_DIR/pegaprox/app.py"
    echo -e "${GREEN}OK${NC}"
fi

# 5. Remove plugin files
echo -n "[5/5] Removing plugin files... "
rm -rf "$PLUGIN_DIR"
echo -e "${GREEN}OK${NC}"

# Rebuild and restart
echo ""
echo -n "Rebuilding frontend... "
cd "$PEGAPROX_DIR"
if command -v node &>/dev/null; then
    bash web/Dev/build.sh > /dev/null 2>&1 && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}warnings${NC}"
else
    bash web/Dev/build.sh --restore > /dev/null 2>&1 && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}warnings${NC}"
fi

echo -n "Restarting PegaProx... "
systemctl restart pegaprox && sleep 2
systemctl is-active --quiet pegaprox && echo -e "${GREEN}OK${NC}" || echo -e "${RED}FAILED${NC}"

echo ""
echo -e "${GREEN}Docker Swarm plugin uninstalled.${NC}"
echo ""

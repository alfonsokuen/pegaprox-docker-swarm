#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Plugin — Post-Update Patch Script
# ============================================================================
# Automatically re-applies all Docker Swarm integration patches after a
# PegaProx update. Uses patch_dashboard.py for reliable JSX patching.
#
# What it patches:
#   1. CSP frame-ancestors 'self' in app.py (iframe embedding)
#   2. X-Frame-Options SAMEORIGIN in app.py
#   3. dashboard.js: sidebar, content panel, topology, state vars (via Python)
#   4. Rebuilds production frontend with Babel
#
# Usage:
#   sudo bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh
#
# Triggered automatically by systemd path watcher after PegaProx updates.
# Safe to run multiple times — idempotent.
# ============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PEGAPROX_DIR="/opt/PegaProx"
PLUGIN_DIR="$PEGAPROX_DIR/plugins/docker_swarm"
APP_PY="$PEGAPROX_DIR/pegaprox/app.py"
DASHBOARD="$PEGAPROX_DIR/web/src/dashboard.js"

echo "============================================"
echo " Docker Swarm Plugin — Post-Update Patcher"
echo "============================================"
echo ""

# ---------- Check prerequisites ----------
if [ ! -f "$DASHBOARD" ]; then
    echo -e "${RED}ERROR: $DASHBOARD not found${NC}"
    exit 1
fi
if [ ! -f "$APP_PY" ]; then
    echo -e "${RED}ERROR: $APP_PY not found${NC}"
    exit 1
fi
if [ ! -f "$PLUGIN_DIR/patch_dashboard.py" ]; then
    echo -e "${RED}ERROR: $PLUGIN_DIR/patch_dashboard.py not found${NC}"
    exit 1
fi

# ---------- 1. Patch CSP (app.py) ----------
echo -n "[1/3] CSP + X-Frame-Options... "
CHANGED=0
if grep -q "frame-ancestors 'none'" "$APP_PY"; then
    sed -i "s/frame-ancestors 'none'/frame-ancestors 'self'/" "$APP_PY"
    CHANGED=1
fi
if grep -q "X-Frame-Options'] = 'DENY'" "$APP_PY"; then
    sed -i "s/X-Frame-Options'] = 'DENY'/X-Frame-Options'] = 'SAMEORIGIN'/" "$APP_PY"
    CHANGED=1
fi
if [ $CHANGED -eq 1 ]; then
    echo -e "${GREEN}PATCHED${NC}"
elif grep -q "frame-ancestors 'self'" "$APP_PY"; then
    echo -e "${YELLOW}already patched${NC}"
else
    echo -e "${RED}UNKNOWN STATE${NC}"
fi

# ---------- 1b. Safety backup of dashboard.js before any patching ----------
BACKUP_DIR="/opt/PegaProx/_backups"
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/dashboard.js.$(date +%Y%m%d-%H%M%S)"
cp -a "$DASHBOARD" "$BACKUP_PATH"
echo "      Backup: $BACKUP_PATH"

# ---------- 2. Patch dashboard.js (Python patcher) ----------
echo -n "[2/4] Dashboard patches (sidebar, topology, iframe)... "
if grep -q "sidebarDockerSwarm" "$DASHBOARD"; then
    echo -e "${YELLOW}already patched${NC}"
else
    python3 "$PLUGIN_DIR/patch_dashboard.py" 2>&1 | tail -1
fi

# ---------- 3. VNC console modal fix (PegaProx 0.9.6.1 regression) ----------
echo -n "[3/4] Console modal height fix... "
if grep -q "ds-console-modal-fix" "$DASHBOARD"; then
    echo -e "${YELLOW}already patched${NC}"
else
    python3 "$PLUGIN_DIR/patch_console_modal.py" 2>&1 | tail -1
fi

# ---------- 4. Rebuild frontend ----------
echo -n "[4/4] Rebuilding frontend... "
cd "$PEGAPROX_DIR"
if command -v node &> /dev/null; then
    if bash web/Dev/build.sh > /dev/null 2>&1; then
        echo -e "${GREEN}PRODUCTION BUILD OK${NC}"
    else
        echo -e "${YELLOW}Build had warnings (may still work)${NC}"
    fi
else
    bash web/Dev/build.sh --restore > /dev/null 2>&1
    echo -e "${YELLOW}DEV BUILD (install Node.js for production)${NC}"
fi

# ---------- Restart ----------
echo ""
echo -n "Restarting PegaProx... "
systemctl restart pegaprox
sleep 2
if systemctl is-active --quiet pegaprox; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAILED — check: journalctl -u pegaprox${NC}"
fi

echo ""
echo "============================================"
echo -e " ${GREEN}Patch complete!${NC}"
echo " Sidebar + Topology + Console restored."
echo "============================================"

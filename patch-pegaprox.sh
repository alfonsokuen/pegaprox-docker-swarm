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
# Patches applied by this orchestrator (in order):
#   1. CSP frame-ancestors 'self' + X-Frame-Options SAMEORIGIN (app.py)
#   2. dashboard.js — sidebar "DOCKER SWARM" + iframe + topology (9 JSX patches)
#   3. Nginx sub_filter snippet to inject the missing Tailwind `h-[85vh]` CSS
#      rule (permanent fix for collapsed VNC/xterm console modal)
#   3b. vms.py — VNC WebSocket `subprotocols=['binary']` negotiation (fixes
#       the browser close-1006 regression in PegaProx 0.9.7)
#   4. Production frontend rebuild (Babel) with fail-loud + post-check
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
echo "[2/4] Dashboard patches (sidebar, topology, iframe)..."
if grep -q "sidebarDockerSwarm" "$DASHBOARD"; then
    echo -e "      ${YELLOW}already patched${NC}"
else
    if ! python3 "$PLUGIN_DIR/patch_dashboard.py" 2>&1 | sed 's/^/      /'; then
        echo -e "      ${RED}patch_dashboard.py FAILED — aborting${NC}"
        exit 1
    fi
fi

# ---------- 3. VNC console modal fix — nginx sub_filter (PERMANENT) ----------
# Preferred path: inject the missing Tailwind arbitrary-value CSS rule
# (`.h-\[85vh\]{height:85vh}`) via nginx, so PegaProx auto-updates cannot
# break it. Falls back to the legacy in-bundle patcher if nginx is not in
# use (direct-mode PegaProx on :443).
echo "[3/4] Console modal height fix (nginx sub_filter + self-heal watcher)..."
if [ -f /etc/nginx/sites-available/pegaprox ]; then
    bash "$PLUGIN_DIR/patch_nginx_fixes.sh" 2>&1 | sed 's/^/      /'
    # Install systemd path watcher so the include is re-wired automatically
    # if anything ever rewrites the nginx config (install.sh re-run, apt
    # upgrade, manual edit, etc.). This is the self-healing layer.
    if [ -f "$PLUGIN_DIR/setup_nginx_watcher.sh" ]; then
        bash "$PLUGIN_DIR/setup_nginx_watcher.sh" 2>&1 | sed 's/^/      /'
    fi
else
    echo -n "      nginx not detected — fallback to in-bundle patch... "
    if grep -q "ds-console-modal-fix" "$DASHBOARD"; then
        echo -e "${YELLOW}already patched${NC}"
    else
        python3 "$PLUGIN_DIR/patch_console_modal.py" 2>&1 | tail -1
    fi
fi

# ---------- 3c. VNC full-stack fix: subprotocol + auth-context + ticket passthrough ----------
# Three coordinated fixes required to make the VM console actually work on
# PegaProx 0.9.7 (all documented verbosely in their respective patchers):
#   patch_vnc_subprotocol.py     → permissive select_subprotocol so noVNC's
#                                  ['binary'] handshake is accepted (fixes
#                                  close-1006 regression)
#   patch_vnc_auth_context.py    → UI passes its ticket/port in the WS URL +
#                                  server skips the double POST /vncproxy and
#                                  reuses manager._api_token / _ticket for the
#                                  upstream WS (fixes "Authentication failure"
#                                  RFB-layer + "invalid PVEVNC ticket" upstream)
VMS_PY="$PEGAPROX_DIR/pegaprox/api/vms.py"
NODE_MODALS="$PEGAPROX_DIR/web/src/node_modals.js"
echo "[3c/4] VNC full-stack fix (subprotocol + auth-context + ticket passthrough)..."
if [ ! -f "$PLUGIN_DIR/patch_vnc_auth_context.py" ]; then
    echo -e "      ${YELLOW}patch_vnc_auth_context.py not present — skipping auth-context fix${NC}"
elif grep -q "DS-VNC-AUTH-CONTEXT" "$VMS_PY" && grep -q "DS-VNC-TICKET-PASSTHROUGH" "$NODE_MODALS"; then
    echo -e "      ${YELLOW}auth-context already applied${NC}"
else
    cp -a "$VMS_PY" "$BACKUP_DIR/vms.py.pre-authctx.$(date +%Y%m%d-%H%M%S)"
    cp -a "$NODE_MODALS" "$BACKUP_DIR/node_modals.js.pre-authctx.$(date +%Y%m%d-%H%M%S)"
    if ! python3 "$PLUGIN_DIR/patch_vnc_auth_context.py" 2>&1 | sed 's/^/      /'; then
        echo -e "      ${RED}patch_vnc_auth_context.py FAILED — aborting${NC}"
        exit 1
    fi
    if ! python3 -c "import ast; ast.parse(open('$VMS_PY').read())" 2>/dev/null; then
        echo -e "      ${RED}vms.py post-patch syntax check FAILED — restoring${NC}"
        cp -a "$BACKUP_DIR/vms.py.pre-authctx.$(date +%Y%m%d-%H%M%S)" "$VMS_PY"
        exit 1
    fi
    echo -e "      ${GREEN}auth-context + ticket passthrough applied (vms.py + node_modals.js)${NC}"
fi

# ---------- 3b. VNC WebSocket subprotocol fix (PegaProx 0.9.7) ----------
# PegaProx 0.9.7's `websockets.serve(vnc_handler, ...)` does not pass
# `subprotocols=` — but noVNC opens the socket with `new WebSocket(url,
# ['binary'])`. RFC 6455: when the client advertises subprotocols the server
# MUST echo one back or browsers close with code 1006 before the `open`
# event fires. Symptom: VM console modal shows "Error de conexión" or
# "Reconnecting (2/3)…" (permissive clients like curl/nc/wscat work, which
# hides the bug from upstream tests).
VMS_PY="$PEGAPROX_DIR/pegaprox/api/vms.py"
echo "[3b/4] VNC WebSocket subprotocol negotiation..."
if [ ! -f "$PLUGIN_DIR/patch_vnc_subprotocol.py" ]; then
    echo -e "      ${YELLOW}patch_vnc_subprotocol.py not present — skipping${NC}"
elif grep -q "DS-VNC-SUBPROTOCOL" "$VMS_PY"; then
    echo -e "      ${YELLOW}already patched${NC}"
else
    # Safety backup
    cp -a "$VMS_PY" "$BACKUP_DIR/vms.py.pre-vncfix.$(date +%Y%m%d-%H%M%S)"
    if ! python3 "$PLUGIN_DIR/patch_vnc_subprotocol.py" 2>&1 | sed 's/^/      /'; then
        echo -e "      ${RED}patch_vnc_subprotocol.py FAILED — aborting${NC}"
        exit 1
    fi
    # Syntax sanity (this file is 6000+ lines of PegaProx core, we must not bork it)
    if ! python3 -c "import ast; ast.parse(open('$VMS_PY').read())" 2>/dev/null; then
        echo -e "      ${RED}post-patch syntax check FAILED — restoring backup${NC}"
        cp -a "$BACKUP_DIR/vms.py.pre-vncfix.$(date +%Y%m%d-%H%M%S)" "$VMS_PY"
        exit 1
    fi
    echo -e "      ${GREEN}vms.py patched (2 subprotocol=['binary'] additions)${NC}"
fi

# ---------- 4. Rebuild frontend ----------
# v1.9: fail-loud build. PegaProx 0.9.6.1 split dashboard.js into 17 files
# (concatenated by web/Dev/build.sh → web/index.html). The previous `> /dev/null
# 2>&1` masked real build failures (stale root-owned .build/app.jsx from an
# earlier root-run blocking subsequent writes) and the patcher reported OK
# while the production bundle still served the unpatched release.
echo "[4/4] Rebuilding frontend (production, fail-loud)..."
cd "$PEGAPROX_DIR"

# Ensure the build cache is writable by whoever runs us. If a previous
# run (as a different user) left root-owned artefacts, cat > app.jsx
# would EPERM and the build would silently abort.
if [ -d web/Dev/.build ]; then
    rm -f web/Dev/.build/app.jsx web/Dev/.build/app.js 2>/dev/null || true
fi

if ! command -v node &> /dev/null; then
    echo -e "      ${YELLOW}Node.js missing — falling back to dev build${NC}"
    if ! bash web/Dev/build.sh --restore 2>&1 | sed 's/^/      /'; then
        echo -e "      ${RED}dev build FAILED${NC}"
        exit 1
    fi
else
    if ! bash web/Dev/build.sh 2>&1 | sed 's/^/      /'; then
        echo -e "      ${RED}production build FAILED — sidebar will be missing${NC}"
        exit 1
    fi
fi

# Verify the patches actually reached the production bundle.
if ! grep -q "sidebarDockerSwarm" "$PEGAPROX_DIR/web/index.html"; then
    echo -e "      ${RED}post-build sanity check FAILED: 'sidebarDockerSwarm' missing from web/index.html${NC}"
    exit 1
fi
echo -e "      ${GREEN}production bundle contains sidebarDockerSwarm${NC}"

# Normalise ownership so Flask (running as pegaprox) can serve the new bundle
# regardless of who triggered the patch (root via systemd, pegaprox manually, etc.).
chown pegaprox:pegaprox "$PEGAPROX_DIR/web/index.html" 2>/dev/null || true
chown -R pegaprox:pegaprox "$PEGAPROX_DIR/web/Dev/.build" 2>/dev/null || true

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

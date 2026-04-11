#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Plugin — Post-Update Patch Script
# ============================================================================
# Run this after every PegaProx update to re-apply:
#   1. Docker Swarm sidebar entry in dashboard.js
#   2. CSP frame-ancestors 'self' in app.py (for iframe embedding)
#   3. Rebuild production frontend
#
# Usage:
#   sudo bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh
#
# Safe to run multiple times — checks if patches are already applied.
# ============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PEGAPROX_DIR="/opt/PegaProx"
DASHBOARD="$PEGAPROX_DIR/web/src/dashboard.js"
APP_PY="$PEGAPROX_DIR/pegaprox/app.py"

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

# ---------- 1. Patch CSP (app.py) ----------
echo -n "[1/4] CSP frame-ancestors... "
if grep -q "frame-ancestors 'none'" "$APP_PY"; then
    sed -i "s/frame-ancestors 'none'/frame-ancestors 'self'/" "$APP_PY"
    sed -i "s/X-Frame-Options'] = 'DENY'/X-Frame-Options'] = 'SAMEORIGIN'/" "$APP_PY"
    echo -e "${GREEN}PATCHED${NC}"
elif grep -q "frame-ancestors 'self'" "$APP_PY"; then
    echo -e "${YELLOW}already patched${NC}"
else
    echo -e "${RED}UNKNOWN STATE — check manually${NC}"
fi

# ---------- 2. Add sidebarDockerSwarm state variable ----------
echo -n "[2/4] Sidebar state variable... "
if grep -q "sidebarDockerSwarm" "$DASHBOARD"; then
    echo -e "${YELLOW}already present${NC}"
else
    # Add after sidebarXHM state
    sed -i '/const \[sidebarXHM, setSidebarXHM\]/a\            const [sidebarDockerSwarm, setSidebarDockerSwarm] = useState(false);' "$DASHBOARD"
    # Add to auto-clear effect
    sed -i 's/setSidebarTopology(false); setSidebarXHM(false);/setSidebarTopology(false); setSidebarXHM(false); setSidebarDockerSwarm(false);/g' "$DASHBOARD"
    # Add to condition checks
    sed -i 's/!selectedGroup && !sidebarXHM/!selectedGroup \&\& !sidebarXHM \&\& !sidebarDockerSwarm/g' "$DASHBOARD"
    sed -i 's/|| sidebarXHM)/|| sidebarXHM || sidebarDockerSwarm)/g' "$DASHBOARD"
    echo -e "${GREEN}PATCHED${NC}"
fi

# ---------- 3. Add Docker Swarm sidebar section ----------
echo -n "[3/4] Sidebar Docker Swarm section... "
if grep -q "Docker Swarm" "$DASHBOARD"; then
    echo -e "${YELLOW}already present${NC}"
else
    # Find the XHM sidebar line and insert Docker Swarm section before it
    XHM_LINE=$(grep -n "XHM sidebar.*only when both" "$DASHBOARD" | head -1 | cut -d: -f1)
    if [ -n "$XHM_LINE" ]; then
        # Create the sidebar patch
        cat > /tmp/_ds_sidebar_patch.js << 'SIDEBAR_EOF'
                                {/* Docker Swarm Manager Plugin */}
                                <div className="mt-4 pt-4 border-t border-proxmox-border">
                                    <div className="flex items-center justify-between px-1 mb-2">
                                        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">Docker Swarm</h2>
                                    </div>
                                    <div className="space-y-1.5">
                                        <button
                                            onClick={() => { setSidebarDockerSwarm(true); setSidebarTopology(false); setSidebarXHM(false); setSelectedCluster(null); setSelectedPBS(null); setSelectedVMware(null); setSelectedGroup(null); }}
                                            className={isCorporate
                                                ? "w-full flex items-center gap-1.5 pl-3 pr-2 py-0.5 text-[13px] leading-5"
                                                : `w-full flex items-center gap-3 px-3 py-2 rounded-xl transition-all ${
                                                    sidebarDockerSwarm
                                                        ? "bg-gradient-to-r from-cyan-500/20 to-blue-600/10 border border-cyan-500/30 text-white"
                                                        : "bg-proxmox-card border border-proxmox-border hover:border-cyan-500/30 text-gray-300 hover:text-white"
                                                  }`
                                            }
                                            style={isCorporate ? (sidebarDockerSwarm ? {background: "rgba(73,175,217,0.10)", borderLeft: "2px solid var(--corp-accent)", color: "var(--color-text)"} : {color: "var(--corp-text-secondary)"}) : undefined}
                                            onMouseEnter={isCorporate ? (e) => { if (!sidebarDockerSwarm) { e.currentTarget.style.background = "var(--color-hover)"; e.currentTarget.style.color = "var(--color-text)"; }} : undefined}
                                            onMouseLeave={isCorporate ? (e) => { if (!sidebarDockerSwarm) { e.currentTarget.style.background = ""; e.currentTarget.style.color = "var(--corp-text-secondary)"; }} : undefined}
                                        >
                                            {isCorporate ? (
                                                <Icons.Box className="w-4 h-4 flex-shrink-0" style={{color: sidebarDockerSwarm ? "var(--corp-accent)" : "#2dd4bf"}} />
                                            ) : (
                                                <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${sidebarDockerSwarm ? "bg-cyan-500/20" : "bg-proxmox-dark"}`}>
                                                    <Icons.Box className="w-4 h-4 text-cyan-400" />
                                                </div>
                                            )}
                                            <div className="flex-1 text-left min-w-0">
                                                <div className={`${isCorporate ? "text-[13px]" : "text-sm"} font-medium truncate`}>Swarm Cluster</div>
                                                {!isCorporate && <div className="text-xs text-gray-500 truncate">Docker Swarm</div>}
                                            </div>
                                            <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{background: "var(--color-success)"}} />
                                        </button>
                                    </div>
                                </div>
SIDEBAR_EOF
        # Insert before XHM line
        sed -i "$((XHM_LINE-1))r /tmp/_ds_sidebar_patch.js" "$DASHBOARD"
        rm -f /tmp/_ds_sidebar_patch.js

        # Now add the content panel (iframe) — find sidebarXHM content panel
        XHM_CONTENT=$(grep -n ") : sidebarXHM ? (" "$DASHBOARD" | head -1 | cut -d: -f1)
        if [ -n "$XHM_CONTENT" ]; then
            cat > /tmp/_ds_content_patch.js << 'CONTENT_EOF'
                                ) : sidebarDockerSwarm ? (
                                    /* Docker Swarm Manager Plugin - embedded view */
                                    <div style={{height: "calc(100vh - 48px)", display: "flex", flexDirection: "column"}}>
                                        {isCorporate && (
                                            <div className="corp-content-header">
                                                <div className="flex items-center gap-2">
                                                    <Icons.Box className="w-4 h-4" style={{color: "#2dd4bf"}} />
                                                    <span className="corp-header-title">Docker Swarm Manager</span>
                                                </div>
                                            </div>
                                        )}
                                        <iframe
                                            src="/api/plugins/docker_swarm/api/ui"
                                            style={{flex: 1, border: "none", width: "100%", height: "100%", background: "#0f1117", borderRadius: isCorporate ? "0" : "12px"}}
                                            title="Docker Swarm Manager"
                                        />
                                    </div>
CONTENT_EOF
            sed -i "$((XHM_CONTENT-1))r /tmp/_ds_content_patch.js" "$DASHBOARD"
            rm -f /tmp/_ds_content_patch.js
        fi
        echo -e "${GREEN}PATCHED${NC}"
    else
        echo -e "${RED}XHM sidebar marker not found — PegaProx version may be incompatible${NC}"
    fi
fi

# ---------- 4. Rebuild frontend ----------
echo -n "[4/4] Rebuilding frontend... "
if command -v node &> /dev/null; then
    cd "$PEGAPROX_DIR"
    bash web/Dev/build.sh > /dev/null 2>&1
    echo -e "${GREEN}PRODUCTION BUILD OK${NC}"
else
    cd "$PEGAPROX_DIR"
    bash web/Dev/build.sh --restore > /dev/null 2>&1
    echo -e "${YELLOW}DEV BUILD (install Node.js for production build)${NC}"
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
echo " Docker Swarm sidebar + iframe restored."
echo "============================================"

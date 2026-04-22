#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Plugin — Nginx Permanent CSS Fixes
# ============================================================================
# Injects CSS rules via nginx sub_filter so they survive PegaProx auto-updates
# without ever touching dashboard.js. This is the *permanent* replacement for
# patch_console_modal.py (which was fragile: rewritten every time the bundle
# was regenerated).
#
# Root cause (v0.9.6.1+):
#   PegaProx templates use the Tailwind arbitrary-value class `h-[85vh]` on
#   the console modal card, but the precompiled /static/css/tailwind.min.css
#   shipped with PegaProx does NOT contain the matching rule. Result: the
#   class has no CSS → height falls back to auto → modal collapses to the
#   header (~73px) and the noVNC/xterm canvas is measured as 0x0.
#
# Fix: one CSS rule (`.h-\[85vh\]{height:85vh}`) served from an nginx snippet
# and injected into every HTML response before </head>.
#
# Idempotent. Safe to run on every patch cycle.
# ============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

NGINX_CONF="/etc/nginx/sites-available/pegaprox"
SNIPPET_DIR="/etc/nginx/snippets"
SNIPPET="$SNIPPET_DIR/pegaprox-ds-fixes.conf"
MARKER="# DS-PEGAPROX-FIXES"

if [ ! -f "$NGINX_CONF" ]; then
    echo -e "${YELLOW}skip: $NGINX_CONF not present (PegaProx not behind nginx)${NC}"
    exit 0
fi

mkdir -p "$SNIPPET_DIR"

# Always (re)write the snippet so rule text stays in sync with the plugin repo.
cat > "$SNIPPET" << 'EOF'
# PegaProx Docker Swarm plugin — permanent CSS fixes
# Update-proof: lives in nginx, never in dashboard.js.
# Managed by /opt/PegaProx/plugins/docker_swarm/patch_nginx_fixes.sh
# Do not edit by hand — it is overwritten on every patch cycle.

sub_filter_once on;
sub_filter_types text/html;
sub_filter '</head>' '<style id="ds-permanent-fixes">.h-\[85vh\]{height:85vh}</style></head>';
proxy_set_header Accept-Encoding "";
EOF

echo "snippet: $SNIPPET (refreshed)"

if grep -q "$MARKER" "$NGINX_CONF"; then
    echo -e "${YELLOW}already wired: marker present in $NGINX_CONF${NC}"
else
    # Inject `include snippets/pegaprox-ds-fixes.conf;` inside the `location / {}`
    # block so sub_filter applies only to HTML responses served by Flask, not to
    # the websocket locations for VNC/SSH.
    if ! grep -q '^[[:space:]]*location / {' "$NGINX_CONF"; then
        echo -e "${RED}ERROR: could not find 'location / {' anchor in $NGINX_CONF${NC}"
        echo -e "${RED}Skipping inject — please add manually: include snippets/pegaprox-ds-fixes.conf;${NC}"
        exit 1
    fi
    sed -i "/^[[:space:]]*location \/ {/a\\        include snippets/pegaprox-ds-fixes.conf;  $MARKER" "$NGINX_CONF"
    echo -e "${GREEN}wired: include added to $NGINX_CONF${NC}"
fi

if nginx -t > /dev/null 2>&1; then
    systemctl reload nginx
    echo -e "${GREEN}nginx reloaded${NC}"
else
    echo -e "${RED}nginx -t FAILED — config not reloaded. Run: nginx -t${NC}"
    exit 1
fi

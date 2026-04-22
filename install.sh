#!/bin/bash
# ============================================================================
# PegaProx Docker Swarm Manager Plugin — One-Line Installer
# ============================================================================
#
# Install:
#   curl -sSL https://raw.githubusercontent.com/alfonsokuen/pegaprox-docker-swarm/main/install.sh | sudo bash
#
# What it does:
#   1. Downloads the plugin to /opt/PegaProx/plugins/docker_swarm/
#   2. Prompts for Swarm manager SSH credentials
#   3. Enables the plugin in PegaProx
#   4. Injects Docker Swarm into PegaProx sidebar + topology
#   5. Optionally installs nginx reverse proxy (fixes VNC console via tunnels)
#   6. Sets up auto-patch for PegaProx updates persistence
#   7. Rebuilds frontend and restarts PegaProx
#
# Requirements:
#   - PegaProx 0.9.0+ installed at /opt/PegaProx
#   - Root access
#   - Docker Swarm cluster with SSH access to at least one manager node
#
# Uninstall:
#   sudo bash /opt/PegaProx/plugins/docker_swarm/uninstall.sh
#
# ============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

REPO_URL="https://github.com/alfonsokuen/pegaprox-docker-swarm"
PEGAPROX_DIR="/opt/PegaProx"
PLUGIN_DIR="$PEGAPROX_DIR/plugins/docker_swarm"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   ${BOLD}PegaProx Docker Swarm Manager Plugin — Installer${NC}${CYAN}          ║${NC}"
echo -e "${CYAN}║   Monitor & manage Docker Swarm from PegaProx              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Prerequisites ──
echo -e "${BLUE}[1/7] Checking prerequisites...${NC}"

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}ERROR: Must run as root (sudo)${NC}"
    exit 1
fi

if [ ! -d "$PEGAPROX_DIR" ]; then
    echo -e "${RED}ERROR: PegaProx not found at $PEGAPROX_DIR${NC}"
    exit 1
fi

if [ ! -f "$PEGAPROX_DIR/pegaprox/app.py" ]; then
    echo -e "${RED}ERROR: PegaProx installation appears incomplete${NC}"
    exit 1
fi

PEGAPROX_VERSION=$(grep 'PEGAPROX_VERSION' "$PEGAPROX_DIR/pegaprox/constants.py" 2>/dev/null | head -1 | grep -oP '[\d.]+' || echo "unknown")
echo -e "  PegaProx ${GREEN}$PEGAPROX_VERSION${NC} found at $PEGAPROX_DIR"

if ! command -v python3 &>/dev/null; then
    echo -e "${RED}ERROR: Python 3 not found${NC}"
    exit 1
fi
echo -e "  Python $(python3 --version 2>&1 | awk '{print $2}') ${GREEN}OK${NC}"

if ! python3 -c "import paramiko" 2>/dev/null; then
    if [ -f "$PEGAPROX_DIR/venv/bin/python" ]; then
        if "$PEGAPROX_DIR/venv/bin/python" -c "import paramiko" 2>/dev/null; then
            echo -e "  paramiko ${GREEN}OK${NC} (in PegaProx venv)"
        else
            echo -e "${RED}ERROR: paramiko not found — required for SSH to Swarm nodes${NC}"
            exit 1
        fi
    fi
else
    echo -e "  paramiko ${GREEN}OK${NC}"
fi

# ── Download Plugin ──
echo ""
echo -e "${BLUE}[2/7] Downloading plugin...${NC}"

if [ -d "$PLUGIN_DIR" ]; then
    echo -e "  ${YELLOW}Plugin directory exists — updating...${NC}"
    # Backup config if exists
    if [ -f "$PLUGIN_DIR/config.json" ]; then
        cp "$PLUGIN_DIR/config.json" /tmp/_ds_config_backup.json
        echo "  Backed up existing config.json"
    fi
fi

# Download via git or tarball
if command -v git &>/dev/null; then
    if [ -d "$PLUGIN_DIR/.git" ]; then
        cd "$PLUGIN_DIR" && git pull --quiet
        echo -e "  ${GREEN}Updated via git pull${NC}"
    else
        rm -rf "$PLUGIN_DIR"
        git clone --quiet "$REPO_URL.git" "$PLUGIN_DIR"
        echo -e "  ${GREEN}Cloned from GitHub${NC}"
    fi
else
    rm -rf "$PLUGIN_DIR"
    mkdir -p "$PLUGIN_DIR"
    curl -sSL "$REPO_URL/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 -C "$PLUGIN_DIR"
    echo -e "  ${GREEN}Downloaded from GitHub${NC}"
fi

# Restore config backup if it existed
if [ -f /tmp/_ds_config_backup.json ]; then
    cp /tmp/_ds_config_backup.json "$PLUGIN_DIR/config.json"
    rm -f /tmp/_ds_config_backup.json
    echo "  Restored existing config.json"
fi

# ── Configure SSH connection (key-based auth) ──
echo ""
echo -e "${BLUE}[3/7] Configuring Swarm connection (SSH key auth)...${NC}"

SSH_DIR="$PLUGIN_DIR/.ssh"
SSH_KEY="$SSH_DIR/id_ed25519"

# Generate SSH keypair if it doesn't exist
if [ ! -f "$SSH_KEY" ]; then
    mkdir -p "$SSH_DIR"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "pegaprox-docker-swarm" > /dev/null 2>&1
    echo -e "  ${GREEN}SSH keypair generated${NC} at $SSH_KEY"
else
    echo -e "  ${YELLOW}SSH keypair already exists${NC} at $SSH_KEY"
fi
chmod 700 "$SSH_DIR"
chmod 600 "$SSH_KEY"
chmod 644 "$SSH_KEY.pub"

if [ -f "$PLUGIN_DIR/config.json" ]; then
    echo -e "  ${YELLOW}config.json already exists — keeping current config${NC}"
    echo "  (Edit $PLUGIN_DIR/config.json or use the plugin Settings tab to change)"
else
    echo ""
    echo -e "  ${BOLD}Enter your Docker Swarm manager SSH details:${NC}"
    echo "  (You can add more nodes later via the plugin Settings tab)"
    echo ""

    read -p "  Swarm manager hostname/IP: " SWARM_HOST
    read -p "  SSH username: " SWARM_USER
    read -p "  Friendly name [swarm-manager]: " SWARM_NAME
    SWARM_NAME=${SWARM_NAME:-swarm-manager}

    cat > "$PLUGIN_DIR/config.json" << CFGEOF
{
    "swarm_hosts": [
        {
            "name": "$SWARM_NAME",
            "host": "$SWARM_HOST",
            "user": "$SWARM_USER",
            "key_file": "$SSH_KEY"
        }
    ],
    "poll_interval": 30
}
CFGEOF
    echo -e "  ${GREEN}config.json created (key-based auth, no passwords stored)${NC}"

    # Distribute public key to the Swarm host
    echo ""
    echo -e "  ${BOLD}Distributing SSH public key to $SWARM_HOST...${NC}"
    echo -e "  ${YELLOW}You will be asked for the SSH password ONE TIME to copy the key.${NC}"
    echo -e "  ${YELLOW}After this, no password will be stored anywhere.${NC}"
    echo ""
    if ssh-copy-id -i "$SSH_KEY.pub" -o StrictHostKeyChecking=accept-new "${SWARM_USER}@${SWARM_HOST}" 2>/dev/null; then
        echo -e "  ${GREEN}Public key installed on $SWARM_HOST${NC}"
    else
        echo -e "  ${RED}Could not copy key automatically.${NC}"
        echo -e "  ${YELLOW}Manually run on each Swarm node:${NC}"
        echo -e "  ${CYAN}  cat $SSH_KEY.pub | ssh ${SWARM_USER}@${SWARM_HOST} 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'${NC}"
    fi
fi

# Set permissions
chown -R pegaprox:pegaprox "$PLUGIN_DIR" 2>/dev/null || chown -R $(stat -c %U "$PEGAPROX_DIR") "$PLUGIN_DIR"
chmod 600 "$PLUGIN_DIR/config.json"
chmod 600 "$SSH_KEY" 2>/dev/null
echo "  Permissions set (config.json: 600, private key: 600)"

# ── Enable plugin in PegaProx ──
echo ""
echo -e "${BLUE}[4/7] Enabling plugin in PegaProx...${NC}"

# Check if sqlite3 is available
if ! command -v sqlite3 &>/dev/null; then
    apt-get update -qq && apt-get install -y -qq sqlite3 > /dev/null 2>&1
fi

DB="$PEGAPROX_DIR/config/pegaprox.db"
if [ -f "$DB" ]; then
    ENABLED=$(sqlite3 "$DB" "SELECT enabled FROM plugin_state WHERE plugin_id='docker_swarm'" 2>/dev/null)
    if [ "$ENABLED" = "1" ]; then
        echo -e "  ${YELLOW}Plugin already enabled${NC}"
    else
        sqlite3 "$DB" "INSERT OR REPLACE INTO plugin_state (plugin_id, enabled, loaded_at, error) VALUES ('docker_swarm', 1, datetime('now'), '')" 2>/dev/null
        echo -e "  ${GREEN}Plugin enabled in database${NC}"
    fi
else
    echo -e "  ${YELLOW}Database not found — enable via PegaProx UI: Settings > Plugins > Rescan > Enable${NC}"
fi

# ── Patch PegaProx (sidebar + CSP + topology) ──
echo ""
echo -e "${BLUE}[5/7] Integrating into PegaProx UI...${NC}"

# Install Node.js if not present (for production build)
if ! command -v node &>/dev/null; then
    echo "  Installing Node.js for production builds..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq nodejs npm > /dev/null 2>&1
        echo -e "  Node.js $(node -v 2>/dev/null) ${GREEN}installed${NC}"
    else
        echo -e "  ${YELLOW}Could not install Node.js — frontend will use browser compilation (slower)${NC}"
    fi
fi

# Run the patch
chmod +x "$PLUGIN_DIR/patch-pegaprox.sh" 2>/dev/null
bash "$PLUGIN_DIR/patch-pegaprox.sh"

# ── Nginx reverse proxy (optional, for VNC console) ──
echo ""
echo -e "${BLUE}[6/7] VNC Console support (nginx reverse proxy)...${NC}"

# Check if behind Cloudflare Tunnel or other reverse proxy
CURRENT_PORT=$(sqlite3 "$DB" "SELECT value FROM server_settings WHERE key='port'" 2>/dev/null || echo "443")

if [ "$CURRENT_PORT" = "5000" ]; then
    echo -e "  ${YELLOW}Already in reverse proxy mode (port 5000)${NC}"
elif command -v nginx &>/dev/null && [ -f /etc/nginx/sites-available/pegaprox ]; then
    echo -e "  ${YELLOW}Nginx already configured${NC}"
else
    echo ""
    echo "  PegaProx VNC console requires WebSocket proxy to work through"
    echo "  reverse proxies (Cloudflare Tunnel, nginx, etc.)"
    echo ""
    read -p "  Install nginx reverse proxy for VNC console? [y/N]: " INSTALL_NGINX

    if [[ "$INSTALL_NGINX" =~ ^[Yy] ]]; then
        # Install nginx
        if ! command -v nginx &>/dev/null; then
            apt-get update -qq && apt-get install -y -qq nginx > /dev/null 2>&1
            echo -e "  nginx ${GREEN}installed${NC}"
        fi

        # Set PegaProx to reverse proxy mode
        sqlite3 "$DB" "INSERT OR REPLACE INTO server_settings (key, value) VALUES ('reverse_proxy_enabled', 'true')"
        sqlite3 "$DB" "INSERT OR REPLACE INTO server_settings (key, value) VALUES ('port', '5000')"
        sqlite3 "$DB" "INSERT OR REPLACE INTO server_settings (key, value) VALUES ('bind_address', '0.0.0.0')"

        # Create nginx config
        cat > /etc/nginx/sites-available/pegaprox << 'NGINXEOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}
upstream pegaprox_app { server 127.0.0.1:5000; }
upstream pegaprox_vnc { server 127.0.0.1:5001; }
upstream pegaprox_ssh { server 127.0.0.1:5002; }

server {
    listen 443;
    server_name _;
    client_max_body_size 100g;
    proxy_connect_timeout 10;
    proxy_read_timeout 3600;
    proxy_send_timeout 3600;

    location ~ ^/api/clusters/.*/vncwebsocket {
        proxy_pass http://pegaprox_vnc;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600;
    }
    location ~ ^/api/clusters/.*/shellws {
        proxy_pass http://pegaprox_ssh;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600;
    }
    location /api/sse/ {
        proxy_pass http://pegaprox_app;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
        chunked_transfer_encoding off;
    }
    location / {
        proxy_pass http://pegaprox_app;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
NGINXEOF
        rm -f /etc/nginx/sites-enabled/default
        ln -sf /etc/nginx/sites-available/pegaprox /etc/nginx/sites-enabled/pegaprox

        if nginx -t > /dev/null 2>&1; then
            systemctl restart nginx
            systemctl enable nginx > /dev/null 2>&1
            echo -e "  ${GREEN}Nginx reverse proxy configured and started${NC}"
            echo ""
            echo -e "  ${YELLOW}NOTE: Update your reverse proxy/tunnel to point to http://YOUR_IP:443${NC}"
            echo -e "  ${YELLOW}(not https://, since nginx handles the proxy without SSL)${NC}"
        else
            echo -e "  ${RED}Nginx config test failed — check /etc/nginx/sites-available/pegaprox${NC}"
        fi
    else
        echo -e "  ${YELLOW}Skipped — VNC console may not work through reverse proxies${NC}"
    fi
fi

# Always wire the permanent CSS fixes + self-healing watcher after any nginx
# (re)configuration. Idempotent; no-op if already in place.
if [ -f /etc/nginx/sites-available/pegaprox ] && [ -f "$PLUGIN_DIR/patch_nginx_fixes.sh" ]; then
    echo ""
    echo -e "  ${BOLD}Wiring permanent nginx fixes...${NC}"
    bash "$PLUGIN_DIR/patch_nginx_fixes.sh" 2>&1 | sed 's/^/    /'
    bash "$PLUGIN_DIR/setup_nginx_watcher.sh" 2>&1 | sed 's/^/    /'
fi

# ── Auto-patch for persistence ──
echo ""
echo -e "${BLUE}[7/7] Setting up auto-patch for PegaProx updates...${NC}"

cat > /etc/systemd/system/pegaprox-swarm-patch.path << EOF
[Unit]
Description=Watch PegaProx for updates (Docker Swarm plugin)
Documentation=$REPO_URL

[Path]
PathModified=$PEGAPROX_DIR/web/src/dashboard.js
PathModified=$PEGAPROX_DIR/pegaprox/app.py

[Install]
WantedBy=multi-user.target
EOF

# Create auto-patch wrapper script (avoids systemd quoting issues with inline bash)
cat > "$PLUGIN_DIR/auto-patch.sh" << 'APEOF'
#!/bin/bash
# Auto-patcher triggered by pegaprox-swarm-patch.path when PegaProx rewrites
# dashboard.js or app.py. Nginx-layer fixes live independently so they are
# not part of this trigger — they persist across PegaProx updates by design.
LOCK=/tmp/.pegaprox-patching
if [ -f "$LOCK" ]; then echo "Skipping - patch in progress"; exit 0; fi
sleep 3
if grep -q sidebarDockerSwarm /opt/PegaProx/web/src/dashboard.js 2>/dev/null && \
   grep -q "frame-ancestors" /opt/PegaProx/pegaprox/app.py 2>/dev/null; then
    echo "Patch not needed"
    exit 0
fi
touch "$LOCK"
/bin/bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh
rm -f "$LOCK"
APEOF
chmod +x "$PLUGIN_DIR/auto-patch.sh"

cat > /etc/systemd/system/pegaprox-swarm-patch.service << 'EOF'
[Unit]
Description=Re-apply Docker Swarm plugin patches after PegaProx update
After=pegaprox.service

[Service]
Type=oneshot
ExecStart=/bin/bash /opt/PegaProx/plugins/docker_swarm/auto-patch.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pegaprox-swarm-patch
EOF

systemctl daemon-reload
systemctl enable pegaprox-swarm-patch.path > /dev/null 2>&1
systemctl start pegaprox-swarm-patch.path
echo -e "  ${GREEN}Auto-patch watcher enabled${NC}"
echo "  PegaProx updates will be automatically re-patched"

# ── Final restart ──
echo ""
systemctl restart pegaprox
sleep 2

if systemctl is-active --quiet pegaprox; then
    echo -e "${GREEN}PegaProx restarted successfully${NC}"
else
    echo -e "${RED}PegaProx failed to start — check: journalctl -u pegaprox${NC}"
fi

# ── Done ──
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   ${GREEN}${BOLD}Installation Complete!${NC}${CYAN}                                      ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Plugin URL:${NC}  https://YOUR_HOST/api/plugins/docker_swarm/api/ui"
echo -e "  ${BOLD}Sidebar:${NC}     Docker Swarm > Swarm Cluster"
echo -e "  ${BOLD}Config:${NC}      $PLUGIN_DIR/config.json"
echo -e "  ${BOLD}Logs:${NC}        journalctl -u pegaprox | grep docker_swarm"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo "  1. Log into PegaProx and click 'Swarm Cluster' in the sidebar"
echo "  2. If you see 'No connection', go to Settings tab and configure SSH hosts"
echo "  3. For VNC console, ensure your reverse proxy supports WebSockets"
echo ""
echo -e "  ${BOLD}Uninstall:${NC}   sudo bash $PLUGIN_DIR/uninstall.sh"
echo -e "  ${BOLD}GitHub:${NC}      $REPO_URL"
echo ""

#!/bin/bash
# Install / refresh the persistence layer so the docker_swarm plugin survives
# every way PegaProx (or its host) can try to revert us:
#
#   Layer 1 — pegaprox-patch.path (already installed by setup_path_watcher.sh)
#     Filesystem watcher, fires on PathChanged for dashboard.js / app.py /
#     vms.py / node_modals.js. Sub-3 s response when a PegaProx update
#     overwrites one of those files in place.
#
#   Layer 2 — pegaprox-patch-ensure.timer (THIS SCRIPT)
#     Periodic systemd timer (every 5 min, AND once ~20 s after boot).
#     Checks that every marker we care about is still present. If any marker
#     is missing, runs the full patch-pegaprox.sh orchestrator. Catches the
#     cases the Path watcher cannot see:
#       - PegaProx atomic replace (mv new old → new inode; some events lost)
#       - files recreated with identical mtime (rare but possible)
#       - host reboot during a PegaProx update (missed PathChanged)
#       - manual edits that rewrote the file without changing content checksum
#
#   Layer 3 — pegaprox-patch-boot.service (THIS SCRIPT)
#     Oneshot that runs once every boot, after pegaprox.service is ready.
#     Same check-and-heal as layer 2 but guaranteed to run at least once
#     post-boot even if the timer is somehow disabled.
#
#   Layer 4 — pegaprox-nginx-fix.path (pre-existing, v1.8.3)
#     Independent watcher for /etc/nginx/sites-available/pegaprox.
#
# All units are idempotent — rerunning this script refreshes them safely.

set -e

PLUGIN_DIR="/opt/PegaProx/plugins/docker_swarm"
PATCH_SH="$PLUGIN_DIR/patch-pegaprox.sh"
AUTO_PATCH_SH="$PLUGIN_DIR/auto-patch.sh"

TIMER=/etc/systemd/system/pegaprox-patch-ensure.timer
ENSURE_SVC=/etc/systemd/system/pegaprox-patch-ensure.service
BOOT_SVC=/etc/systemd/system/pegaprox-patch-boot.service
ENSURE_SH="$PLUGIN_DIR/ensure-patches.sh"


# ---------- The actual check script invoked by timer + boot unit ----------
cat > "$ENSURE_SH" << 'EOF'
#!/bin/bash
# Fired by pegaprox-patch-ensure.timer every 5 min, and by
# pegaprox-patch-boot.service once after boot. Runs the full orchestrator
# iff any expected marker is missing.
LOCK=/tmp/.pegaprox-patching
if [ -f "$LOCK" ]; then exit 0; fi

need_patch=0
grep -q sidebarDockerSwarm            /opt/PegaProx/web/src/dashboard.js 2>/dev/null  || need_patch=1
grep -q "frame-ancestors"             /opt/PegaProx/pegaprox/app.py      2>/dev/null  || need_patch=1
grep -q DS-VNC-SUBPROTOCOL            /opt/PegaProx/pegaprox/api/vms.py  2>/dev/null  || need_patch=1
grep -q DS-VNC-AUTH-CONTEXT           /opt/PegaProx/pegaprox/api/vms.py  2>/dev/null  || need_patch=1
grep -q DS-VNC-TICKET-PASSTHROUGH     /opt/PegaProx/web/src/node_modals.js 2>/dev/null || need_patch=1

if [ "$need_patch" -eq 0 ]; then
    exit 0
fi

echo "$(date -Iseconds) [ensure] marker missing — running orchestrator" >&2
touch "$LOCK"
/bin/bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh
rm -f "$LOCK"
EOF
chmod +x "$ENSURE_SH"


# ---------- ensure service (invoked by timer AND manually by boot unit) ----------
cat > "$ENSURE_SVC" << EOF
[Unit]
Description=PegaProx docker_swarm plugin — ensure all markers present (heal if drifted)
After=pegaprox.service
Requires=pegaprox.service

[Service]
Type=oneshot
ExecStart=$ENSURE_SH
TimeoutStartSec=600
EOF


# ---------- 5-minute timer ----------
cat > "$TIMER" << 'EOF'
[Unit]
Description=PegaProx docker_swarm plugin — re-apply patches if any marker drifted
Documentation=https://github.com/alfonsokuen/pegaprox-docker-swarm

[Timer]
# First tick ~20 s after boot (catches any drift introduced during boot itself).
OnBootSec=20s
# Then every 5 minutes.
OnUnitActiveSec=5min
AccuracySec=15s
Unit=pegaprox-patch-ensure.service

[Install]
WantedBy=timers.target
EOF


# ---------- post-boot oneshot (defence-in-depth for when the timer is disabled) ----------
cat > "$BOOT_SVC" << EOF
[Unit]
Description=PegaProx docker_swarm plugin — one-shot drift check at boot
After=pegaprox.service
Requires=pegaprox.service

[Service]
Type=oneshot
ExecStart=$ENSURE_SH
TimeoutStartSec=600
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF


systemctl daemon-reload
systemctl enable --now pegaprox-patch-ensure.timer  > /dev/null 2>&1 || true
systemctl enable       pegaprox-patch-boot.service   > /dev/null 2>&1 || true

# Show what we just installed
echo "persistence layer active:"
echo "  timer:       $(systemctl is-active pegaprox-patch-ensure.timer) ($(systemctl is-enabled pegaprox-patch-ensure.timer))"
echo "  ensure-svc:  $(systemctl is-enabled pegaprox-patch-ensure.service 2>/dev/null || echo static)"
echo "  boot-svc:    $(systemctl is-active pegaprox-patch-boot.service) ($(systemctl is-enabled pegaprox-patch-boot.service))"
echo "next timer:    $(systemctl show pegaprox-patch-ensure.timer --property=NextElapseUSecRealtime --value)"

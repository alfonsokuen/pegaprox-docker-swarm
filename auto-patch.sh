#!/bin/bash
# Triggered by pegaprox-patch.path systemd watcher after PegaProx updates.
# Bails out early if all expected markers are present; otherwise runs the full
# patch-pegaprox.sh orchestrator.
LOCK=/tmp/.pegaprox-patching
if [ -f "$LOCK" ]; then echo "Skipping - patch in progress"; exit 0; fi
sleep 3
if grep -q sidebarDockerSwarm /opt/PegaProx/web/src/dashboard.js 2>/dev/null && \
   grep -q "frame-ancestors" /opt/PegaProx/pegaprox/app.py 2>/dev/null && \
   grep -q "DS-VNC-SUBPROTOCOL" /opt/PegaProx/pegaprox/api/vms.py 2>/dev/null; then
    echo "Patch not needed"
    exit 0
fi
touch "$LOCK"
/bin/bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh
rm -f "$LOCK"

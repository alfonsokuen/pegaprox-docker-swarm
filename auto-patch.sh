#!/bin/bash
# Triggered by pegaprox-swarm-patch.path systemd watcher after PegaProx updates.
#
# v1.14.3 hardening:
#   * Wait + retry: PegaProx update can write multiple files over several
#     seconds; we wait for filesystem to settle before grepping markers.
#   * Validation: after patch-pegaprox.sh runs, verify the integration
#     actually landed (sidebarDockerSwarm in web/index.html). If missing,
#     log loud and exit non-zero so journalctl shows the broken state.
#   * Lock with PID + timestamp so stale locks don't pin everything.
#   * All output goes to journal via parent service unit (StandardOutput=journal).

set -u

LOCK=/tmp/.pegaprox-patching
LOG_TAG="auto-patch"
RC=0

log() { echo "[$LOG_TAG $(date +%H:%M:%S)] $*"; }
fail() { echo "[$LOG_TAG $(date +%H:%M:%S)] FAIL: $*" >&2; RC=2; }

# Stale lock guard (older than 10 min = previous run died)
if [ -f "$LOCK" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -lt 600 ]; then
        log "patch already in progress (lock age ${LOCK_AGE}s) — skipping"
        exit 0
    fi
    log "stale lock (age ${LOCK_AGE}s) — clearing"
    rm -f "$LOCK"
fi

# Settle for filesystem updates (PegaProx writes multiple files in sequence)
sleep 5

# Required markers — if all present, no work to do.
markers_ok() {
    grep -q sidebarDockerSwarm /opt/PegaProx/web/src/dashboard.js 2>/dev/null && \
    grep -q sidebarDockerSwarm /opt/PegaProx/web/index.html 2>/dev/null && \
    grep -q "frame-ancestors" /opt/PegaProx/pegaprox/app.py 2>/dev/null
}

if markers_ok; then
    log "all markers present — no patch needed"
    exit 0
fi

log "missing markers detected — running patch-pegaprox.sh"
echo "$$ $(date -Iseconds)" > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

if ! /bin/bash /opt/PegaProx/plugins/docker_swarm/patch-pegaprox.sh 2>&1; then
    fail "patch-pegaprox.sh exited non-zero"
fi

# Verify the patch actually landed in BOTH the source dashboard.js
# and the rebuilt web/index.html. If web/index.html is missing the marker,
# the post-build either failed or the rebuild didn't run.
if ! grep -q sidebarDockerSwarm /opt/PegaProx/web/src/dashboard.js 2>/dev/null; then
    fail "sidebarDockerSwarm missing from dashboard.js after patch"
fi
if ! grep -q sidebarDockerSwarm /opt/PegaProx/web/index.html 2>/dev/null; then
    fail "sidebarDockerSwarm missing from web/index.html after patch (rebuild failed?)"
fi

if [ "$RC" -eq 0 ]; then
    log "patch completed successfully"
else
    fail "patch did NOT fully complete — UI integration broken"
fi

exit "$RC"

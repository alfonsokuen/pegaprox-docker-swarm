#!/usr/bin/env python3
"""
Standalone idempotent patch that fixes the collapsed VNC console modal
introduced by PegaProx Beta 0.9.6.1. The outer modal card renders with
height=0 because no explicit sizing is applied; the inner `flex-1` wrapper
inherits 0 height and the noVNC canvas collapses to 0x0 even though the
WebSocket/VNC pipeline is healthy.

This patch injects a runtime useEffect hook that appends a single <style>
tag overriding the modal sizing whenever a canvas is present inside it.
It uses :has() which is supported by all evergreen browsers shipped in
the fleet (Chrome/Edge/Firefox/Safari modern).

Safe to re-run: keyed on the 'ds-console-modal-fix' marker.
"""

DASHBOARD = "/opt/PegaProx/web/src/dashboard.js"
MARKER = "ds-console-modal-fix"

with open(DASHBOARD) as f:
    content = f.read()

if MARKER in content:
    print("Console modal fix already applied - skipping")
    raise SystemExit(0)

# Anchor: a line added by patch_dashboard.py step 1b. We piggyback after
# it so our useEffect runs inside the main Dashboard component scope.
ANCHOR = "const [swarmTopoData, setSwarmTopoData] = useState(null);"

css_rule = (
    ".modal-backdrop .bg-proxmox-card.rounded-2xl.shadow-2xl.animate-scale-in:has(canvas)"
    "{height:85vh!important;width:min(85vw,1400px)!important;flex-direction:column!important}"
)

hook = f"""
            useEffect(() => {{
                if (document.getElementById('{MARKER}')) return;
                const s = document.createElement('style');
                s.id = '{MARKER}';
                s.textContent = "{css_rule}";
                document.head.appendChild(s);
            }}, []);"""

if ANCHOR not in content:
    print(f"ERROR: anchor not found in {DASHBOARD}. Run patch_dashboard.py first.")
    raise SystemExit(1)

content = content.replace(ANCHOR, ANCHOR + hook, 1)

with open(DASHBOARD, "w") as f:
    f.write(content)

print(f"Applied: console modal CSS fix injected ({MARKER})")

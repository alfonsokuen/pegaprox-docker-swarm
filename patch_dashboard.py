#!/usr/bin/env python3
"""
Apply all Docker Swarm patches to /opt/PegaProx/web/src/dashboard.js.

Safe to run multiple times (idempotent): if `sidebarDockerSwarm` is already
present the script exits 0.

Behaviour v1.9 (2026-04-24):
  * Each anchor-replace is verified. If the anchor is missing the script
    fails with exit 2 naming the missing anchor. Previously `changes += 1`
    ran unconditionally and hid upstream refactors (PegaProx 0.9.6.1 split
    `dashboard.js` across 17 files; anchors still live in dashboard.js but
    a future reshuffle would silently break the integration).
  * Exit codes:
        0 = already patched OR all 9 patches applied cleanly
        1 = I/O / argument error
        2 = anchor missing (PegaProx upstream changed structure)
"""

import os
import sys

DASHBOARD = "/opt/PegaProx/web/src/dashboard.js"


def _die(msg, code=2):
    sys.stderr.write("[patch_dashboard] FATAL: " + msg + "\n")
    sys.exit(code)


def _require_replace(content, old, new, label):
    if old not in content:
        _die("anchor missing for " + repr(label) + " - upstream likely refactored")
    updated = content.replace(old, new, 1)
    if updated == content:
        _die("replace for " + repr(label) + " produced no change")
    return updated


def _require_rfind_replace(content, old, new, label):
    idx = content.rfind(old)
    if idx < 0:
        _die("anchor missing for " + repr(label) + " (rfind) - upstream likely refactored")
    return content[:idx] + new + content[idx + len(old):]


def main():
    if not os.path.isfile(DASHBOARD):
        _die(DASHBOARD + " not found", code=1)

    with open(DASHBOARD, "r", encoding="utf-8") as f:
        content = f.read()

    if "sidebarDockerSwarm" in content:
        print("[patch_dashboard] already patched - skipping")
        return 0

    applied = []

    # 1. State: sidebarDockerSwarm
    anchor = "const [sidebarXHM, setSidebarXHM] = useState(false);"
    content = _require_replace(
        content,
        anchor,
        anchor + "\n            const [sidebarDockerSwarm, setSidebarDockerSwarm] = useState(false);",
        "1 state:sidebarDockerSwarm",
    )
    applied.append("1 state:sidebarDockerSwarm")

    # 1b. State: swarmTopoData
    anchor = "const [sidebarTopology, setSidebarTopology] = useState(false);"
    content = _require_replace(
        content,
        anchor,
        anchor + "\n            const [swarmTopoData, setSwarmTopoData] = useState(null);",
        "1b state:swarmTopoData",
    )
    applied.append("1b state:swarmTopoData")

    # 2. Auto-clear on cluster/pbs/vmware/group select
    content = _require_replace(
        content,
        "setSidebarTopology(false); setSidebarXHM(false);",
        "setSidebarTopology(false); setSidebarXHM(false); setSidebarDockerSwarm(false);",
        "2 auto-clear",
    )
    applied.append("2 auto-clear")

    # 3a. Dashboard-active negative guard
    content = _require_replace(
        content,
        "!selectedGroup && !sidebarXHM",
        "!selectedGroup && !sidebarXHM && !sidebarDockerSwarm",
        "3a condition:dashboard-active",
    )
    applied.append("3a condition:dashboard-active")

    # 3b. Sidebar-active disjunction
    content = _require_replace(
        content,
        "|| sidebarXHM)",
        "|| sidebarXHM || sidebarDockerSwarm)",
        "3b condition:sidebar-active",
    )
    applied.append("3b condition:sidebar-active")

    # 4. Topology fetch useEffect
    topo_anchor = (
        "}, [sidebarTopology]);\n"
        "\n"
        "            // LW: Feb 2026 - corporate sidebar inventory tree state"
    )
    topo_new = (
        "}, [sidebarTopology]);\n"
        "\n"
        "            useEffect(() => {\n"
        "                if (!sidebarTopology) return;\n"
        "                fetch('/api/plugins/docker_swarm/api/topology', {\n"
        "                    credentials: 'include',\n"
        "                    headers: getAuthHeaders ? getAuthHeaders() : {}\n"
        "                })\n"
        "                    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })\n"
        "                    .then(d => { if (d && d.nodes && d.nodes.length > 0) setSwarmTopoData(d); })\n"
        "                    .catch(e => console.warn('[DockerSwarm] Topology fetch:', e));\n"
        "            }, [sidebarTopology]);\n"
        "\n"
        "            // LW: Feb 2026 - corporate sidebar inventory tree state"
    )
    content = _require_replace(content, topo_anchor, topo_new, "4 topology-useEffect")
    applied.append("4 topology-useEffect")

    # 5. Sidebar section injected BEFORE the XHM sidebar marker
    xhm_marker = "{/* LW: Mar 2026 - XHM sidebar (only when both PVE + XCP-ng clusters exist) */}"
    swarm_sidebar = (
        "{/* Docker Swarm Manager Plugin */}\n"
        "                                <div className=\"mt-4 pt-4 border-t border-proxmox-border\">\n"
        "                                    <div className=\"flex items-center justify-between px-1 mb-2\">\n"
        "                                        <h2 className=\"text-sm font-semibold text-gray-400 uppercase tracking-wider\">Docker Swarm</h2>\n"
        "                                    </div>\n"
        "                                    <div className=\"space-y-1.5\">\n"
        "                                        <button\n"
        "                                            onClick={() => { setSidebarDockerSwarm(true); setSidebarTopology(false); setSidebarXHM(false); setSelectedCluster(null); setSelectedPBS(null); setSelectedVMware(null); setSelectedGroup(null); }}\n"
        "                                            className={isCorporate\n"
        "                                                ? \"w-full flex items-center gap-1.5 pl-3 pr-2 py-0.5 text-[13px] leading-5\"\n"
        "                                                : `w-full flex items-center gap-3 px-3 py-2 rounded-xl transition-all ${\n"
        "                                                    sidebarDockerSwarm\n"
        "                                                        ? \"bg-gradient-to-r from-cyan-500/20 to-blue-600/10 border border-cyan-500/30 text-white\"\n"
        "                                                        : \"bg-proxmox-card border border-proxmox-border hover:border-cyan-500/30 text-gray-300 hover:text-white\"\n"
        "                                                  }`\n"
        "                                            }\n"
        "                                            style={isCorporate ? (sidebarDockerSwarm ? {background: \"rgba(73,175,217,0.10)\", borderLeft: \"2px solid var(--corp-accent)\", color: \"var(--color-text)\"} : {color: \"var(--corp-text-secondary)\"}) : undefined}\n"
        "                                            onMouseEnter={isCorporate ? (e) => { if (!sidebarDockerSwarm) { e.currentTarget.style.background = \"var(--color-hover)\"; e.currentTarget.style.color = \"var(--color-text)\"; }} : undefined}\n"
        "                                            onMouseLeave={isCorporate ? (e) => { if (!sidebarDockerSwarm) { e.currentTarget.style.background = \"\"; e.currentTarget.style.color = \"var(--corp-text-secondary)\"; }} : undefined}\n"
        "                                        >\n"
        "                                            {isCorporate ? (\n"
        "                                                <Icons.Box className=\"w-4 h-4 flex-shrink-0\" style={{color: sidebarDockerSwarm ? \"var(--corp-accent)\" : \"#2dd4bf\"}} />\n"
        "                                            ) : (\n"
        "                                                <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${sidebarDockerSwarm ? \"bg-cyan-500/20\" : \"bg-proxmox-dark\"}`}>\n"
        "                                                    <Icons.Box className=\"w-4 h-4 text-cyan-400\" />\n"
        "                                                </div>\n"
        "                                            )}\n"
        "                                            <div className=\"flex-1 text-left min-w-0\">\n"
        "                                                <div className={`${isCorporate ? \"text-[13px]\" : \"text-sm\"} font-medium truncate`}>Swarm Cluster</div>\n"
        "                                                {!isCorporate && <div className=\"text-xs text-gray-500 truncate\">Docker Swarm</div>}\n"
        "                                            </div>\n"
        "                                            <div className=\"w-1.5 h-1.5 rounded-full shrink-0\" style={{background: \"var(--color-success)\"}} />\n"
        "                                        </button>\n"
        "                                    </div>\n"
        "                                </div>\n"
        "\n"
        "                                            " + xhm_marker
    )
    content = _require_replace(
        content,
        "            " + xhm_marker,
        "            " + swarm_sidebar,
        "5 sidebar-section",
    )
    applied.append("5 sidebar-section")

    # 6. Content panel (iframe) - LAST occurrence of `) : sidebarXHM ? (`
    xhm_content = ") : sidebarXHM ? ("
    swarm_content = (
        ") : sidebarDockerSwarm ? (\n"
        "                                    <div style={{height: \"calc(100vh - 48px)\", display: \"flex\", flexDirection: \"column\"}}>\n"
        "                                        {isCorporate && (\n"
        "                                            <div className=\"corp-content-header\">\n"
        "                                                <div className=\"flex items-center gap-2\">\n"
        "                                                    <Icons.Box className=\"w-4 h-4\" style={{color: \"#2dd4bf\"}} />\n"
        "                                                    <span className=\"corp-header-title\">Docker Swarm Manager</span>\n"
        "                                                </div>\n"
        "                                            </div>\n"
        "                                        )}\n"
        "                                        <iframe\n"
        "                                            src=\"/api/plugins/docker_swarm/api/ui\"\n"
        "                                            style={{flex: 1, border: \"none\", width: \"100%\", height: \"100%\", background: \"#0f1117\", borderRadius: isCorporate ? \"0\" : \"12px\"}}\n"
        "                                            title=\"Docker Swarm Manager\"\n"
        "                                        />\n"
        "                                    </div>\n"
        "                                " + xhm_content
    )
    content = _require_rfind_replace(content, xhm_content, swarm_content, "6 content-panel")
    applied.append("6 content-panel")

    # 7. Topology multiCluster concat (last occurrence)
    old_topo = "})}\n                                                isCorporate={true}"
    new_topo = "}).concat(swarmTopoData ? [swarmTopoData] : [])}\n                                                isCorporate={true}"
    content = _require_rfind_replace(content, old_topo, new_topo, "7 topology-concat")
    applied.append("7 topology-concat")

    # 8. Auto-refresh TopologyView on multiCluster growth
    topo_autorefresh_marker = "// NS: view mode toggle - default to diagram, cards as fallback"
    topo_autorefresh = (
        "// Auto-refresh topology when multiCluster grows (Swarm data arrives after render)\n"
        "            const prevMcLength = useRef(0);\n"
        "            useEffect(() => {\n"
        "                const curLen = multiCluster?.length || 0;\n"
        "                if (curLen > prevMcLength.current && prevMcLength.current > 0) {\n"
        "                    snapshotMultiCluster.current = multiCluster;\n"
        "                    setTopoRevision(r => r + 1);\n"
        "                }\n"
        "                prevMcLength.current = curLen;\n"
        "            }, [multiCluster?.length]);\n"
        "\n"
        "            " + topo_autorefresh_marker
    )
    if "prevMcLength" not in content:
        content = _require_replace(content, topo_autorefresh_marker, topo_autorefresh, "8 topology-auto-refresh")
        applied.append("8 topology-auto-refresh")

    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(content)

    print("[patch_dashboard] applied " + str(len(applied)) + " patches:")
    for label in applied:
        print("  - " + label)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Apply all Docker Swarm patches to a CLEAN dashboard.js.
Run after restoring dashboard.js from backup or PegaProx update.
"""

with open("/opt/PegaProx/web/src/dashboard.js") as f:
    content = f.read()

if "sidebarDockerSwarm" in content:
    print("Already patched - skipping")
    exit(0)

changes = 0

# 1. State variable
old = "const [sidebarXHM, setSidebarXHM] = useState(false);"
new = old + "\n            const [sidebarDockerSwarm, setSidebarDockerSwarm] = useState(false);"
content = content.replace(old, new, 1)
changes += 1

# 1b. Topology state
old = "const [sidebarTopology, setSidebarTopology] = useState(false);"
new = old + "\n            const [swarmTopoData, setSwarmTopoData] = useState(null);"
content = content.replace(old, new, 1)
changes += 1

# 2. Auto-clear
content = content.replace(
    "setSidebarTopology(false); setSidebarXHM(false);",
    "setSidebarTopology(false); setSidebarXHM(false); setSidebarDockerSwarm(false);"
)
changes += 1

# 3. Conditions
content = content.replace(
    "!selectedGroup && !sidebarXHM",
    "!selectedGroup && !sidebarXHM && !sidebarDockerSwarm"
)
content = content.replace(
    "|| sidebarXHM)",
    "|| sidebarXHM || sidebarDockerSwarm)"
)
changes += 1

# 4. Topology fetch effect
topo_effect = """}, [sidebarTopology]);

            // LW: Feb 2026 - corporate sidebar inventory tree state"""
topo_effect_new = """}, [sidebarTopology]);

            useEffect(() => {
                if (!sidebarTopology) return;
                fetch('/api/plugins/docker_swarm/api/topology', {credentials: 'include'})
                    .then(r => r.json()).then(d => { if (d && d.nodes) setSwarmTopoData(d); })
                    .catch(() => {});
            }, [sidebarTopology]);

            // LW: Feb 2026 - corporate sidebar inventory tree state"""
content = content.replace(topo_effect, topo_effect_new, 1)
changes += 1

# 5. Sidebar section
xhm_marker = "{/* LW: Mar 2026 - XHM sidebar (only when both PVE + XCP-ng clusters exist) */}"
swarm_sidebar = """{/* Docker Swarm Manager Plugin */}
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

                                            """ + xhm_marker
content = content.replace("            " + xhm_marker, "            " + swarm_sidebar, 1)
changes += 1

# 6. Content panel
xhm_content = ") : sidebarXHM ? ("
swarm_content = """) : sidebarDockerSwarm ? (
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
                                """ + xhm_content
idx = content.rfind(xhm_content)
if idx >= 0:
    content = content[:idx] + swarm_content + content[idx + len(xhm_content):]
    changes += 1

# 7. Topology multiCluster concat
old_topo = """})}
                                                isCorporate={true}"""
new_topo = """}).concat(swarmTopoData ? [swarmTopoData] : [])}
                                                isCorporate={true}"""
idx = content.rfind(old_topo)
if idx >= 0:
    content = content[:idx] + new_topo + content[idx + len(old_topo):]
    changes += 1
    print("Topology concat injected")

with open("/opt/PegaProx/web/src/dashboard.js", "w") as f:
    f.write(content)

print(f"Applied {changes} patches")

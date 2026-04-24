# Changelog

All notable changes to the PegaProx Docker Swarm plugin are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This
project follows Semantic Versioning.

## [1.9.2] — 2026-04-24

### Fixed — VM console actually connects (Authentication failure / invalid PVEVNC ticket)

After v1.9.1 unblocked the WebSocket handshake, the console still failed:
the modal showed "Connecting…" forever, nginx logged `101 72`, and our
diagnostic probe recorded exactly `sent=29B recv=60B` on every session —
the wire-shape of an RFB "Authentication failure" (12 server-hello + 3 sec-
types + 16 challenge + 30 bytes of auth-fail + reason string).

Root cause, confirmed end-to-end:

1. Browser calls `GET /vnc` → PegaProx POSTs `/vncproxy` on PVE → ticket_A.
   PVE's side-effect is `set_password vnc <ticket_A>` on the QEMU monitor,
   so QEMU's VNC password is now `ticket_A[:8]` (the DES key used at RFB
   level).
2. Browser opens the WebSocket with `new RFB(url, {password: ticket_A})`.
3. PegaProx's VNC handler (port 5001) ran a **fresh login** and POSTed
   `/vncproxy` **a second time** → ticket_B. Side-effect: QEMU's VNC
   password is rewritten to `ticket_B[:8]`, silently invalidating
   `ticket_A`.
4. Browser sends `DES(ticket_A[:8])(challenge)`; QEMU expects
   `DES(ticket_B[:8])(challenge)` → mismatch → `Authentication failure`.

Additional complication: PegaProx connects to Proxmox via an auto-created
**API token** (`root@pam!pegaprox_…`), not a user/password session. The
handler's fresh login produced a PVEAuthCookie that did **not** match the
token-emitted ticket, so when we first tried "just use the browser's
ticket" in v1.9.2-attempt-1, PVE returned
`Handshake status 401 permission denied - invalid PVEVNC ticket` for the
upstream WebSocket.

### Added

- **`patch_vnc_auth_context.py`** (new, idempotent, markers
  `DS-VNC-AUTH-CONTEXT` in `pegaprox/api/vms.py` and
  `DS-VNC-TICKET-PASSTHROUGH` in `web/src/node_modals.js`) — the full fix:
  - **UI** appends `&vncticket=…&vncport=…` to the WebSocket URL so the
    handler can use the same ticket the browser already has.
  - **Server** stops the second POST `/vncproxy` when those params are
    present, and the upstream WebSocket handshake reuses whichever
    credential the manager is already using:
      * `manager._using_api_token` → `Authorization: PVEAPIToken=…`
      * elif `manager._ticket`     → `Cookie: PVEAuthCookie=…`
      * else → fresh login fallback (cold-start edge case only).

### Changed

- **`patch-pegaprox.sh`** gains step `[3c/4]` that runs
  `patch_vnc_auth_context.py` with a syntax-check rollback guard, between
  the nginx console-modal fix and the frontend rebuild.
- **`auto-patch.sh`** skip condition now also requires
  `DS-VNC-AUTH-CONTEXT` (in vms.py) and `DS-VNC-TICKET-PASSTHROUGH` (in
  node_modals.js) to be present; otherwise it re-runs the orchestrator
  after any PegaProx auto-update that stomps either file.
- **`setup_path_watcher.sh`** refreshes `pegaprox-patch.path` to also
  monitor `web/src/node_modals.js` (fourth watched file; previously
  dashboard.js + app.py + vms.py).

### Verified on CT 119 with diagvnc + real user browser

  Playwright probe (diagvnc, VM 120 on pve2) reached the RFB challenge
  (16 B) for the first time in this investigation:

      t= 273 ms   open, ws.protocol = "binary"
      t=1651 ms   msg 12 B   "RFB 003.008\n"      ← server hello
      t=1773 ms   msg  2 B   0x01 0x02            ← sec-types: VNC auth
      t=1893 ms   msg 16 B   (random)             ← VNC challenge

  The user then confirmed the real browser ("ya funciono") — noVNC
  performs DES(ticket_A[:8])(challenge), QEMU still holds ticket_A[:8]
  because the handler no longer overwrites it, auth succeeds, session
  proceeds to framebuffer.

## [1.9.1] — 2026-04-24

### Fixed — v1.9.0 regression: `subprotocols=['binary']` rejected clients that don't advertise a subprotocol

v1.9.0 added `subprotocols=['binary']` to `websockets.serve(...)` to fix the
noVNC "close 1006" regression in PegaProx 0.9.7. That unblocked browsers
with modern noVNC bundles but introduced a new failure mode:

- `websockets` library's default behaviour when `subprotocols=` is set and
  the client offers nothing in common is `NegotiationError` → **HTTP 400
  Bad Request** with empty body.
- Every permissive client stops working: `curl`, `wscat`, internal health
  probes, and — critically — **any browser running a cached old noVNC that
  doesn't advertise `binary`**.

Observed on CT 119 (2026-04-24 15:01 UTC): the user saw "Connecting…"
forever because their browser had cached an older noVNC; nginx access log
showed `GET …/vncwebsocket?token=… 400 60` for every retry.

### Changed

- **`patch_vnc_subprotocol.py`** now installs a permissive
  `select_subprotocol=` callback instead of advertising `subprotocols=`.
  The callback returns `'binary'` if the client offered it (noVNC modern
  case → handshake echoes back, browser fires `open` with
  `ws.protocol = 'binary'`) and `None` otherwise (handshake completes with
  no subprotocol negotiated — RFC-valid, works for every permissive client).
  The `DS-VNC-SUBPROTOCOL` marker is preserved so the watcher skip-check
  stays stable, and the patcher now handles three input states idempotently:
  stock PegaProx 0.9.7, v1.9.0-patched, and v1.9.1-patched.

### Verified on CT 119

  | Client                           | Before v1.9.1   | After v1.9.1                    |
  |----------------------------------|-----------------|---------------------------------|
  | Browser with `['binary']`        | 101 + RFB       | 101 + `Protocol: binary` + RFB  |
  | Browser with no subprotocol      | **400 empty**   | 101 + RFB                       |
  | curl / wscat / nc (no subproto)  | **400 empty**   | 101 + RFB                       |

## [1.9.0] — 2026-04-24

### Fixed — PegaProx 0.9.7 compatibility (VNC/xterm console: "Error de conexión")

PegaProx 0.9.7 ships `start_vnc_websocket_server` calling
`websockets.serve(vnc_handler, …, ping_interval=20, ping_timeout=10)`
— but without `subprotocols=`. Meanwhile `noVNC` opens the socket with
`new WebSocket(url, ['binary'])`. Per RFC 6455, when the client advertises
subprotocols the server MUST echo one back in the 101 response or the browser
tears the connection down with **close code 1006 before the `open` event
fires**. Chrome/Firefox/Edge all do this; permissive clients (curl / nc /
wscat / python-websocket) do not — which is why upstream integration tests
passed while every real VM console in every tenant showed "Error de conexión"
or "Reconnecting (2/3)…".

Diagnosed empirically on CT 119 (pegaprox, 2026-04-24) with a flush-print at
the VNC handler entry and a browser-side `WebSocket(url, ['binary'])` probe:
```
Browser  new WebSocket(url, ['binary'])  →  close 1006 at 44 ms, no open event
Browser  new WebSocket(url)              →  open at 40 ms, RFB 003.008\n at 1.5s
```
Once `subprotocols=['binary']` is added to both `websockets.serve` call sites
(primary + IPv4-fallback), browsers negotiate `ws.protocol = "binary"` and the
full noVNC session proceeds normally.

### Added

- **`patch_vnc_subprotocol.py`** (new, idempotent, marker
  `DS-VNC-SUBPROTOCOL`) — surgical patch for
  `/opt/PegaProx/pegaprox/api/vms.py` that adds `subprotocols=['binary']` to
  both `websockets.serve` call sites inside `start_vnc_websocket_server`.
  Fails with exit 2 if the anchor strings are missing (PegaProx core
  refactor detection). Backed up before any change, Python syntax checked
  after.
- **`setup_path_watcher.sh`** (new) — refreshes the systemd path watcher
  `pegaprox-patch.path` so it also monitors
  `/opt/PegaProx/pegaprox/api/vms.py`. The previous install.sh only watched
  `dashboard.js` and `app.py`; after this release, any PegaProx auto-update
  that touches vms.py also re-triggers the orchestrator.

### Changed

- **`patch-pegaprox.sh`** now runs `patch_vnc_subprotocol.py` as step [3b/4],
  between the console-modal nginx fix and the frontend rebuild. Backs up
  `vms.py` beforehand and aborts with rollback if post-patch syntax check
  fails — this file is ~6000 lines of PegaProx core and must never be left
  in a broken state.
- **`auto-patch.sh`** skip condition now also checks for the
  `DS-VNC-SUBPROTOCOL` marker, so the watcher re-runs the orchestrator after
  any PegaProx update that replaces vms.py.

### Fixed — PegaProx 0.9.6.1 compatibility (sidebar silently disappeared)

PegaProx `Beta 0.9.6.1` (released 2026-04-23) split the 400KB `dashboard.js`
into 17 feature files concatenated by a new `web/Dev/build.sh` into
`web/index.html`. Our `patch_dashboard.py` still patches the correct source
file — `dashboard.js` is still the biggest file in the concat list — but the
plugin's `patch-pegaprox.sh` invoked the rebuild with `> /dev/null 2>&1`, so
when the build aborted with `app.jsx: Permission denied` (stale root-owned
artefacts in `web/Dev/.build/` from a prior root-triggered run) nothing was
logged and the orchestrator reported "Build had warnings (may still work)"
while the production bundle remained the stock unpatched release.

Net effect: after auto-updating to 0.9.6.1 the "DOCKER SWARM" sidebar entry
vanished; source was patched, bundle was not.

### Changed

- **`patch-pegaprox.sh` — fail-loud frontend build** (step 4/4):
  - Drops `> /dev/null 2>&1`. All Babel/Node stdout+stderr now flows through
    `sed` into the systemd journal, prefixed with `      ` for readability.
  - Pre-cleans `web/Dev/.build/app.jsx` and `app.js` before rebuilding so a
    root-owned artefact from an earlier run cannot EPERM the `cat > app.jsx`
    truncate step in `build.sh`.
  - Adds a post-build sanity check: fails hard with exit 1 if the compiled
    `web/index.html` does not contain `sidebarDockerSwarm` (catches silent
    Babel failures + any future build-path drift).
  - `chown pegaprox:pegaprox` on the generated `web/index.html` and the
    `.build/` cache so the Flask process (running as `pegaprox`) can always
    read the bundle regardless of who triggered the patch.
  - `patch_dashboard.py` failure is now fatal for the orchestrator (exit 1)
    instead of a silent `| tail -1`.

- **`patch_dashboard.py` — strict anchor validation**:
  - Each of the 9 `str.replace()` steps is now wrapped in
    `_require_replace()` / `_require_rfind_replace()` which exits with code 2
    and a clear error if the anchor is missing, instead of incrementing a
    counter unconditionally and reporting "Applied N patches" when in fact
    nothing was patched.
  - Exit codes documented: 0 clean or already-patched, 1 I/O, 2 upstream
    refactor detected.
  - Applied-patch list is printed by label so the journal shows exactly
    which of the 9 integrations (state, conditions, sidebar, iframe,
    topology fetch, topology concat, auto-refresh, etc.) landed.

### Meta

- `manifest.json` version bumped `1.0.0` → `1.9.0` to align with the repo
  tag. The `1.0.0` marker had been stale since v1.x and was never used as a
  compatibility gate.

### Verified on CT 119 (pegasus.idkmanager.com)

- Manual rebuild as root generated `web/index.html` (3.73 MB) containing
  `sidebarDockerSwarm` ×3; browser reload shows the "DOCKER SWARM" sidebar
  entry restored on PegaProx 0.9.6.1.
- Layer 2 (nginx sub_filter for the `h-[85vh]` console modal fix, v1.8.3)
  unchanged and still healthy.
- `systemctl status pegaprox-patch.path pegaprox-nginx-fix.path` → active
  (waiting).

## [1.8.4] — 2026-04-22

### Fixed
- Disk auto-prune: add missing `ThreadPoolExecutor` import that caused the
  background poller to crash the first time the threshold was exceeded.

## [1.8.3] — 2026-04-22

### Added
- `pegaprox-nginx-fix.path` systemd watcher: if anything rewrites
  `/etc/nginx/sites-available/pegaprox` (reinstall, `apt upgrade`, manual
  edit) the include for `snippets/pegaprox-ds-fixes.conf` is re-wired in
  under 3 seconds. Console-modal height fix is now self-healing.

## [1.8.2] — 2026-04-22

### Changed
- Moved the Tailwind `h-[85vh]` CSS injection from a dashboard.js patcher
  (erased on every PegaProx auto-update) into an nginx `sub_filter` so the
  console modal height fix survives PegaProx updates.

## [1.8.1] — 2026-04-22

### Fixed
- Collapsed VNC/xterm console modal regression introduced by PegaProx
  `Beta 0.9.6.1` (Tailwind arbitrary-value class `h-[85vh]` shipped without
  the matching CSS rule — JIT not used in production).

## [1.8.0] — earlier

### Added
- Disk management: manual prune endpoints + automatic policy with threshold,
  interval and whitelisted targets.

---

Older versions are tracked in the git history only.

# Changelog

All notable changes to the PegaProx Docker Swarm plugin are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This
project follows Semantic Versioning.

## [1.10.0] — 2026-05-02

Major performance pass + first new feature since the security work. Backwards
compatible with existing config / state. Recommended upgrade.

### Performance — SSH connection pool (P1)

`_ssh_exec` was opening a fresh paramiko `SSHClient` per call: TCP handshake,
SSH key exchange, auth, single command, tear-down. With 3 managers and ~30
services in a poll cycle, the previous implementation generated **~95 TCP+SSH
handshakes every 30 s**. Now there is one long-lived connection per host,
kept alive with `transport.set_keepalive(30)`, and each `exec_command` opens
a new channel on the existing transport — paramiko's safe parallelism path.

- New `_ssh_pool` dict + `_ssh_get_client(host_cfg)` helper.
- `_ssh_exec` retries once with a fresh connection if the pooled transport
  died between fetch and use (handles sshd idle timeout, network blip).
- `_ssh_pool_close_all()` invalidates the pool when the user saves new host
  credentials via `/config/save` — auth or host changes get picked up
  immediately.
- Stale entries (idle > 10 min) are recycled on next use.

Measured impact (live cluster, 3 managers): poll cycle latency
**~5–8 s → ~1 s** at warm pool, sshd fork count on each manager drops ~95 %,
each interactive operation (scale, restart) saves the 200–400 ms handshake
that previously dominated.

### Performance — eliminate N+1 SSH calls in fetchers (P2)

`_fetch_services`, `_fetch_stacks` and `_fetch_nodes` were issuing one SSH
call per item — 30 services = 30 round-trips serialised through paramiko.
The same `xargs -I{} docker … inspect {}` pattern that already worked in
`_fetch_networks` and `_fetch_volumes` now applies here too:

- **`_fetch_services`**: `docker service ls -q | xargs -r -I{} docker
  service inspect {} --format "{{json .}}"` — **N → 1 SSH call**.
- **`_fetch_stacks`**: now derives entirely from the `services` cache (label
  `com.docker.stack.namespace` is already populated). On a cold cache it
  falls through to a fresh `_fetch_services()`. **N+1 → 0 SSH calls** in
  steady state.
- **`_fetch_nodes`**: `docker node ls -q | xargs -r -I{} docker node
  inspect {} --format "{{json .}}"` — **N → 1 SSH call**.
- **`_bg_poll_once`** is now two-phase: phase 1 fetches `overview`, `nodes`,
  `services` in parallel; phase 2 (`stacks`) runs after phase 1 completes
  so it can consume the `services` cache. Worker pool reduced 4 → 3.

Combined with P1, a typical poll cycle drops from ~95 to **~8 SSH calls**
in roughly **~1 s** total (cluster of 3 managers and 30 services).

### Added — Webhooks for CI/CD trigger (A4)

Each Swarm service can now have a webhook URL that CI systems POST to,
triggering `docker service update --image <repo>:<tag> --force` on the
service. Authentication is the secret in the URL (cryptographically random
UUID4), validated with `hmac.compare_digest` to avoid timing attacks.
Ported from Portainer's webhook concept; no agent or extra port required.

- `POST /webhook-create`  body `{service_name}` → returns `{id, secret, url_path}`.
  Secret is shown ONCE on creation; admins can list it again with
  `GET /webhooks?unmask=1` (audited).
- `POST /webhook-revoke`  body `{id}` removes the webhook.
- `GET  /webhooks`         lists configured webhooks (secrets masked by default).
- `POST /webhook/trigger?id=<wid>&secret=<secret>[&tag=<tag>]` — the
  CI-facing endpoint. With `tag`, replaces the image tag on the service
  (`repo:newtag`); without it, just `--force`-updates (re-pull with same tag
  if the registry advertises a new digest). Tag value is validated against
  `_RX_IMAGE_REF` before interpolation.
- Webhooks persist to `state/webhooks.json` (mode 600) with atomic write.
- Trigger stats tracked: `last_triggered_at`, `last_triggered_tag`, `trigger_count`.
- Every successful trigger AND every rejected attempt (bad id, bad secret,
  bad tag) is logged via `log_audit('webhook', …)`.

Example for GitHub Actions:

```yaml
- name: Force-update Swarm service
  run: |
    curl -fsSL -X POST \
      "https://pegasus.example.com/api/plugins/docker_swarm/api/webhook/trigger?id=${{ secrets.WH_ID }}&secret=${{ secrets.WH_SECRET }}&tag=${{ github.sha }}"
```

### Notes

- `_ssh_pool_close_all` runs on every `config-save` — expect a brief
  reconnection cost on the next poll, then warm again. No user-visible
  effect.
- `_fetch_stacks` no longer makes its own `service inspect` call. If a
  service was created between the `service ls` and the cache being
  consulted, it appears in the next poll cycle.
- Webhooks are unauthenticated by design (CI systems can't carry a PegaProx
  session). The secret in the URL is the only auth — treat it like a deploy
  key. Rotation: revoke + create.

## [1.9.5] — 2026-05-02

Follow-up to v1.9.4 after a second-pass review. Addresses the issues caught
during double-review.

### Fixed

- **Silent unmask downgrade**. `service-detail` and `stack-detail` previously
  accepted `?unmask=1` from non-admins by silently returning masked envs +
  no error, so the UI confirm dialog (which warned "queda registrado en el
  audit log") was lying to non-admins. Now both endpoints return an explicit
  HTTP 403 when a non-admin requests unmask, and the frontend toasts the
  error. (Reported by frontend reviewer.)
- **`stack-deploy` cleanup race**. The `trap "rm -f \"$tmpf\"" EXIT` was
  installed AFTER `printf … | base64 -d > "$tmpf"` — if the write failed
  (disk full, missing `base64`, etc.) the temp file leaked. Trap now runs
  immediately after `mktemp`, so cleanup is guaranteed even when later
  steps fail under `set -e`.
- **State file atomicity**. `_api_stack_stop` now writes the JSON via a
  sibling `.tmp` file + `os.replace` so concurrent `stack-stop` calls on
  the same stack cannot leave a half-written state file (which would have
  forced `stack-start` to fall back to `replicas=1` for every service).
- **`refresh` and `node-stats` are now admin only**. Both triggered SSH
  fan-outs to every Swarm host, usable as a low-grade DoS by any user with
  `plugins.view`. `node-stats` additionally exposed CPU load, memory, disk
  totals, and uptime — minor reconnaissance signal.
- **`container-action` now invalidates the `overview` cache** (via
  `_invalidate('nodes')`) so the running/stopped/paused container counts
  update immediately on the dashboard after a start/stop/remove.
- **Stack envs view ported to v1.9.4 hardening**. The `Variables` tab in
  the stack detail view used the OLD client-side regex
  (`password|secret|key|token|api_key`) and rendered server-masked `***`
  as a literal red `***`. Now it uses the same expanded regex as the
  service detail view, recognises server-side masking, displays bullets
  (`••••••••`), and has the same `Mostrar reales (admin)` toggle that
  calls `?unmask=1`. (Reported by frontend reviewer.)
- **Masked value render**. Both env views now display `••••••••` (bullets)
  instead of the literal `***` that came from the server. `***` was visually
  confusing — looked like a real value or like corrupt data.
- **`force` flag explicit-bool cast** in `service-update` (`bool(x) is True`)
  so a string body like `{"force": "; rm -rf /"}` is unambiguously rejected
  rather than relying on truthy-evaluation. The `--force` literal was
  already safe; this is just defence in depth.

### Internal

- **Validator consolidation completed** for the few endpoints v1.9.4 missed:
  `service-logs`, `container-logs`, `stack-logs`, `tasks`. All now use
  `_valid(_RX_DOCKER_REF, …)` / `_valid(_RX_STACK_NAME, …)` and
  `shlex.quote()`. The CHANGELOG of v1.9.4 claimed this was already done
  everywhere — now it actually is.
- `_fetch_tasks(service_id)` documents that callers must pre-validate
  `service_id`, and quotes it defensively anyway.

## [1.9.4] — 2026-05-02

Security & correctness pass. **Recommend everyone upgrade.**

### Fixed — security (critical)

- **RBAC**: every state-mutating endpoint now requires admin. Previously the
  following were callable by any authenticated PegaProx user with `plugins.view`:
  `service-scale`, `service-restart`, `service-rollback`, `service-update`,
  `container-action` (incl. `rm -f`), `node-action` (drain/active/pause),
  `image-pull`, `image-remove`, `volume-remove`, `network-remove`, `stack-stop`,
  `stack-start`, `rebalance-service`, `test-connection`. `stack-compose` is
  also admin-only now because the reconstructed YAML embeds env vars.
- **Command injection** in `service-update`: `image`, `limit_cpu`,
  `limit_memory`, `env_add[]` and `env_rm[]` were interpolated raw into the
  shell command. A body like `{"image": "x; rm -rf / #"}` would execute as
  the SSH user on the manager. All user-controlled args are now validated
  against allowlist regexes and `shlex.quote`'d. Same fix applied to
  `image-pull`, `image-remove`, `volume-remove`, `network-remove`,
  `service-rollback`, `service-restart`, `service-scale`, `service-remove`,
  `node-action`, `container-action`, `stack-deploy`, `stack-stop`,
  `stack-start`, `stack-remove`, `rebalance-service`.
- **SSH host-key TOFU + persistence** replaces silent `AutoAddPolicy()`.
  Host keys are recorded in `<plugin>/known_hosts` (mode 0600) on first
  contact; later mismatches are rejected by paramiko (`BadHostKeyException`),
  catching MITM attempts after the first connection. Previously, anyone
  who could redirect TCP to a Swarm manager IP could harvest the SSH
  password we sent.
- **Path traversal in `/tmp`**: `stack-deploy` no longer writes to a
  predictable `/tmp/_pegaprox_stack_<name>.yml` (where any local user could
  pre-create a symlink to overwrite arbitrary files). It now uses
  `mktemp(1)` on the remote and cleans up via `trap`. Stack stop/start
  state was being persisted to `/tmp/_pegaprox_stack_replicas_<name>.json`,
  also predictable; that state now lives **locally** under
  `<plugin>/state/stack_<name>.json` (mode 0600) and is cleared when the
  stack is removed.
- **`test-connection` is admin-only**. Before, any authenticated user could
  abuse this endpoint to make the PegaProx server SSH-connect to arbitrary
  internal IPs with arbitrary credentials — a clean SSRF + credential-spray
  oracle. Host and user inputs are now also strictly validated.
- **Server-side env masking**. `service-detail` and `stack-detail` previously
  returned env vars in plaintext; the UI masked them client-side, so any
  user could read them with DevTools. Sensitive keys (matching
  `password|secret|token|apikey|jwt|bearer|auth|private|credential|dsn|
  passwd|passphrase`) are now masked server-side. Admins can request
  `?unmask=1` to view the real values; each unmask is logged via
  `log_audit`.

### Fixed — correctness

- **`balance_score` was wrong in three edge cases**:
  - 1 healthy node out of N configured: previously returned 100
    ("perfect balance") when in fact nothing is being balanced; now
    returns 0 with a recommendation explaining the situation.
  - All tasks concentrated on a single node (others healthy but idle):
    previously returned a high score because the std-dev calc skipped
    when "only one active node"; now returns 0 with a clear
    recommendation naming the saturated node.
  - The "active nodes" filter is now "healthy nodes" (no error). Idle
    healthy nodes count toward the average, so cluster *capacity* is
    reflected, not just the busy subset.

- **`docker_swarm.topology` determinism**: service→node fallback assignment
  used `hash(svc_name) % len(nodes)`, which changes between processes
  because Python randomises `hash()` (PYTHONHASHSEED). The topology view
  showed services on different nodes after every restart. Replaced with
  `zlib.crc32` (deterministic across processes/restarts).

- **`poll_interval` now reloaded each iteration** of the background poll
  loop. Previously it was read once at thread start, so changing the
  setting from the UI required a plugin restart to take effect.

- **Cache invalidation** on mutations is now centralised in
  `_invalidate(domain)`. Mutations to services also invalidate `overview`
  and `stacks` (and vice versa). Previously some mutations only cleared
  one cache key, so the UI showed stale data for up to 8s after an
  action across tabs.

### Added

- `_invalidate(domain)` cache helper.
- `_PersistentTOFUPolicy` paramiko host-key policy that auto-saves to
  `known_hosts` on first contact.
- `state/` subdirectory under the plugin for non-secret persistent state
  (currently: stack stop/start replica counts).
- Admin "Show real values" / "Hide" toggle in the service-detail Env tab
  (calls `?unmask=1`, audited).
- `api()` helper now surfaces HTTP status as `_http` and translates
  403 to a friendly Spanish message.

### Internal

- All input validators consolidated to module-level regexes
  (`_RX_DOCKER_REF`, `_RX_STACK_NAME`, `_RX_IMAGE_REF`, `_RX_RESOURCE`,
  `_RX_ENV_ENTRY`, `_RX_HOSTNAME`, `_RX_USERNAME`).
- Replaced ad-hoc inline `all(c.isalnum() or c in '-_.')` checks with
  `_valid(rx, s)` calls — cleaner and easier to audit.

## [1.9.3] — 2026-04-24

### Added — persistence layer (2 new defence-in-depth units)

The filesystem watcher from v1.8.3 (`pegaprox-patch.path`) is fast (<3 s
response to file changes) but can miss a few edge cases:
  - PegaProx updates that replace files via atomic `mv` with identical mtime
  - Host reboot mid-update, so the PathChanged event is never delivered
  - Manual edits that rewrite a file without changing its checksum
  - Plugin reinstalls that rewrite every file in a tight loop faster than
    systemd.path debounce

`setup_persistence.sh` (new, idempotent) installs two extra units:

- **`pegaprox-patch-ensure.timer`** — periodic (`OnBootSec=20s`,
  `OnUnitActiveSec=5min`). Fires `pegaprox-patch-ensure.service`, which
  runs `ensure-patches.sh` — a tiny script that greps for all five
  expected markers (`sidebarDockerSwarm`, `frame-ancestors`,
  `DS-VNC-SUBPROTOCOL`, `DS-VNC-AUTH-CONTEXT`,
  `DS-VNC-TICKET-PASSTHROUGH`) and only runs the full orchestrator if any
  of them is missing. Cheap no-op when everything is healthy.
- **`pegaprox-patch-boot.service`** — oneshot, `WantedBy=multi-user.target`.
  Runs the same drift check once every boot, independent of the timer
  (belt-and-suspenders for the case where the timer is ever disabled).

### Changed

- `patch-pegaprox.sh` now ends with an unconditional call to
  `setup_persistence.sh`, so every patch cycle refreshes the healing
  units (in case they were stopped, masked, or the unit files were
  overwritten).

### Persistence layers now in place

  | Layer | Mechanism                                       | Trigger          | Latency   |
  |-------|-------------------------------------------------|------------------|-----------|
  | 1     | `pegaprox-patch.path`                           | inotify          | <3 s      |
  | 2     | `pegaprox-patch-ensure.timer`                   | every 5 min      | ≤5 min    |
  | 3     | `pegaprox-patch-boot.service`                   | every boot       | ~1 boot   |
  | 4     | `pegaprox-nginx-fix.path` (v1.8.3, unchanged)   | inotify on nginx | <3 s      |

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

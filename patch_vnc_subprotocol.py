#!/usr/bin/env python3
"""
Teach the PegaProx VNC WebSocket server to handle the `binary` subprotocol
that noVNC requests — without rejecting clients that omit the subprotocol.

### Background

PegaProx 0.9.7 ships `start_vnc_websocket_server` calling
`websockets.serve(vnc_handler, …, ping_interval=20, ping_timeout=10)` — but
without `subprotocols=`. Meanwhile noVNC opens the socket with
`new WebSocket(url, ['binary'])`. Per RFC 6455, when the client advertises
subprotocols the server MUST echo one back or browsers close with code 1006
before the `open` event fires. Symptom: VM console shows "Error de conexión"
on every VM in every tenant.

### v1.9.0 first cut (and why it wasn't enough)

v1.9.0 added `subprotocols=['binary']` to the two `websockets.serve` call
sites. That fixed browsers that DO advertise `binary` — but it broke any
client that doesn't: the websockets default behaviour, when `subprotocols=`
is set and no overlap exists with the client's list, is `NegotiationError`
→ **HTTP 400 Bad Request**. Users saw "Connecting…" forever.

Cached noVNC, old PegaProx builds, `curl`, `wscat`, internal health probes,
etc. all send no subprotocol — v1.9.0 rejected them all.

### v1.9.1 fix (this file)

Replace `subprotocols=['binary']` with a permissive `select_subprotocol=`
callback that:
  * returns `'binary'` when the client advertised it (noVNC case → echoes
    back, browser fires `open` and negotiates `ws.protocol = 'binary'`)
  * returns `None` when the client advertised nothing or something else
    (permissive clients, cached/older noVNC → connection proceeds without a
    negotiated subprotocol, which is RFC-valid)

This keeps the v1.9.0 behaviour for modern noVNC while un-breaking everything
else.

### Idempotency

The patcher handles three states:
  1. Stock PegaProx 0.9.7 (no subprotocol kwarg)     → install select_subprotocol
  2. v1.9.0 patched (subprotocols=['binary'])        → swap to select_subprotocol
  3. Already v1.9.1 patched (select_subprotocol set) → exit 0

Marker: `DS-VNC-SUBPROTOCOL` (kept from v1.9.0 to keep the watcher skip-check
stable; that check still works because the marker remains).

### Exit codes

  0 OK / already patched
  1 I/O error
  2 anchor not found (upstream changed beyond recognition — re-inspect)
"""
import os
import sys

TARGET = "/opt/PegaProx/pegaprox/api/vms.py"
MARKER = "DS-VNC-SUBPROTOCOL"


HELPER_MARKER = "def _ds_vnc_select_subprotocol"
HELPER_BLOCK = (
    "\n"
    "    # DS-VNC-SUBPROTOCOL helper (v1.9.1): permissive subprotocol negotiation.\n"
    "    # noVNC opens the socket with ['binary'] — we must echo it back or the\n"
    "    # browser closes with code 1006 before `open` fires. Permissive clients\n"
    "    # (curl / wscat / older cached noVNC) advertise nothing — we must NOT\n"
    "    # reject them either, which is why we can't just pass subprotocols=.\n"
    "    def _ds_vnc_select_subprotocol(connection, subprotocols):\n"
    "        return 'binary' if 'binary' in subprotocols else None\n"
    "\n"
)

# v1.9.0 form (with subprotocols=['binary']): swap to select_subprotocol
V190_PRIMARY = (
    "async with websockets.serve(vnc_handler, ws_host, port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10, subprotocols=['binary']):  # " + MARKER
)
V190_FALLBACK = (
    "async with websockets.serve(vnc_handler, '0.0.0.0', port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10, subprotocols=['binary']):  # " + MARKER
)

# Stock PegaProx 0.9.7 form (no subprotocol kwarg)
STOCK_PRIMARY = (
    "async with websockets.serve(vnc_handler, ws_host, port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10):"
)
STOCK_FALLBACK = (
    "async with websockets.serve(vnc_handler, '0.0.0.0', port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10):"
)

# v1.9.1 target form — permissive via select_subprotocol
NEW_PRIMARY = (
    "async with websockets.serve(vnc_handler, ws_host, port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10, "
    "select_subprotocol=_ds_vnc_select_subprotocol):  # " + MARKER
)
NEW_FALLBACK = (
    "async with websockets.serve(vnc_handler, '0.0.0.0', port, ssl=ssl_context, "
    "ping_interval=20, ping_timeout=10, "
    "select_subprotocol=_ds_vnc_select_subprotocol):  # " + MARKER
)


def _die(msg, code=2):
    sys.stderr.write("[patch_vnc_subprotocol] FATAL: " + msg + "\n")
    sys.exit(code)


def main():
    if not os.path.isfile(TARGET):
        _die(TARGET + " not found", code=1)

    with open(TARGET, "r", encoding="utf-8") as f:
        content = f.read()

    if "select_subprotocol=_ds_vnc_select_subprotocol" in content:
        print("[patch_vnc_subprotocol] already v1.9.1 — skipping")
        return 0

    # Ensure helper function is defined inside start_vnc_websocket_server.
    # Anchor: the function signature of start_vnc_websocket_server.
    helper_anchor = (
        "def start_vnc_websocket_server(port=5001, ssl_cert=None, "
        "ssl_key=None, host='0.0.0.0'):\n"
        '    """Start a dedicated WebSocket server for VNC proxying"""\n'
    )
    if HELPER_MARKER not in content:
        if helper_anchor not in content:
            _die("start_vnc_websocket_server signature anchor not found")
        content = content.replace(helper_anchor, helper_anchor + HELPER_BLOCK, 1)

    # Swap the two serve() calls to the permissive form.
    # Accept either the v1.9.0 shape or the stock PegaProx 0.9.7 shape.
    swapped_primary = swapped_fallback = False

    if V190_PRIMARY in content:
        content = content.replace(V190_PRIMARY, NEW_PRIMARY, 1)
        swapped_primary = True
    elif STOCK_PRIMARY in content:
        content = content.replace(STOCK_PRIMARY, NEW_PRIMARY, 1)
        swapped_primary = True

    if V190_FALLBACK in content:
        content = content.replace(V190_FALLBACK, NEW_FALLBACK, 1)
        swapped_fallback = True
    elif STOCK_FALLBACK in content:
        content = content.replace(STOCK_FALLBACK, NEW_FALLBACK, 1)
        swapped_fallback = True

    if not swapped_primary:
        _die("primary websockets.serve anchor not found (neither v1.9.0 nor stock)")
    if not swapped_fallback:
        _die("fallback websockets.serve anchor not found (neither v1.9.0 nor stock)")

    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(content)

    print("[patch_vnc_subprotocol] v1.9.1 applied: permissive select_subprotocol installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

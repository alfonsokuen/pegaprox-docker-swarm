#!/usr/bin/env python3
"""
Teach the PegaProx VNC WebSocket server to negotiate the `binary` subprotocol
that noVNC requests.

Why: noVNC opens the WebSocket with `new WebSocket(url, ['binary'])` (see
PegaProx static/js/novnc/core/rfb.js, which the PegaProx SPA uses to render
the VM console). Per RFC 6455, when a client advertises subprotocols the
server MUST either echo one of them back in the 101 response or the browser
tears the connection down with close code 1006 before the `open` event fires.

PegaProx's `websockets.serve(vnc_handler, ...)` never passed `subprotocols=`,
so:
  - Chrome/Firefox/Edge: 101 Switching → immediate close 1006, noVNC retries 3×
    and shows "Error de conexión" / "Reconnecting (2/3)…"
  - Permissive clients (nc, curl, python websocket): work fine — which hid the
    bug from the PegaProx integration tests.

Verified diagnosis (CT 119, 2026-04-24):
  Browser  new WebSocket(url, ['binary'])  →  close 1006 at 44 ms, no `open`
  Browser  new WebSocket(url)              →  open at 40 ms, `RFB 003.008\\n` at 1.5 s

Fix: add `subprotocols=['binary']` to both `websockets.serve` calls in the
primary and IPv4-fallback branches of `start_vnc_websocket_server`.

Idempotent via marker `DS-VNC-SUBPROTOCOL`.

Exit codes: 0 OK / already patched, 1 I/O, 2 anchor not found.
"""
import os
import sys

TARGET = "/opt/PegaProx/pegaprox/api/vms.py"
MARKER = "DS-VNC-SUBPROTOCOL"


def _die(msg, code=2):
    sys.stderr.write("[patch_vnc_subprotocol] FATAL: " + msg + "\n")
    sys.exit(code)


def main():
    if not os.path.isfile(TARGET):
        _die(TARGET + " not found", code=1)

    with open(TARGET, "r", encoding="utf-8") as f:
        content = f.read()

    if MARKER in content:
        print("[patch_vnc_subprotocol] already patched (marker present) - skipping")
        return 0

    # Two call sites inside start_vnc_websocket_server(): primary IPv6+IPv4 bind,
    # and the IPv4-only fallback when the first bind raises OSError.
    OLD_PRIMARY = (
        "async with websockets.serve(vnc_handler, ws_host, port, ssl=ssl_context, "
        "ping_interval=20, ping_timeout=10):"
    )
    NEW_PRIMARY = (
        "async with websockets.serve(vnc_handler, ws_host, port, ssl=ssl_context, "
        "ping_interval=20, ping_timeout=10, subprotocols=['binary']):  # " + MARKER
    )

    OLD_FALLBACK = (
        "async with websockets.serve(vnc_handler, '0.0.0.0', port, ssl=ssl_context, "
        "ping_interval=20, ping_timeout=10):"
    )
    NEW_FALLBACK = (
        "async with websockets.serve(vnc_handler, '0.0.0.0', port, ssl=ssl_context, "
        "ping_interval=20, ping_timeout=10, subprotocols=['binary']):  # " + MARKER
    )

    if OLD_PRIMARY not in content:
        _die("primary websockets.serve anchor not found")
    if OLD_FALLBACK not in content:
        _die("fallback websockets.serve anchor not found")

    content = content.replace(OLD_PRIMARY, NEW_PRIMARY, 1)
    content = content.replace(OLD_FALLBACK, NEW_FALLBACK, 1)

    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(content)

    print("[patch_vnc_subprotocol] subprotocols=['binary'] added to both websockets.serve call sites")
    return 0


if __name__ == "__main__":
    sys.exit(main())

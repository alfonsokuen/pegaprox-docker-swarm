#!/usr/bin/env python3
"""
Comprehensive VNC fix — combines ticket passthrough with manager auth-context
reuse (PVEAPIToken OR PVEAuthCookie).

### Root cause (re-confirmed on CT 119, 2026-04-24 18:53 UTC)

PegaProx 0.9.7's VNC WS handler:
  1. Browser fetches /vnc → manager POSTs /vncproxy on PVE → ticket_A.
     PVE side-effect: `set_password vnc <ticket_A>` on QEMU monitor →
     **QEMU's VNC password is now ticket_A[:8]** (DES key for RFB auth).
  2. Browser opens WebSocket with `{password: ticket_A}`.
  3. Handler runs a **fresh login** and **calls /vncproxy AGAIN** → ticket_B.
     Side-effect: QEMU's VNC password is now ticket_B[:8], overwriting A.
  4. Browser sends `DES(ticket_A[:8])(challenge)`; QEMU expects ticket_B[:8]
     → RFB Authentication failure. Wire shape: sent 29B, recv 60B
     (12 server-hello + 3 sec-types + 16 challenge + 30 "Authentication
     failure"). Exactly what our diag probe captured.

Previous attempts and why they failed:
  * v1.9.2 (ticket passthrough only): browser's ticket + handler's fresh
    login cookie → PVE rejects upstream WS with "invalid PVEVNC ticket"
    because the vncticket was emitted under a DIFFERENT auth context
    (the manager's API token, not the handler's fresh user/pass session).
  * v1.9.2b (manager._ticket cookie): `manager._ticket` is `None` because
    PegaProx clusters are configured with API tokens (see IDK Cluster:
    `api_token_user = root@pam!pegaprox_…`), so manager never populates
    `_ticket`. Fell through to fresh login → same cookie mismatch.

### This fix (v1.9.2)

Both halves together, respecting API token auth:

  A) **UI passthrough**: `node_modals.js` appends `&vncticket=…&vncport=…`
     to the wsUrl so the handler uses the SAME ticket the browser has.
  B) **Server auth-context reuse**: inside `vnc_handler`, connect upstream
     with the manager's SAME auth credentials that emitted the ticket:
       - If `manager._using_api_token`: send
         `Authorization: PVEAPIToken={manager._api_token}`
       - Elif `manager._ticket`:          send `Cookie: PVEAuthCookie={…}`
       - Else fresh login fallback (preserves backward compat).
     Also SKIP the handler's redundant POST /vncproxy entirely when the
     browser already passed ticket+port (this is the bit that stops QEMU's
     password from being clobbered).

Markers: `DS-VNC-TICKET-PASSTHROUGH` (both files), `DS-VNC-AUTH-CONTEXT`
(vms.py). Idempotent.

Exit codes: 0 OK / already patched, 1 I/O, 2 anchor not found.
"""
import os
import sys

VMS_PY = "/opt/PegaProx/pegaprox/api/vms.py"
NODE_MODALS = "/opt/PegaProx/web/src/node_modals.js"
MARKER_UI = "DS-VNC-TICKET-PASSTHROUGH"
MARKER_SRV = "DS-VNC-AUTH-CONTEXT"


# ---------------------------------------------------------------------------
# UI patch — append ticket/port to wsUrl
# ---------------------------------------------------------------------------

UI_OLD = (
    "const wsUrl = `${wsProtocol}//${window.location.hostname}:${vncPortNum}/api/clusters/"
    "${clusterId}/vms/${vm.node}/${vm.type}/${vm.vmid}/vncwebsocket?token=${encodeURIComponent(vncWsToken)}`;"
)
UI_NEW = (
    "// " + MARKER_UI + " — pass the ticket the browser already has so the\n"
    "                        // handler uses the SAME ticket (avoids a second POST /vncproxy\n"
    "                        // that would reset QEMU's VNC password and break RFB auth).\n"
    "                        const _dsVncExtras = "
    "'&vncticket=' + encodeURIComponent(ticketData.ticket) + "
    "'&vncport=' + encodeURIComponent(ticketData.port);\n"
    "                        const wsUrl = `${wsProtocol}//${window.location.hostname}:${vncPortNum}/api/clusters/"
    "${clusterId}/vms/${vm.node}/${vm.type}/${vm.vmid}/vncwebsocket?token=${encodeURIComponent(vncWsToken)}`"
    " + _dsVncExtras;"
)


# ---------------------------------------------------------------------------
# Server patch — replace the login+vncproxy block with auth-context-aware
# logic that skips the redundant fetch when the browser already supplied
# the ticket.
# ---------------------------------------------------------------------------

OLD_BLOCK = (
    "            # Login to Proxmox to get auth ticket\n"
    "            login_data = urlencode({\n"
    "                'username': manager.config.user,\n"
    "                'password': manager.config.pass_\n"
    "            }).encode('utf-8')\n"
    "\n"
    "            login_req = urllib.request.Request(\n"
    "                f\"https://{host}:8006/api2/json/access/ticket\",\n"
    "                data=login_data, method='POST'\n"
    "            )\n"
    "\n"
    "            with urllib.request.urlopen(login_req, context=ssl_ctx, timeout=10) as response:\n"
    "                login_result = json.loads(response.read().decode('utf-8'))\n"
    "\n"
    "            pve_ticket = login_result['data']['ticket']\n"
    "            csrf_token = login_result['data']['CSRFPreventionToken']\n"
    "\n"
    "            # Request VNC proxy ticket (must connect to WS within ~10s)\n"
    "            if vm_type == 'qemu':\n"
    "                vnc_url = f\"https://{host}:8006/api2/json/nodes/{node}/qemu/{vmid}/vncproxy\"\n"
    "            else:\n"
    "                vnc_url = f\"https://{host}:8006/api2/json/nodes/{node}/lxc/{vmid}/vncproxy\"\n"
    "\n"
    "            vnc_data = urlencode({'websocket': '1'}).encode('utf-8')\n"
    "            vnc_req = urllib.request.Request(vnc_url, data=vnc_data, method='POST')\n"
    "            vnc_req.add_header('Cookie', f'PVEAuthCookie={pve_ticket}')\n"
    "            vnc_req.add_header('CSRFPreventionToken', csrf_token)\n"
    "\n"
    "            with urllib.request.urlopen(vnc_req, context=ssl_ctx, timeout=10) as response:\n"
    "                vnc_result = json.loads(response.read().decode('utf-8'))\n"
    "\n"
    "            vnc_ticket = vnc_result['data']['ticket']\n"
    "            port = vnc_result['data']['port']\n"
)

NEW_BLOCK = (
    "            # " + MARKER_SRV + " — reuse the manager's auth context (API token OR cookie)\n"
    "            # AND prefer the browser's ticket to avoid a second /vncproxy that resets\n"
    "            # QEMU's VNC password. See patch_vnc_auth_context.py for the full story.\n"
    "            _bq_vncticket = query_params.get('vncticket', [None])[0]\n"
    "            _bq_vncport = query_params.get('vncport', [None])[0]\n"
    "\n"
    "            # Decide what auth the upstream WS handshake will carry.\n"
    "            # PegaProx usually connects via API token (see manager._using_api_token);\n"
    "            # the vncproxy ticket was emitted under that same context, so the WS upstream\n"
    "            # handshake MUST carry the same Authorization header to be accepted.\n"
    "            pve_ticket = None  # will be used as Cookie PVEAuthCookie if set\n"
    "            _ds_ws_auth_header_name = None\n"
    "            _ds_ws_auth_header_value = None\n"
    "            if getattr(manager, '_using_api_token', False) and getattr(manager, '_api_token', None):\n"
    "                _ds_ws_auth_header_name = 'Authorization'\n"
    "                _ds_ws_auth_header_value = f'PVEAPIToken={manager._api_token}'\n"
    "                csrf_token = None  # not needed in token auth\n"
    "            elif getattr(manager, '_ticket', None):\n"
    "                pve_ticket = manager._ticket\n"
    "                csrf_token = getattr(manager, '_csrf_token', None)\n"
    "            else:\n"
    "                # Fresh login fallback (used on cold start or when manager lost its session)\n"
    "                login_data = urlencode({\n"
    "                    'username': manager.config.user,\n"
    "                    'password': manager.config.pass_\n"
    "                }).encode('utf-8')\n"
    "                login_req = urllib.request.Request(\n"
    "                    f\"https://{host}:8006/api2/json/access/ticket\",\n"
    "                    data=login_data, method='POST'\n"
    "                )\n"
    "                with urllib.request.urlopen(login_req, context=ssl_ctx, timeout=10) as response:\n"
    "                    login_result = json.loads(response.read().decode('utf-8'))\n"
    "                pve_ticket = login_result['data']['ticket']\n"
    "                csrf_token = login_result['data']['CSRFPreventionToken']\n"
    "\n"
    "            # Happy path: browser supplied ticket+port → skip the second /vncproxy.\n"
    "            # Fallback: re-fetch (legacy behaviour, known to overwrite QEMU's VNC password).\n"
    "            if _bq_vncticket and _bq_vncport:\n"
    "                vnc_ticket = _bq_vncticket\n"
    "                port = _bq_vncport\n"
    "            else:\n"
    "                if vm_type == 'qemu':\n"
    "                    vnc_url = f\"https://{host}:8006/api2/json/nodes/{node}/qemu/{vmid}/vncproxy\"\n"
    "                else:\n"
    "                    vnc_url = f\"https://{host}:8006/api2/json/nodes/{node}/lxc/{vmid}/vncproxy\"\n"
    "                vnc_data = urlencode({'websocket': '1'}).encode('utf-8')\n"
    "                vnc_req = urllib.request.Request(vnc_url, data=vnc_data, method='POST')\n"
    "                if _ds_ws_auth_header_name == 'Authorization':\n"
    "                    vnc_req.add_header('Authorization', _ds_ws_auth_header_value)\n"
    "                else:\n"
    "                    vnc_req.add_header('Cookie', f'PVEAuthCookie={pve_ticket}')\n"
    "                    if csrf_token:\n"
    "                        vnc_req.add_header('CSRFPreventionToken', csrf_token)\n"
    "                with urllib.request.urlopen(vnc_req, context=ssl_ctx, timeout=10) as response:\n"
    "                    vnc_result = json.loads(response.read().decode('utf-8'))\n"
    "                vnc_ticket = vnc_result['data']['ticket']\n"
    "                port = vnc_result['data']['port']\n"
)

# The `create_connection(...)` block afterwards uses `header={'Cookie': f'PVEAuthCookie={pve_ticket}'}`.
# We must also change that line to honour the chosen auth context.

OLD_WS_CONNECT = (
    "            pve_ws = ws_client.create_connection(\n"
    "                pve_ws_url,\n"
    "                sslopt={\"cert_reqs\": ssl.CERT_NONE},\n"
    "                header={\"Cookie\": f\"PVEAuthCookie={pve_ticket}\"},\n"
    "                timeout=5\n"
    "            )\n"
)

NEW_WS_CONNECT = (
    "            # " + MARKER_SRV + " — upstream WS must use the SAME auth context\n"
    "            # that emitted the vncproxy ticket, otherwise PVE returns\n"
    "            # 'permission denied - invalid PVEVNC ticket'.\n"
    "            if _ds_ws_auth_header_name == 'Authorization':\n"
    "                _ds_upstream_headers = {_ds_ws_auth_header_name: _ds_ws_auth_header_value}\n"
    "            else:\n"
    "                _ds_upstream_headers = {\"Cookie\": f\"PVEAuthCookie={pve_ticket}\"}\n"
    "            pve_ws = ws_client.create_connection(\n"
    "                pve_ws_url,\n"
    "                sslopt={\"cert_reqs\": ssl.CERT_NONE},\n"
    "                header=_ds_upstream_headers,\n"
    "                timeout=5\n"
    "            )\n"
)


def _die(msg, code=2):
    sys.stderr.write("[patch_vnc_auth_context] FATAL: " + msg + "\n")
    sys.exit(code)


def main():
    # vms.py
    if not os.path.isfile(VMS_PY):
        _die(VMS_PY + " not found", code=1)
    vms = open(VMS_PY, "r", encoding="utf-8").read()

    if MARKER_SRV in vms:
        print("[patch_vnc_auth_context] vms.py: already patched — skipping")
    else:
        if OLD_BLOCK not in vms:
            _die("vms.py: login+vncproxy anchor not found")
        if OLD_WS_CONNECT not in vms:
            _die("vms.py: create_connection anchor not found")
        vms = vms.replace(OLD_BLOCK, NEW_BLOCK, 1).replace(OLD_WS_CONNECT, NEW_WS_CONNECT, 1)
        open(VMS_PY, "w", encoding="utf-8").write(vms)
        print("[patch_vnc_auth_context] vms.py: handler now reuses manager auth context + accepts browser ticket")

    # node_modals.js
    if not os.path.isfile(NODE_MODALS):
        _die(NODE_MODALS + " not found", code=1)
    nm = open(NODE_MODALS, "r", encoding="utf-8").read()
    if MARKER_UI in nm:
        print("[patch_vnc_auth_context] node_modals.js: already patched — skipping")
    else:
        if UI_OLD not in nm:
            _die("node_modals.js: wsUrl anchor not found")
        nm = nm.replace(UI_OLD, UI_NEW, 1)
        open(NODE_MODALS, "w", encoding="utf-8").write(nm)
        print("[patch_vnc_auth_context] node_modals.js: wsUrl now carries ticket+port")

    return 0


if __name__ == "__main__":
    sys.exit(main())

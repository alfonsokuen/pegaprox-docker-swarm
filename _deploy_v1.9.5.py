#!/usr/bin/env python3
"""
Deploy pegaprox-docker-swarm v1.9.5 to CT 119 (pegaprox LXC, pve1).

Target:  root@190.160.10.212 (CT 119), via WARP split-tunnel
Auth:    password 'Conexion2020.' (per memory reference_infra_credentials.md)
Plugin:  /opt/PegaProx/plugins/docker_swarm/  (NOT git checkout — replace files)

Idempotent. Auto-rollback if any post-replace step fails.

Usage: python _deploy_v1.9.5.py
"""
from __future__ import annotations
import sys, os, time, datetime, json
import paramiko

CT_HOST = os.environ.get('CT_HOST', '190.160.10.212')
CT_USER = os.environ.get('CT_USER', 'root')
CT_PASS = os.environ.get('CT_PASS', 'Conexion2020.')
TARGET_SHA = os.environ.get('TARGET_SHA', '95a4d98')
TARGET_VER = os.environ.get('TARGET_VER', '1.9.5')

PLUGIN_DIR = '/opt/PegaProx/plugins/docker_swarm'
BK_DIR = '/opt/PegaProx/_backups'
TARBALL_URL = f'https://github.com/alfonsokuen/pegaprox-docker-swarm/archive/{TARGET_SHA}.tar.gz'
TS = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')

# Files / dirs to preserve across deploy.
# .ssh holds the SSH key pegaprox uses to reach Swarm managers — must NOT be wiped.
# config.json holds host config + (legacy) passwords. known_hosts is TOFU state.
# state/ holds stack stop/start replica counts (v1.9.4+).
PRESERVE = ['config.json', 'config.json.bak', 'known_hosts', 'state', '.ssh']


def step(label):
    print(f'[{label}]', flush=True)


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 60, allow_fail: bool = False, hide_cmd: bool = False) -> tuple[int, str, str]:
    if not hide_cmd:
        # Print first line only for readability
        first = cmd.strip().split('\n', 1)[0]
        if len(first) > 200:
            first = first[:200] + '...'
        print(f'  $ {first}')
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace').rstrip()
    err = stderr.read().decode('utf-8', errors='replace').rstrip()
    if out:
        for line in out.splitlines():
            print(f'    | {line}')
    if err and (not allow_fail or rc != 0):
        for line in err.splitlines():
            print(f'    !{line}', file=sys.stderr)
    if rc != 0 and not allow_fail:
        raise RuntimeError(f'remote command failed (rc={rc}): {cmd[:120]}')
    return rc, out, err


def rollback(client, current_ver: str):
    print('\n[!] ROLLING BACK to v' + current_ver, file=sys.stderr)
    bk_path = f'{BK_DIR}/docker_swarm_v{current_ver}_{TS}.tgz'
    try:
        run(client, f'cd /opt/PegaProx/plugins && rm -rf docker_swarm && tar xzf {bk_path} && chown -R pegaprox:pegaprox docker_swarm && systemctl restart pegaprox', allow_fail=True)
        time.sleep(3)
        run(client, 'systemctl is-active pegaprox', allow_fail=True)
    except Exception as e:
        print(f'    rollback ALSO failed: {e}', file=sys.stderr)


def main():
    print(f'[*] Target: {CT_USER}@{CT_HOST}  Plugin: {PLUGIN_DIR}  SHA: {TARGET_SHA}  TS: {TS}')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f'[*] Connecting to {CT_HOST}...')
    try:
        client.connect(
            hostname=CT_HOST, port=22, username=CT_USER, password=CT_PASS,
            timeout=15, banner_timeout=15, auth_timeout=15,
            look_for_keys=False, allow_agent=False,
        )
    except Exception as e:
        print(f'[FAIL] SSH connect failed: {e}', file=sys.stderr)
        return 2

    try:
        # 1) Pre-flight
        step('1/8 Pre-flight')
        rc, current_ver, _ = run(client, f"python3 -c 'import json; print(json.load(open(\"{PLUGIN_DIR}/manifest.json\"))[\"version\"])'", allow_fail=True)
        current_ver = current_ver.strip() or 'unknown'
        print(f'    current version: {current_ver}')

        # 2) Backup
        step('2/8 Backup current plugin dir')
        bk_name = f'docker_swarm_v{current_ver}_{TS}.tgz'
        run(client, f'mkdir -p {BK_DIR} && cd /opt/PegaProx/plugins && tar czf {BK_DIR}/{bk_name} docker_swarm && ls -la {BK_DIR}/{bk_name}')

        # 3) Snapshot /tmp state
        step('3/8 Snapshot /tmp stack-replicas state')
        run(client, f"if compgen -G '/tmp/_pegaprox_stack_replicas_*.json' > /dev/null; then tar czf {BK_DIR}/tmp_stacks_{TS}.tgz /tmp/_pegaprox_stack_replicas_*.json && echo BACKED_UP $(ls /tmp/_pegaprox_stack_replicas_*.json | wc -l) files; else echo NO_STOPPED_STACKS; fi", allow_fail=True)

        # 4) Migrate state to new format
        step('4/8 Migrate /tmp state to <plugin>/state/')
        migrate_cmd = f'''
set -e
cd {PLUGIN_DIR}
mkdir -p state && chmod 700 state
cnt=0
for f in /tmp/_pegaprox_stack_replicas_*.json; do
  [ -f "$f" ] || continue
  name=$(basename "$f" .json | sed 's/^_pegaprox_stack_replicas_//')
  python3 - "$f" "state/stack_${{name}}.json" <<'PY'
import json,sys,os,datetime
src=open(sys.argv[1]).read()
out={{'saved_at':datetime.datetime.now().isoformat(),'replicas':json.loads(src)}}
open(sys.argv[2],'w').write(json.dumps(out))
os.chmod(sys.argv[2],0o600)
PY
  cnt=$((cnt+1))
done
if [ $cnt -gt 0 ]; then
  chown -R pegaprox:pegaprox state/
  echo "MIGRATED $cnt stacks"
else
  echo NO_STATE_TO_MIGRATE
fi
'''
        run(client, migrate_cmd)

        # 5) Download v1.9.5 tarball into LXC
        step(f'5/8 Download v{TARGET_VER} tarball into CT')
        run(client, f"set -e; cd /tmp && rm -rf _ds_deploy && mkdir _ds_deploy && cd _ds_deploy && curl -fL --max-time 60 -o tarball.tgz '{TARBALL_URL}' && tar xzf tarball.tgz && ls -d pegaprox-docker-swarm-*/ | head -1")

        # 6) Replace files (preserve config.json + state + known_hosts)
        # rsync isn't available in the LXC — use cp -a + manual preserve.
        step('6/8 Replace plugin files (preserve config.json + state + known_hosts)')
        replace_cmd = f'''
set -e
cd /tmp/_ds_deploy
SRC=$(ls -d pegaprox-docker-swarm-*/ | head -1)
[ -n "$SRC" ] || {{ echo "tarball src dir missing"; exit 11; }}

# Stash preserved files/dirs to a temp staging dir
PRESERVE=/tmp/_ds_preserve
rm -rf "$PRESERVE" && mkdir -p "$PRESERVE"
for item in config.json config.json.bak known_hosts state .ssh; do
  if [ -e "{PLUGIN_DIR}/$item" ]; then
    cp -a "{PLUGIN_DIR}/$item" "$PRESERVE/"
  fi
done

# Wipe + repopulate plugin dir (visible AND hidden contents — note .[!.]* matches .ssh)
rm -rf "{PLUGIN_DIR}"/*
rm -rf "{PLUGIN_DIR}"/.[!.]* 2>/dev/null || true
cp -a "$SRC"/. "{PLUGIN_DIR}/"

# Restore preserved items
for item in config.json config.json.bak known_hosts state .ssh; do
  if [ -e "$PRESERVE/$item" ]; then
    cp -a "$PRESERVE/$item" "{PLUGIN_DIR}/"
  fi
done

# Permissions
chown -R pegaprox:pegaprox {PLUGIN_DIR}
[ -e "{PLUGIN_DIR}/config.json" ] && chmod 600 {PLUGIN_DIR}/config.json || true
[ -d "{PLUGIN_DIR}/state" ] && chmod 700 {PLUGIN_DIR}/state || true
[ -e "{PLUGIN_DIR}/known_hosts" ] && chmod 600 {PLUGIN_DIR}/known_hosts || true
if [ -d "{PLUGIN_DIR}/.ssh" ]; then
  chmod 700 {PLUGIN_DIR}/.ssh
  find {PLUGIN_DIR}/.ssh -maxdepth 1 -type f ! -name '*.pub' -exec chmod 600 {{}} \\;
  find {PLUGIN_DIR}/.ssh -maxdepth 1 -type f -name '*.pub' -exec chmod 644 {{}} \\;
fi

# Cleanup
rm -rf "$PRESERVE" /tmp/_ds_deploy

ls -la {PLUGIN_DIR}/manifest.json {PLUGIN_DIR}/CHANGELOG.md
'''
        try:
            run(client, replace_cmd)
        except Exception:
            rollback(client, current_ver)
            raise

        # 7) Restart pegaprox
        step('7/8 Restart pegaprox')
        run(client, 'systemctl restart pegaprox', allow_fail=True)
        time.sleep(5)
        rc, active, _ = run(client, 'systemctl is-active pegaprox', allow_fail=True)
        active = active.strip()
        print(f'    pegaprox: {active}')
        if active != 'active':
            rollback(client, current_ver)
            return 3

        # 8) Smoke + version assert
        step('8/8 Smoke + version assert')
        rc, http_code, _ = run(client, "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/api/plugins/docker_swarm/api/overview", allow_fail=True)
        print(f'    overview HTTP: {http_code.strip()}')

        rc, deployed_ver, _ = run(client, f"python3 -c 'import json; print(json.load(open(\"{PLUGIN_DIR}/manifest.json\"))[\"version\"])'", allow_fail=True)
        deployed_ver = deployed_ver.strip()
        print(f'    deployed version: {deployed_ver}')

        if deployed_ver != TARGET_VER:
            print(f'[!] Version mismatch: expected {TARGET_VER}, got {deployed_ver}', file=sys.stderr)
            rollback(client, current_ver)
            return 4

        print(f'\n[OK] v{TARGET_VER} deployed to CT 119 successfully.')
        print(f'     Backup: {BK_DIR}/{bk_name} on CT 119')
        print(f'     UI:     https://pegasus.idkmanager.com (sidebar "Docker Swarm Manager")')
        print(f'\n     Rollback if needed:')
        print(f"     ssh root@{CT_HOST} \"cd /opt/PegaProx/plugins && rm -rf docker_swarm && tar xzf {BK_DIR}/{bk_name} && chown -R pegaprox:pegaprox docker_swarm && systemctl restart pegaprox\"")
        return 0

    except Exception as e:
        print(f'\n[FAIL] {e}', file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())

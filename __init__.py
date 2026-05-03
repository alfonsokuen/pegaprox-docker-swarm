# -*- coding: utf-8 -*-
"""
Docker Swarm Manager — PegaProx Plugin
Monitor and manage Docker Swarm clusters via SSH from PegaProx.

Connects to Swarm manager nodes over SSH (using paramiko, already in PegaProx),
executes docker CLI commands, and exposes results via PegaProx plugin API.

Features:
  - Swarm overview dashboard (nodes, services, resource usage)
  - Node listing with status, role, CPU/RAM
  - Service management (list, scale, restart/force-update, logs)
  - Stack listing with service counts
  - Container/task listing with logs
  - Real-time-ish metrics via background polling
"""

import os
import re
import json
import time
import shlex
import zlib
import logging
import threading
from datetime import datetime
from flask import request, jsonify, send_file

from pegaprox.api.plugins import register_plugin_route
from pegaprox.utils.auth import load_users
from pegaprox.utils.audit import log_audit

PLUGIN_ID = 'docker_swarm'
PLUGIN_NAME = 'Docker Swarm Manager'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(PLUGIN_DIR, 'state')
KNOWN_HOSTS_PATH = os.path.join(PLUGIN_DIR, 'known_hosts')
log = logging.getLogger(f'plugin.{PLUGIN_ID}')

# In-memory cache
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 8  # seconds — short for near-realtime feel

# Background thread
_bg_thread = None
_bg_stop = threading.Event()

# Cache key dependency map: which keys to invalidate per "domain"
_CACHE_DEPS = {
    'services': ('services', 'overview', 'stacks'),
    'stacks':   ('stacks', 'services', 'overview'),
    'nodes':    ('nodes', 'overview'),
    'all':      ('overview', 'nodes', 'services', 'stacks'),
}

def _invalidate(domain='services'):
    """Invalidate cache keys related to a domain so the next request fetches fresh data."""
    keys = _CACHE_DEPS.get(domain, (domain,))
    with _cache_lock:
        for k in keys:
            _cache.pop(k, None)


# ---------------------------------------------------------------------------
# Metrics persistence (v1.13.0 — Phase 3)
# ---------------------------------------------------------------------------
# Time-series of per-node CPU/RAM/tasks samples in a SQLite ring buffer.
# Used to power sparklines in the dashboard + the new "Tendencias" tab.
# Sampling cost: ~zero — we piggyback on _api_load_balance which already
# fetches all this data for the existing Balance view. We just write a row.

import sqlite3 as _sqlite3

METRICS_DB = os.path.join(STATE_DIR, 'metrics.db')
METRICS_RETENTION_DAYS = 30
_metrics_lock = threading.Lock()
_metrics_initialized = False


def _metrics_init_db():
    """Create the metrics table + indexes if they don't exist. Idempotent."""
    global _metrics_initialized
    if _metrics_initialized:
        return
    with _metrics_lock:
        if _metrics_initialized:
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        conn = _sqlite3.connect(METRICS_DB, timeout=10)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS node_metrics (
                    ts INTEGER NOT NULL,
                    host TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    cpu_count INTEGER,
                    cpu_percent REAL,
                    mem_used INTEGER,
                    mem_total INTEGER,
                    mem_percent REAL,
                    tasks_running INTEGER,
                    PRIMARY KEY (host, ts)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_node_metrics_ts ON node_metrics(ts DESC)")
            conn.commit()
        finally:
            conn.close()
        _metrics_initialized = True


def _metrics_record_load_balance(load_balance_data):
    """Persist a row per node from a /load-balance result. Called from
    _api_load_balance after the data dict is built. No-op if no data."""
    if not load_balance_data or 'nodes' not in load_balance_data:
        return
    _metrics_init_db()
    ts = int(time.time())
    rows = []
    for n in load_balance_data['nodes']:
        if n.get('error'):
            continue
        rows.append((
            ts,
            n.get('host', ''),
            n.get('hostname', ''),
            int(n.get('cpu_count') or 0),
            float(n.get('cpu_percent') or 0),
            int(n.get('mem_used') or 0),
            int(n.get('mem_total') or 0),
            float(n.get('mem_percent') or 0),
            int(n.get('tasks_running') or 0),
        ))
    if not rows:
        return
    with _metrics_lock:
        conn = _sqlite3.connect(METRICS_DB, timeout=10)
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO node_metrics
                (ts, host, hostname, cpu_count, cpu_percent, mem_used, mem_total, mem_percent, tasks_running)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            # Prune lazily — once every ~5 min worth of samples
            if ts % 300 < 30:
                cutoff = ts - METRICS_RETENTION_DAYS * 86400
                conn.execute("DELETE FROM node_metrics WHERE ts < ?", (cutoff,))
            conn.commit()
        except Exception as e:
            log.error(f"[{PLUGIN_ID}] metrics write failed: {e}")
        finally:
            conn.close()


_RX_DURATION = re.compile(r'(\d+)([smhd])')


def _parse_duration_to_sec(s, default_sec=86400, max_sec=30 * 86400):
    """Parse '24h', '7d', '30m', '1h30m' into seconds. Caps at max_sec.
    Returns default_sec on empty/garbage input."""
    if not s or not isinstance(s, str):
        return default_sec
    matches = _RX_DURATION.findall(s.strip().lower())
    if not matches:
        try:
            return min(int(s), max_sec)  # raw seconds
        except (TypeError, ValueError):
            return default_sec
    total = 0
    for n, unit in matches:
        n = int(n)
        if unit == 's': total += n
        elif unit == 'm': total += n * 60
        elif unit == 'h': total += n * 3600
        elif unit == 'd': total += n * 86400
    return min(total, max_sec) if total > 0 else default_sec


def _metrics_query_history(host, metric, duration_sec):
    """Return a list of {ts, value} for one (host, metric) over the window."""
    _metrics_init_db()
    cutoff = int(time.time()) - duration_sec
    # Whitelist metric column to prevent SQL injection — never interpolate user input
    if metric not in ('cpu_percent', 'mem_percent', 'mem_used', 'tasks_running', 'cpu_count'):
        return []
    with _metrics_lock:
        conn = _sqlite3.connect(METRICS_DB, timeout=10)
        try:
            cur = conn.execute(
                f"SELECT ts, {metric} FROM node_metrics WHERE host = ? AND ts >= ? ORDER BY ts ASC",
                (host, cutoff),
            )
            return [{'ts': r[0], 'value': r[1]} for r in cur.fetchall()]
        finally:
            conn.close()


def _metrics_query_trends(duration_sec):
    """Per-node summary stats over the window: avg/max + current sample."""
    _metrics_init_db()
    cutoff = int(time.time()) - duration_sec
    with _metrics_lock:
        conn = _sqlite3.connect(METRICS_DB, timeout=10)
        try:
            cur = conn.execute("""
                SELECT host, MAX(hostname),
                       AVG(cpu_percent), MAX(cpu_percent),
                       AVG(mem_percent), MAX(mem_percent),
                       AVG(tasks_running), MAX(tasks_running),
                       COUNT(*),
                       MIN(ts), MAX(ts)
                FROM node_metrics
                WHERE ts >= ?
                GROUP BY host
                ORDER BY MAX(hostname)
            """, (cutoff,))
            agg = cur.fetchall()
            # Latest sample per host
            cur2 = conn.execute("""
                SELECT host, cpu_percent, mem_percent, tasks_running, ts
                FROM node_metrics m
                WHERE ts = (SELECT MAX(ts) FROM node_metrics WHERE host = m.host)
            """)
            current = {r[0]: {'cpu': r[1], 'mem': r[2], 'tasks': r[3], 'ts': r[4]} for r in cur2.fetchall()}
        finally:
            conn.close()
    nodes = []
    for r in agg:
        host = r[0]
        cur = current.get(host, {})
        nodes.append({
            'host': host,
            'hostname': r[1],
            'samples': r[8],
            'first_ts': r[9],
            'last_ts': r[10],
            'cpu': {'avg': round(r[2] or 0, 1), 'max': round(r[3] or 0, 1), 'current': round(cur.get('cpu') or 0, 1)},
            'mem': {'avg': round(r[4] or 0, 1), 'max': round(r[5] or 0, 1), 'current': round(cur.get('mem') or 0, 1)},
            'tasks': {'avg': round(r[6] or 0, 1), 'max': r[7] or 0, 'current': cur.get('tasks') or 0},
        })
    return nodes


# ---------------------------------------------------------------------------
# Input validation helpers (allowlists for use in shell command interpolation)
# ---------------------------------------------------------------------------

# Docker IDs / service-names / container-names / volume / network names
_RX_DOCKER_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}$')
# Stack names (more restrictive — used in filenames)
_RX_STACK_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$')
# Image refs: registry/path:tag@sha256:digest — broad but safe (no shell metas)
_RX_IMAGE_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/@\-]{0,254}$')
# Resource limits: numbers, dots, units (m, M, G, etc.)
_RX_RESOURCE = re.compile(r'^[0-9.]+[a-zA-Z]{0,3}$')
# Env entry: KEY=value where value can be anything except newlines/null
_RX_ENV_ENTRY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}(=[^\n\x00]*)?$')

def _valid(rx, s):
    return isinstance(s, str) and bool(rx.match(s))


# ---------------------------------------------------------------------------
# Sensitive env masking
# ---------------------------------------------------------------------------

_RX_SENSITIVE_ENV = re.compile(
    r'(password|secret|token|apikey|api_key|jwt|bearer|auth|private|credential|dsn|passwd|passphrase)',
    re.IGNORECASE,
)

def _mask_env_list(envs, unmask=False):
    """Mask values of env entries whose KEY looks sensitive.
    Input/output: list of "KEY=value" strings."""
    if unmask or not envs:
        return envs
    out = []
    for e in envs:
        if not isinstance(e, str) or '=' not in e:
            out.append(e)
            continue
        key, _, _ = e.partition('=')
        if _RX_SENSITIVE_ENV.search(key):
            out.append(f'{key}=***')
        else:
            out.append(e)
    return out


def _is_admin():
    """Return True if current request user is admin. Returns False on any error."""
    try:
        from pegaprox.models.permissions import ROLE_ADMIN
        username = request.session.get('user', '')
        users = load_users()
        return users.get(username, {}).get('role') == ROLE_ADMIN
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SSH Helper
# ---------------------------------------------------------------------------

def _load_config():
    cfg_path = os.path.join(PLUGIN_DIR, 'config.json')
    try:
        with open(cfg_path) as f:
            return json.load(f)
    except Exception:
        return {"swarm_hosts": [], "poll_interval": 30}


def _save_config(cfg):
    cfg_path = os.path.join(PLUGIN_DIR, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(cfg, f, indent=4)


class _PersistentTOFUPolicy:
    """First-connect: auto-add the host key and persist it to known_hosts.
    Subsequent connections will find the key already in known_hosts; if the
    remote presents a *different* key, paramiko itself rejects (BadHostKeyException).
    This gives us defence in depth: real MITM after first contact gets caught."""
    def __init__(self, known_hosts_path):
        self._path = known_hosts_path

    def missing_host_key(self, client, hostname, key):
        try:
            client.get_host_keys().add(hostname, key.get_name(), key)
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            client.save_host_keys(self._path)
            try:
                os.chmod(self._path, 0o600)
            except Exception:
                pass
            log.info(f"[{PLUGIN_ID}] new host key persisted for {hostname} "
                     f"({key.get_name()} fp={key.get_fingerprint().hex()})")
        except Exception as e:
            log.warning(f"[{PLUGIN_ID}] could not persist host key for {hostname}: {e}")


# ---------------------------------------------------------------------------
# SSH connection pool (P1 — v1.10.0)
# ---------------------------------------------------------------------------
# Each Swarm host gets ONE long-lived paramiko.SSHClient kept alive with a 30s
# transport keepalive. Subsequent _ssh_exec calls open a fresh session on the
# existing transport (paramiko-safe) instead of reopening TCP+key-exchange.
# Pool entries are dropped & rebuilt automatically if the transport dies.

_ssh_pool = {}            # host_id -> {'client': SSHClient, 'last_used': ts}
_ssh_pool_lock = threading.Lock()
SSH_KEEPALIVE = 30        # seconds — paramiko sends keepalive ping
SSH_POOL_IDLE_MAX = 600   # 10min idle -> recycle on next use


def _ssh_pool_key(host_cfg):
    return f"{host_cfg.get('user', '')}@{host_cfg['host']}"


def _ssh_pool_close_all(reason='manual'):
    """Drop every pooled SSH connection. Call on config change or shutdown."""
    with _ssh_pool_lock:
        n = len(_ssh_pool)
        for entry in _ssh_pool.values():
            try:
                entry['client'].close()
            except Exception:
                pass
        _ssh_pool.clear()
    if n:
        log.info(f"[{PLUGIN_ID}] SSH pool closed ({n} entries, reason={reason})")


def _ssh_get_client(host_cfg, timeout=15):
    """Return a connected, healthy paramiko.SSHClient for host_cfg.
    Reuses a pooled connection when possible; reconnects transparently if dead.
    Raises on auth/network failure — caller handles."""
    import paramiko

    pkey = _ssh_pool_key(host_cfg)

    # Fast path: existing healthy connection.
    with _ssh_pool_lock:
        entry = _ssh_pool.get(pkey)
        if entry:
            client = entry['client']
            transport = client.get_transport()
            now = time.time()
            stale = (now - entry['last_used']) > SSH_POOL_IDLE_MAX
            if transport and transport.is_active() and not stale:
                entry['last_used'] = now
                return client
            # Drop dead/stale entry; we'll rebuild outside the lock.
            try:
                client.close()
            except Exception:
                pass
            _ssh_pool.pop(pkey, None)

    # Slow path: build a fresh connection.
    client = paramiko.SSHClient()
    if os.path.isfile(KNOWN_HOSTS_PATH):
        try:
            client.load_host_keys(KNOWN_HOSTS_PATH)
        except Exception as e:
            log.warning(f"[{PLUGIN_ID}] could not load known_hosts: {e}")
    client.set_missing_host_key_policy(_PersistentTOFUPolicy(KNOWN_HOSTS_PATH))

    connect_kwargs = dict(
        hostname=host_cfg['host'], port=22, username=host_cfg['user'],
        timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
    )
    key_file = host_cfg.get('key_file', '')
    password = host_cfg.get('password', '')
    if key_file and os.path.isfile(key_file):
        connect_kwargs['key_filename'] = key_file
        connect_kwargs['look_for_keys'] = False
        connect_kwargs['allow_agent'] = False
    elif password:
        connect_kwargs['password'] = password
        connect_kwargs['look_for_keys'] = False
        connect_kwargs['allow_agent'] = False
    else:
        try:
            client.close()
        except Exception:
            pass
        raise RuntimeError('No key_file or password configured')

    client.connect(**connect_kwargs)
    transport = client.get_transport()
    if transport:
        transport.set_keepalive(SSH_KEEPALIVE)

    # Race-safe pool insert: if another thread won the race, prefer ours and close
    # the stale entry to avoid leaking transports (last writer wins, but no leak).
    with _ssh_pool_lock:
        existing = _ssh_pool.get(pkey)
        if existing and existing['client'] is not client:
            try:
                existing['client'].close()
            except Exception:
                pass
        _ssh_pool[pkey] = {'client': client, 'last_used': time.time()}
    return client


def _ssh_exec(host_cfg_or_host, user=None, password=None, command='', timeout=15):
    """Execute command on remote host via SSH, return (stdout, stderr, exit_code).

    Uses the SSH connection pool — first call to a host opens TCP+key-exchange,
    subsequent calls reuse the same transport (paramiko opens a new channel per
    exec_command, which is the safe parallelism path).

    Accepts either:
      - A host config dict: {host, user, key_file?, password?}
      - Legacy positional args: (host, user, password, command)
    """
    import paramiko

    if isinstance(host_cfg_or_host, dict):
        h = host_cfg_or_host
    else:
        h = {
            'host': host_cfg_or_host, 'user': user or '',
            'password': password or '', 'key_file': '',
        }

    if not h.get('host') or not h.get('user'):
        return '', 'host and user required', -1
    if not h.get('key_file') and not h.get('password'):
        return '', 'No key_file or password configured', -1

    # 2 attempts: first uses pool, second forces fresh connection if pool entry was stale.
    last_err = ''
    for attempt in range(2):
        try:
            client = _ssh_get_client(h, timeout=timeout)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            return out, err, exit_code
        except paramiko.BadHostKeyException as e:
            log.error(f"[{PLUGIN_ID}] HOST KEY MISMATCH for {h['host']} — possible MITM: {e}")
            return '', f"Host key mismatch for {h['host']} (refusing to connect)", -1
        except (paramiko.SSHException, EOFError, OSError, ConnectionError) as e:
            # Transport may have died between pool fetch and exec — drop it and retry once.
            with _ssh_pool_lock:
                entry = _ssh_pool.pop(_ssh_pool_key(h), None)
                if entry:
                    try:
                        entry['client'].close()
                    except Exception:
                        pass
            last_err = str(e)
            if attempt == 0:
                continue
            return '', last_err, -1
        except Exception as e:
            return '', str(e), -1
    return '', last_err or 'unreachable', -1


def _docker_cmd(command, host_cfg=None):
    """Run a docker command on the first available Swarm manager.
    Returns parsed JSON or raw string."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    if host_cfg:
        hosts = [host_cfg]

    for h in hosts:
        out, err, code = _ssh_exec(h, command=command)
        if code == 0:
            return out.strip()
        log.warning(f"[{PLUGIN_ID}] Command failed on {h['host']}: {err.strip()}")

    return None


def _docker_json(command, host_cfg=None):
    """Run docker command expecting JSON output. Returns parsed data or None."""
    raw = _docker_cmd(command, host_cfg)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Docker --format '{{json .}}' outputs one JSON per line
        results = []
        for line in raw.strip().split('\n'):
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return results if results else None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry['ts']) < CACHE_TTL:
            return entry['data']
    return None


def _cache_set(key, data):
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}


# ---------------------------------------------------------------------------
# Data fetchers (used by API and background thread)
# ---------------------------------------------------------------------------

def _fetch_overview():
    """Fetch complete Swarm overview."""
    info = _docker_json('docker info --format "{{json .}}"')
    if not info:
        return {'error': 'Cannot connect to Docker Swarm'}

    nodes = _docker_json(
        'docker node ls --format "{{json .}}"'
    ) or []

    services = _docker_json(
        'docker service ls --format "{{json .}}"'
    ) or []

    # System-wide df
    df_raw = _docker_cmd('docker system df --format "{{json .}}"')
    disk_usage = []
    if df_raw:
        for line in df_raw.strip().split('\n'):
            try:
                disk_usage.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    overview = {
        'swarm': {
            'id': info.get('Swarm', {}).get('NodeID', ''),
            'cluster_id': info.get('Swarm', {}).get('Cluster', {}).get('ID', ''),
            'is_manager': info.get('Swarm', {}).get('ControlAvailable', False),
            'managers': info.get('Swarm', {}).get('Managers', 0),
            'nodes_count': info.get('Swarm', {}).get('Nodes', 0),
            'docker_version': info.get('ServerVersion', ''),
            'os': info.get('OperatingSystem', ''),
            'arch': info.get('Architecture', ''),
            'hostname': info.get('Name', ''),
            'kernel': info.get('KernelVersion', ''),
            'cpus': info.get('NCPU', 0),
            'memory_bytes': info.get('MemTotal', 0),
            'containers_running': info.get('ContainersRunning', 0),
            'containers_stopped': info.get('ContainersStopped', 0),
            'containers_paused': info.get('ContainersPaused', 0),
            'images': info.get('Images', 0),
        },
        'nodes': nodes,
        'services_count': len(services),
        'disk_usage': disk_usage,
        'updated_at': datetime.now().isoformat(),
    }
    return overview


def _fetch_nodes():
    """Fetch node details with resource usage (P2: 1 SSH call for batched inspect, was N+1)."""
    nodes_ls = _docker_json('docker node ls --format "{{json .}}"') or []
    if not nodes_ls:
        return []

    inspects_raw = _docker_cmd(
        'docker node ls -q | xargs -r -I{} docker node inspect {} --format "{{json .}}"'
    )
    inspects_by_id = {}
    if inspects_raw:
        for line in inspects_raw.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                nid = data.get('ID', '')
                if nid:
                    inspects_by_id[nid] = data

    detailed = []
    for node in nodes_ls:
        node_id = node.get('ID', '')
        inspect = inspects_by_id.get(node_id)
        resources = {}
        if inspect:
            res = inspect.get('Description', {}).get('Resources', {})
            resources = {
                'cpus': res.get('NanoCPUs', 0) / 1e9 if res.get('NanoCPUs') else 0,
                'memory_bytes': res.get('MemoryBytes', 0),
            }
            platform = inspect.get('Description', {}).get('Platform', {})
            engine = inspect.get('Description', {}).get('Engine', {})
            node['platform_os'] = platform.get('OS', '')
            node['platform_arch'] = platform.get('Architecture', '')
            node['engine_version'] = engine.get('EngineVersion', '')
            node['addr'] = inspect.get('Status', {}).get('Addr', '')
            node['state'] = inspect.get('Status', {}).get('State', '')
            node['message'] = inspect.get('Status', {}).get('Message', '')
        node['resources'] = resources
        detailed.append(node)
    return detailed


def _fetch_services():
    """Fetch all services with details (P2: 1 SSH call for batched inspect, was N+1).

    `service ls` gives the basic row + Replicas. A second call pipes all IDs through
    xargs to a batched `service inspect`, returning one JSON-per-line. We then merge
    the inspect data into the ls rows in O(1) lookups by ID/name.
    """
    services_ls = _docker_json('docker service ls --format "{{json .}}"') or []
    if not services_ls:
        return []

    # Batched inspect — one SSH call returns all service inspect docs.
    # `-r` so xargs is silent on empty stdin; quoting wraps each ID safely.
    inspects_raw = _docker_cmd(
        'docker service ls -q | xargs -r -I{} docker service inspect {} --format "{{json .}}"'
    )
    inspects_by_id = {}
    inspects_by_name = {}
    if inspects_raw:
        for line in inspects_raw.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            svc_id = data.get('ID', '')
            if svc_id:
                inspects_by_id[svc_id] = data
            name = data.get('Spec', {}).get('Name', '')
            if name:
                inspects_by_name[name] = data

    detailed = []
    for svc in services_ls:
        svc_id = svc.get('ID', '')
        svc_name = svc.get('Name', '')
        inspect = inspects_by_id.get(svc_id) or inspects_by_name.get(svc_name)
        if inspect:
            spec = inspect.get('Spec', {})
            task_tmpl = spec.get('TaskTemplate', {})
            container_spec = task_tmpl.get('ContainerSpec', {})
            resources_spec = task_tmpl.get('Resources', {})
            placement = task_tmpl.get('Placement', {})
            endpoint = inspect.get('Endpoint', {})
            mode = spec.get('Mode', {})

            svc['image_full'] = container_spec.get('Image', '')
            svc['env'] = len(container_spec.get('Env', []))
            svc['mounts'] = len(container_spec.get('Mounts', []))
            svc['constraints'] = placement.get('Constraints', [])
            svc['labels'] = spec.get('Labels', {})
            svc['ports_detail'] = endpoint.get('Ports', [])
            svc['vip'] = [v.get('Addr', '') for v in endpoint.get('VirtualIPs', [])]
            svc['resources_limits'] = resources_spec.get('Limits', {})
            svc['resources_reservations'] = resources_spec.get('Reservations', {})
            svc['created'] = inspect.get('CreatedAt', '')
            svc['updated'] = inspect.get('UpdatedAt', '')
            svc['update_status'] = inspect.get('UpdateStatus', {})
            # Audit fields (v1.11.0 — Policy Auditor)
            svc['placement_preferences'] = placement.get('Preferences', []) or []
            svc['placement_max_replicas'] = placement.get('MaxReplicas', 0) or 0
            svc['restart_policy'] = task_tmpl.get('RestartPolicy', {}) or {}
            svc['healthcheck'] = container_spec.get('Healthcheck', {}) or {}
            svc['update_config'] = spec.get('UpdateConfig', {}) or {}
            svc['rollback_config'] = spec.get('RollbackConfig', {}) or {}

            if 'Replicated' in mode:
                svc['mode_type'] = 'replicated'
                svc['replicas_spec'] = mode['Replicated'].get('Replicas', 0)
            elif 'Global' in mode:
                svc['mode_type'] = 'global'
                svc['replicas_spec'] = 'global'
            else:
                svc['mode_type'] = 'unknown'

            svc['stack'] = svc['labels'].get('com.docker.stack.namespace', '')
        detailed.append(svc)
    return detailed


def _fetch_stacks():
    """Fetch stacks derived from services (P2: 0 SSH calls — consumes services cache).

    Stack membership lives in the `com.docker.stack.namespace` label which
    `_fetch_services` already collects. We reuse its cache when populated; on a
    cold cache (first poll) we fall through to a fresh fetch. Either way, this
    function adds zero new SSH calls per cycle once warmed up — was N+1 before.
    """
    services = _cache_get('services')
    if services is None:
        services = _fetch_services()

    stack_map = {}
    for svc in services or []:
        replicas = svc.get('Replicas', '0/0')
        parts = replicas.split('/')
        running = int(parts[0]) if parts[0].isdigit() else 0
        desired = int(parts[-1]) if parts[-1].isdigit() else 0

        # Stack namespace populated by _fetch_services from spec.Labels
        ns = svc.get('stack') or svc.get('labels', {}).get('com.docker.stack.namespace', '')
        if not ns:
            continue

        if ns not in stack_map:
            stack_map[ns] = {
                'Name': ns, 'Services': 0,
                'running': 0, 'desired': 0, 'svc_running': 0, 'svc_total': 0
            }
        stack_map[ns]['Services'] += 1
        stack_map[ns]['svc_total'] += 1
        stack_map[ns]['running'] += running
        stack_map[ns]['desired'] += desired
        if running > 0:
            stack_map[ns]['svc_running'] += 1

    for s in stack_map.values():
        if s['desired'] == 0:
            s['status'] = 'stopped'
        elif s['running'] == s['desired']:
            s['status'] = 'running'
        elif s['running'] > 0:
            s['status'] = 'partial'
        else:
            s['status'] = 'stopped'
    return list(stack_map.values())


def _fetch_containers():
    """Fetch containers from ALL swarm nodes via SSH."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    all_containers = []

    for h in hosts:
        raw = None
        out, err, code = _ssh_exec(
            h, command='docker ps -a --format "{{json .}}" --no-trunc'
        )
        if code == 0 and out.strip():
            for line in out.strip().split('\n'):
                line = line.strip()
                if line:
                    try:
                        c = json.loads(line)
                        c['_node'] = h.get('name', h['host'])
                        c['_host'] = h['host']
                        all_containers.append(c)
                    except json.JSONDecodeError:
                        pass

    return all_containers


def _fetch_tasks(service_id):
    """Fetch tasks for a specific service. Caller MUST validate service_id."""
    tasks = _docker_json(
        f'docker service ps {shlex.quote(service_id)} --format "{{{{json .}}}}" --no-trunc'
    ) or []
    return tasks


def _fetch_networks():
    """Fetch Docker networks from ALL nodes with IPAM details."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    all_nets = []
    seen = set()

    for h in hosts:
        out, err, code = _ssh_exec(
            h, command='docker network ls -q | xargs -I{} docker network inspect {} --format "{{json .}}" 2>/dev/null'
        )
        if code == 0 and out.strip():
            for line in out.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    n = json.loads(line)
                    nid = n.get('Id', '')
                    if nid in seen:
                        continue
                    seen.add(nid)
                    ipam = n.get('IPAM', {}).get('Config', [{}])
                    ipam0 = ipam[0] if ipam else {}
                    labels = n.get('Labels', {})
                    all_nets.append({
                        'Name': n.get('Name', ''),
                        'ID': nid[:12],
                        'Driver': n.get('Driver', ''),
                        'Scope': n.get('Scope', ''),
                        'Attachable': n.get('Attachable', False),
                        'Internal': n.get('Internal', False),
                        'IPAM_Driver': n.get('IPAM', {}).get('Driver', 'default'),
                        'Subnet': ipam0.get('Subnet', ''),
                        'Gateway': ipam0.get('Gateway', ''),
                        'IPRange': ipam0.get('IPRange', ''),
                        'Stack': labels.get('com.docker.stack.namespace', ''),
                        'System': n.get('Name', '') in ('bridge', 'host', 'none', 'ingress', 'docker_gwbridge'),
                        '_node': h.get('name', h['host']),
                    })
                except json.JSONDecodeError:
                    pass
    return all_nets


def _fetch_volumes():
    """Fetch Docker volumes from ALL nodes with details."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    all_vols = []

    for h in hosts:
        out, err, code = _ssh_exec(
            h, command='docker volume ls -q | xargs -I{} docker volume inspect {} --format "{{json .}}" 2>/dev/null'
        )
        if code == 0 and out.strip():
            for line in out.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    v = json.loads(line)
                    labels = v.get('Labels', {}) or {}
                    all_vols.append({
                        'Name': v.get('Name', ''),
                        'Driver': v.get('Driver', ''),
                        'Scope': v.get('Scope', ''),
                        'Mountpoint': v.get('Mountpoint', ''),
                        'CreatedAt': v.get('CreatedAt', ''),
                        'Stack': labels.get('com.docker.stack.namespace', ''),
                        'Labels': labels,
                        '_node': h.get('name', h['host']),
                        '_host': h['host'],
                    })
                except json.JSONDecodeError:
                    pass
    return all_vols


def _fetch_images():
    """Fetch Docker images from ALL nodes."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    all_imgs = []

    for h in hosts:
        out, err, code = _ssh_exec(
            h, command='docker image ls --format "{{json .}}" --no-trunc'
        )
        if code == 0 and out.strip():
            for line in out.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    img = json.loads(line)
                    img['_node'] = h.get('name', h['host'])
                    img['_host'] = h['host']
                    # Check if in use
                    all_imgs.append(img)
                except json.JSONDecodeError:
                    pass
    return all_imgs


# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

def _bg_poll_once():
    """Run a single poll cycle.
    Phase 1 (parallel): overview, nodes, services — independent.
    Phase 2 (after services finishes): stacks — derives from services cache.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    phase1 = {
        'overview': _fetch_overview,
        'nodes': _fetch_nodes,
        'services': _fetch_services,
    }
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='swarm-fetch') as pool:
        futures = {pool.submit(fn): key for key, fn in phase1.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                _cache_set(key, future.result())
            except Exception as e:
                log.error(f"[{PLUGIN_ID}] Fetch {key} failed: {e}")

    # Phase 2 — stacks consumes the just-cached services list (zero new SSH calls).
    try:
        _cache_set('stacks', _fetch_stacks())
    except Exception as e:
        log.error(f"[{PLUGIN_ID}] Fetch stacks failed: {e}")

    # Phase 3 — metrics sample (v1.13.0). Calls _api_load_balance for its side
    # effect of writing one row per node into the metrics SQLite ring buffer.
    # Runs every poll cycle so we get continuous samples even when no UI is open.
    try:
        _api_load_balance()
    except Exception as e:
        log.error(f"[{PLUGIN_ID}] Metrics sample failed: {e}")


def _bg_poll():
    """Background thread that refreshes cache periodically + runs disk auto-prune.
    poll_interval is re-read every iteration so Settings changes take effect live."""
    log.info(f"[{PLUGIN_ID}] Background poll started")

    last_disk_check = 0
    while not _bg_stop.is_set():
        cfg_now = _load_config()
        interval = max(5, int(cfg_now.get('poll_interval', 30)))

        try:
            _bg_poll_once()
        except Exception as e:
            log.error(f"[{PLUGIN_ID}] Background poll error: {e}")

        # Disk auto-prune check según intervalo configurable
        try:
            auto = cfg_now.get('disk_auto_prune', {})
            if auto.get('enabled'):
                check_min = auto.get('check_interval_min', 30)
                now_ts = time.time()
                if now_ts - last_disk_check >= check_min * 60:
                    _disk_auto_prune_tick()
                    last_disk_check = now_ts
        except Exception as e:
            log.error(f"[{PLUGIN_ID}] Auto-prune tick error: {e}")

        _bg_stop.wait(interval)


# ---------------------------------------------------------------------------
# Policy Auditor (v1.11.0 — Phase 1)
# ---------------------------------------------------------------------------
# Audits each Swarm service against best-practice policies and grades A-F.
# Read-only: never mutates services. The auditor surfaces findings; humans (or
# a future Applier in Phase 2) decide whether to apply the suggested fixes.
#
# Severity:
#   P0 — critical: outage almost certain (no spread + replicas>1, replicas>nodes)
#   P1 — important: degrades availability or scheduler quality
#   P2 — recommended: best practices that prevent surprises (no limits, :latest)
#   P3 — nice to have: polish (no healthcheck, no rollback config)
#
# Aggregate grade per service:
#   F — at least one P0 fail
#   D — 0 P0 fails (still "running"); collects ≥3 P1 fails
#   C — 0 P0 fails, ≤2 P1 fails
#   B — 0 P0 fails, ≤1 P1 fail, ≤2 P2 fails
#   A — 0 P0 fails, 0 P1 fails, ≤1 P2 fail (P3 doesn't gate the grade)

POLICY_CHECKS = [
    {
        'id': 'replicas_vs_nodes',
        'severity': 'P0',
        'title': 'Réplicas no exceden nodos sanos',
        'description': 'replicas > nodos sanos significa que algunas réplicas nunca se schedulearán.',
    },
    {
        'id': 'anti_affinity',
        'severity': 'P0',
        'title': 'Anti-afinidad configurada para multi-réplica',
        'description': 'Con replicas>1, sin spread ni max_replicas_per_node todas pueden caer en un solo nodo.',
    },
    {
        'id': 'resource_reservations',
        'severity': 'P1',
        'title': 'Reservations de CPU y memoria definidas',
        'description': 'Sin reservations el scheduler no puede planificar capacidad — colocaciones a ciegas.',
    },
    {
        'id': 'single_replica_risk',
        'severity': 'P1',
        'title': 'Servicio replicated con >1 réplica para HA',
        'description': 'Una sola réplica = downtime durante updates/fallos. Aceptable solo para singletons.',
    },
    {
        'id': 'resource_limits',
        'severity': 'P2',
        'title': 'Limits de CPU y memoria definidas',
        'description': 'Sin limits un servicio puede consumir un nodo entero y matar a sus vecinos.',
    },
    {
        'id': 'restart_policy',
        'severity': 'P2',
        'title': 'Política de restart distinta a none',
        'description': 'Restart=none deja al servicio caído ante cualquier fallo transitorio.',
    },
    {
        'id': 'image_pinning',
        'severity': 'P2',
        'title': 'Imagen anclada (no :latest sin digest)',
        'description': ':latest es mutable — un docker pull asincrónico puede romperte el rolling update.',
    },
    {
        'id': 'healthcheck',
        'severity': 'P3',
        'title': 'Healthcheck definido',
        'description': 'Sin healthcheck Swarm no detecta degradación parcial — solo crashes duros.',
    },
    {
        'id': 'update_rollback',
        'severity': 'P3',
        'title': 'Auto-rollback en updates',
        'description': 'update_config.failure_action=rollback evita que un mal deploy te deje caído.',
    },
    {
        'id': 'update_parallelism',
        'severity': 'P3',
        'title': 'update_config.parallelism explícito',
        'description': 'parallelism=1 (con orden start-first) garantiza que siempre hay réplicas vivas durante el update.',
    },
]

# Heurística para detectar singletons donde replicas=1 es legítimo:
#   - DBs primary (postgres/patroni con role=primary), redis sentinel master, etcd leader
#   - El usuario marcó label `singleton=true` o nombre matchea patrones clásicos.
_SINGLETON_NAME_PATTERNS = re.compile(
    r'(patroni\d+|postgres|mysql|mariadb|etcd\d+|sentinel|leader|primary|master)$',
    re.IGNORECASE,
)

def _is_singleton_service(svc):
    """Best-effort: si parece un singleton (DB primary, etcd leader, etc.) no penalizar replicas=1."""
    labels = svc.get('labels', {}) or {}
    if str(labels.get('singleton', '')).lower() in ('true', '1', 'yes'):
        return True
    name = svc.get('Name', '') or ''
    return bool(_SINGLETON_NAME_PATTERNS.search(name))


def _has_spread(svc):
    """Has any placement.preferences entry of type Spread (over node.id, hostname, etc.)."""
    prefs = svc.get('placement_preferences', []) or []
    for p in prefs:
        if isinstance(p, dict) and p.get('Spread'):
            return True
    return False


def _check_replicas_vs_nodes(svc, ctx):
    """Replicas can only be 'unschedulable' when max_replicas_per_node forbids
    stacking — Swarm by default allows multiple replicas on the same node.
    So this check fails only when max_replicas_per_node × healthy_nodes
    cannot accommodate the requested replicas. Without max_per_node, the
    spread preference handles distribution and replicas > nodes is fine.
    """
    if svc.get('mode_type') != 'replicated':
        return {'status': 'skip', 'message': 'Servicio en modo global o non-replicated.'}
    replicas = svc.get('replicas_spec', 0) or 0
    healthy = ctx.get('healthy_nodes', 0)
    if replicas <= 0:
        return {'status': 'skip', 'message': 'Replicas=0 (servicio detenido).'}
    if healthy <= 0:
        return {'status': 'skip', 'message': 'No hay nodos sanos detectados — no se puede evaluar.'}
    max_per_node = svc.get('placement_max_replicas', 0) or 0
    if max_per_node:
        capacity = max_per_node * healthy
        if replicas > capacity:
            return {
                'status': 'fail',
                'message': (
                    f'replicas={replicas} > capacidad={capacity} '
                    f'(max_replicas_per_node={max_per_node} × {healthy} nodos sanos). '
                    f'{replicas - capacity} réplica(s) nunca podrán schedulearse.'
                ),
                'fix_hint': (
                    f'docker service scale {svc.get("Name")}={capacity}  '
                    f'# o subí max_replicas_per_node, o añadí nodos al cluster'
                ),
            }
        return {'status': 'pass', 'message': f'replicas={replicas} ≤ capacidad={capacity}.'}
    # Sin max_replicas_per_node: Swarm puede stackear múltiples réplicas por
    # nodo, así que replicas > nodos no impide schedulear. Pasa.
    return {
        'status': 'pass',
        'message': (
            f'replicas={replicas}, nodos sanos={healthy}. Sin max_replicas_per_node, '
            f'Swarm permite stackear — siempre schedulea.'
        ),
    }


def _check_anti_affinity(svc, ctx):
    if svc.get('mode_type') != 'replicated':
        return {'status': 'skip', 'message': 'Servicio en modo global (anti-afinidad implícita).'}
    replicas = svc.get('replicas_spec', 0) or 0
    if replicas <= 1:
        return {'status': 'skip', 'message': 'replicas≤1 — anti-afinidad N/A.'}
    has_spread = _has_spread(svc)
    max_per_node = svc.get('placement_max_replicas', 0) or 0
    if has_spread and max_per_node:
        return {
            'status': 'pass',
            'message': f'spread + max_replicas_per_node={max_per_node}.',
        }
    if has_spread:
        return {
            'status': 'pass',
            'message': 'placement.preferences con Spread configurado.',
        }
    if max_per_node and max_per_node < replicas:
        return {
            'status': 'pass',
            'message': f'max_replicas_per_node={max_per_node} < replicas={replicas}.',
        }
    return {
        'status': 'fail',
        'message': f'replicas={replicas} sin spread ni max_replicas_per_node — todas pueden caer en un solo nodo.',
        'fix_hint': (
            'Recomendado en compose: deploy.placement.preferences:\n'
            '  - spread: node.id\n'
            'O bien: deploy.placement.max_replicas_per_node: 1'
        ),
    }


def _check_resource_reservations(svc, ctx):
    res = svc.get('resources_reservations', {}) or {}
    cpu = res.get('NanoCPUs', 0) or 0
    mem = res.get('MemoryBytes', 0) or 0
    if cpu and mem:
        return {'status': 'pass', 'message': f'cpu={cpu/1e9:.2f}, mem={mem/(1024*1024):.0f}MiB.'}
    if cpu or mem:
        return {
            'status': 'warn',
            'message': 'Solo un recurso reservado — definí ambos para que el scheduler planifique bien.',
            'fix_hint': 'deploy.resources.reservations: { cpus: "0.25", memory: 256M }',
        }
    return {
        'status': 'fail',
        'message': 'Sin reservations — scheduler coloca a ciegas.',
        'fix_hint': 'deploy.resources.reservations: { cpus: "0.25", memory: 256M }',
    }


def _check_resource_limits(svc, ctx):
    lim = svc.get('resources_limits', {}) or {}
    cpu = lim.get('NanoCPUs', 0) or 0
    mem = lim.get('MemoryBytes', 0) or 0
    if cpu and mem:
        return {'status': 'pass', 'message': f'cpu={cpu/1e9:.2f}, mem={mem/(1024*1024):.0f}MiB.'}
    if cpu or mem:
        return {
            'status': 'warn',
            'message': 'Solo un límite definido — definí ambos para evitar starvation cruzado.',
            'fix_hint': 'deploy.resources.limits: { cpus: "1.0", memory: 512M }',
        }
    return {
        'status': 'fail',
        'message': 'Sin limits — un servicio mal portado puede tirar el nodo entero.',
        'fix_hint': 'deploy.resources.limits: { cpus: "1.0", memory: 512M }',
    }


def _check_restart_policy(svc, ctx):
    rp = svc.get('restart_policy', {}) or {}
    cond = (rp.get('Condition', '') or '').lower()
    if cond in ('any', 'on-failure'):
        max_attempts = rp.get('MaxAttempts', 0)
        if max_attempts and max_attempts < 3:
            return {
                'status': 'warn',
                'message': f'Restart={cond} con max_attempts={max_attempts} — tolerancia muy baja.',
                'fix_hint': 'restart_policy: { condition: any, max_attempts: 0 }  # 0 = ilimitado',
            }
        return {'status': 'pass', 'message': f'Restart={cond}.'}
    if cond == 'none':
        return {
            'status': 'fail',
            'message': 'Restart=none — un crash deja al servicio caído permanentemente.',
            'fix_hint': 'restart_policy: { condition: any }',
        }
    return {
        'status': 'warn',
        'message': f'Restart condition vacía o desconocida ({cond or "none"}).',
        'fix_hint': 'restart_policy: { condition: any }',
    }


def _check_image_pinning(svc, ctx):
    img = svc.get('image_full', '') or svc.get('Image', '') or ''
    if not img:
        return {'status': 'skip', 'message': 'Sin imagen detectada.'}
    has_digest = '@sha256:' in img
    if has_digest:
        return {'status': 'pass', 'message': 'Imagen anclada con digest sha256.'}
    # Strip digest if present then check tag
    tag_part = img.rsplit('@', 1)[0]
    if ':' not in tag_part.rsplit('/', 1)[-1]:
        return {
            'status': 'fail',
            'message': f'{img} sin tag — implícitamente :latest.',
            'fix_hint': 'Usá tags inmutables (versión semver o digest sha256).',
        }
    tag = tag_part.rsplit(':', 1)[-1].lower()
    if tag in ('latest', 'main', 'master', 'develop', 'dev', 'edge'):
        return {
            'status': 'fail',
            'message': f'Tag mutable :{tag} — un pull puede traer una imagen distinta sin avisar.',
            'fix_hint': 'Usá un tag versionado (v1.2.3) o digest sha256.',
        }
    return {'status': 'pass', 'message': f'Tag inmutable :{tag}.'}


def _check_single_replica_risk(svc, ctx):
    if svc.get('mode_type') != 'replicated':
        return {'status': 'skip', 'message': 'Servicio global — no aplica.'}
    replicas = svc.get('replicas_spec', 0) or 0
    if replicas == 0:
        return {'status': 'skip', 'message': 'Servicio detenido.'}
    if replicas == 1:
        if _is_singleton_service(svc):
            return {
                'status': 'pass',
                'message': 'replicas=1 aceptable (singleton detectado por nombre/label).',
            }
        return {
            'status': 'fail',
            'message': 'replicas=1 — no hay HA. Updates causan downtime.',
            'fix_hint': f'docker service scale {svc.get("Name")}=2  (o añadir label singleton=true si es intencional)',
        }
    return {'status': 'pass', 'message': f'replicas={replicas}.'}


def _check_healthcheck(svc, ctx):
    hc = svc.get('healthcheck', {}) or {}
    test = hc.get('Test', []) or []
    if not test:
        return {
            'status': 'fail',
            'message': 'Sin healthcheck — Swarm solo detecta crashes, no degradación.',
            'fix_hint': 'healthcheck: { test: ["CMD", "curl", "-f", "http://localhost/health"], interval: 30s, retries: 3 }',
        }
    if isinstance(test, list) and test[0] == 'NONE':
        return {
            'status': 'fail',
            'message': 'Healthcheck explícitamente desactivado (NONE).',
            'fix_hint': 'Remové "test: NONE" o reemplazá por un comando real.',
        }
    return {'status': 'pass', 'message': 'Healthcheck activo.'}


def _check_update_rollback(svc, ctx):
    uc = svc.get('update_config', {}) or {}
    fa = (uc.get('FailureAction', '') or '').lower()
    if fa == 'rollback':
        return {'status': 'pass', 'message': 'failure_action=rollback.'}
    if fa in ('continue', 'pause', ''):
        return {
            'status': 'fail',
            'message': f'failure_action={fa or "default"} — un mal deploy te deja caído sin auto-rollback.',
            'fix_hint': 'update_config: { failure_action: rollback, monitor: 30s }',
        }
    return {'status': 'warn', 'message': f'failure_action={fa}.'}


def _check_update_parallelism(svc, ctx):
    uc = svc.get('update_config', {}) or {}
    par = uc.get('Parallelism', None)
    order = (uc.get('Order', '') or '').lower()
    replicas = svc.get('replicas_spec', 0) or 0
    if par is None:
        return {
            'status': 'fail',
            'message': 'parallelism no definido — Swarm usa default (1) pero implícito es frágil.',
            'fix_hint': 'update_config: { parallelism: 1, order: start-first }',
        }
    if isinstance(replicas, int) and replicas >= 2 and par >= replicas:
        return {
            'status': 'fail',
            'message': f'parallelism={par} ≥ replicas={replicas} — actualizás todo a la vez = downtime.',
            'fix_hint': 'update_config: { parallelism: 1, order: start-first }',
        }
    if order != 'start-first':
        return {
            'status': 'warn',
            'message': f'order={order or "stop-first"} — start-first prefiere disponibilidad sobre velocidad.',
            'fix_hint': 'update_config: { order: start-first }',
        }
    return {'status': 'pass', 'message': f'parallelism={par}, order=start-first.'}


_CHECK_FUNCS = {
    'replicas_vs_nodes': _check_replicas_vs_nodes,
    'anti_affinity': _check_anti_affinity,
    'resource_reservations': _check_resource_reservations,
    'single_replica_risk': _check_single_replica_risk,
    'resource_limits': _check_resource_limits,
    'restart_policy': _check_restart_policy,
    'image_pinning': _check_image_pinning,
    'healthcheck': _check_healthcheck,
    'update_rollback': _check_update_rollback,
    'update_parallelism': _check_update_parallelism,
}


def _audit_service(svc, ctx):
    """Run every check against one service. Return findings + grade."""
    results = []
    for meta in POLICY_CHECKS:
        cid = meta['id']
        fn = _CHECK_FUNCS.get(cid)
        if not fn:
            continue
        try:
            r = fn(svc, ctx) or {}
        except Exception as e:
            r = {'status': 'skip', 'message': f'Error en check: {e}'}
        results.append({
            'id': cid,
            'severity': meta['severity'],
            'title': meta['title'],
            'status': r.get('status', 'skip'),
            'message': r.get('message', ''),
            'fix_hint': r.get('fix_hint', ''),
        })
    grade = _grade_from_findings(results)
    return {
        'service_id': svc.get('ID', ''),
        'service_name': svc.get('Name', ''),
        'stack': svc.get('stack', ''),
        'replicas_spec': svc.get('replicas_spec', 0),
        'mode_type': svc.get('mode_type', ''),
        'image': svc.get('image_full', ''),
        'grade': grade,
        'findings': results,
        'summary': _summarize_findings(results),
    }


def _summarize_findings(findings):
    counts = {'pass': 0, 'fail': 0, 'warn': 0, 'skip': 0}
    by_sev = {'P0': 0, 'P1': 0, 'P2': 0, 'P3': 0}  # fails+warns por severidad
    for f in findings:
        counts[f['status']] = counts.get(f['status'], 0) + 1
        if f['status'] in ('fail', 'warn'):
            by_sev[f['severity']] = by_sev.get(f['severity'], 0) + 1
    return {'by_status': counts, 'issues_by_severity': by_sev}


def _grade_from_findings(findings):
    p0 = p1 = p2 = 0
    for f in findings:
        if f['status'] != 'fail':
            continue
        if f['severity'] == 'P0':
            p0 += 1
        elif f['severity'] == 'P1':
            p1 += 1
        elif f['severity'] == 'P2':
            p2 += 1
    if p0 >= 1:
        return 'F'
    if p1 >= 3:
        return 'D'
    if p1 >= 2:
        return 'C'
    if p1 >= 1 or p2 >= 3:
        return 'B' if p1 == 0 else 'C'
    if p2 >= 2:
        return 'B'
    if p2 >= 1:
        return 'B'
    return 'A'


_GRADE_RANK = {'A': 4, 'B': 3, 'C': 2, 'D': 1, 'F': 0}
_RANK_GRADE = {v: k for k, v in _GRADE_RANK.items()}


def _cluster_grade(audits):
    if not audits:
        return 'A', 0.0
    ranks = [_GRADE_RANK.get(a['grade'], 0) for a in audits]
    avg = sum(ranks) / len(ranks)
    # Floor to letter
    return _RANK_GRADE.get(int(avg), 'F'), round(avg, 2)


def _run_cluster_audit(service_filter=None):
    """Run full cluster audit. Optionally filter to a single service by name."""
    services = _cache_get('services')
    if services is None:
        services = _fetch_services()

    # Healthy node count (active + ready) for replicas/anti-affinity checks
    nodes = _cache_get('nodes')
    if nodes is None:
        nodes = _fetch_nodes()
    healthy = 0
    for n in nodes or []:
        avail = (n.get('Availability', '') or '').lower()
        status = (n.get('Status', '') or '').lower()
        if avail == 'active' and status == 'ready':
            healthy += 1

    ctx = {'healthy_nodes': healthy, 'total_nodes': len(nodes or [])}

    audits = []
    for svc in services or []:
        if service_filter and svc.get('Name') != service_filter:
            continue
        audits.append(_audit_service(svc, ctx))

    cluster_grade, avg_rank = _cluster_grade(audits)

    # Distribution of grades + worst offenders
    grade_dist = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    for a in audits:
        grade_dist[a['grade']] = grade_dist.get(a['grade'], 0) + 1
    worst = sorted(
        audits,
        key=lambda a: (
            _GRADE_RANK.get(a['grade'], 0),
            -a['summary']['issues_by_severity']['P0'],
            -a['summary']['issues_by_severity']['P1'],
        ),
    )[:10]

    return {
        'cluster_grade': cluster_grade,
        'avg_rank': avg_rank,
        'healthy_nodes': healthy,
        'total_nodes': ctx['total_nodes'],
        'service_count': len(audits),
        'grade_distribution': grade_dist,
        'worst_offenders': [{
            'service_name': a['service_name'],
            'stack': a['stack'],
            'grade': a['grade'],
            'p0_issues': a['summary']['issues_by_severity']['P0'],
            'p1_issues': a['summary']['issues_by_severity']['P1'],
        } for a in worst if a['grade'] != 'A'],
        'audits': audits,
        'updated_at': datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Policy Applier (v1.12.0 — Phase 2)
# ---------------------------------------------------------------------------
# Read-only auditor was Phase 1. Applier turns each finding's fix_hint into a
# one-click `docker service update`. Admin-gated. Dry-run by default — must
# pass confirm=true to actually mutate. Audited via log_audit on every call.
#
# Only 4 of the 10 checks have programmatic fixes. The rest need workload-
# specific input (resource sizing, healthcheck command, replica count) and
# stay manual — the UI surfaces fix_hint but no apply button.

# Each applier is a function svc -> command-string. `applies_when` is a hook
# that re-validates the precondition at apply time (race-safe — service may
# have changed between audit and apply).

def _applier_anti_affinity(svc):
    name = svc.get('Name', '')
    if not _valid(_RX_DOCKER_REF, name):
        raise ValueError(f'Invalid service name: {name!r}')
    return f"docker service update --placement-pref-add 'spread=node.id' {shlex.quote(name)}"


def _applier_restart_policy(svc):
    name = svc.get('Name', '')
    if not _valid(_RX_DOCKER_REF, name):
        raise ValueError(f'Invalid service name: {name!r}')
    return f"docker service update --restart-condition any {shlex.quote(name)}"


def _applier_update_rollback(svc):
    name = svc.get('Name', '')
    if not _valid(_RX_DOCKER_REF, name):
        raise ValueError(f'Invalid service name: {name!r}')
    return (
        f"docker service update --update-failure-action rollback "
        f"--update-monitor 30s {shlex.quote(name)}"
    )


def _applier_update_parallelism(svc):
    name = svc.get('Name', '')
    if not _valid(_RX_DOCKER_REF, name):
        raise ValueError(f'Invalid service name: {name!r}')
    return (
        f"docker service update --update-parallelism 1 "
        f"--update-order start-first {shlex.quote(name)}"
    )


POLICY_APPLIERS = {
    'anti_affinity': {
        'description': 'Añadir placement.preferences: spread=node.id',
        'apply': _applier_anti_affinity,
        'severity': 'P0',
    },
    'restart_policy': {
        'description': 'Cambiar RestartPolicy.Condition a "any"',
        'apply': _applier_restart_policy,
        'severity': 'P2',
    },
    'update_rollback': {
        'description': 'Setear UpdateConfig.FailureAction=rollback + Monitor=30s',
        'apply': _applier_update_rollback,
        'severity': 'P3',
    },
    'update_parallelism': {
        'description': 'Setear UpdateConfig.Parallelism=1 + Order=start-first',
        'apply': _applier_update_parallelism,
        'severity': 'P3',
    },
}


def _is_check_applicable(svc, check_id):
    """Re-evaluate the check at apply time. Only 'fail' status warrants action."""
    fn = _CHECK_FUNCS.get(check_id)
    if not fn:
        return False, 'check id desconocido'
    try:
        # ctx for re-eval — we don't really need healthy_nodes for the 4
        # auto-fixers (none of them are gated by node count), but the
        # check signature requires ctx. Fetch nodes lazily.
        nodes = _cache_get('nodes') or _fetch_nodes() or []
        healthy = sum(
            1 for n in nodes
            if (n.get('Availability', '') or '').lower() == 'active'
            and (n.get('Status', '') or '').lower() == 'ready'
        )
        result = fn(svc, {'healthy_nodes': healthy, 'total_nodes': len(nodes)}) or {}
    except Exception as e:
        return False, f'error re-evaluando: {e}'
    status = result.get('status', 'skip')
    if status == 'fail':
        return True, result.get('message', '')
    return False, f'check ya no falla (status={status}): {result.get("message", "")}'


def _api_policy_apply():
    """POST — Apply an auto-fix for one (service, check_id) pair.

    Body: {service_name: str, check_id: str, confirm: bool}
      confirm=false (default) → dry-run: returns the command but does not run.
      confirm=true            → actually executes; admin required.

    Always re-validates that the check still fails before executing,
    so a stale UI cannot apply a fix that's no longer needed.
    """
    err = _require_admin()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    service_name = (payload.get('service_name') or '').strip()
    check_id = (payload.get('check_id') or '').strip()
    confirm = bool(payload.get('confirm', False))

    if not _valid(_RX_DOCKER_REF, service_name):
        return {'error': 'Invalid service_name'}, 400
    if not check_id or check_id not in POLICY_APPLIERS:
        return {
            'error': f'Unknown or non-fixable check_id. Auto-fixable: '
                     f'{sorted(POLICY_APPLIERS.keys())}',
        }, 400

    services = _fetch_services() or []
    svc = next((s for s in services if s.get('Name') == service_name), None)
    if not svc:
        return {'error': f'Service not found: {service_name}'}, 404

    applicable, why = _is_check_applicable(svc, check_id)
    if not applicable:
        return {
            'service': service_name,
            'check_id': check_id,
            'applicable': False,
            'reason': why,
        }, 200

    applier = POLICY_APPLIERS[check_id]
    try:
        command = applier['apply'](svc)
    except ValueError as e:
        return {'error': str(e)}, 400

    if not confirm:
        # Dry-run: report the command we would run.
        return {
            'service': service_name,
            'check_id': check_id,
            'applicable': True,
            'reason': why,
            'description': applier['description'],
            'command': command,
            'dry_run': True,
            'applied': False,
        }

    # Real run: execute via SSH on a Swarm manager.
    out, errstr, rc = _ssh_exec(
        _load_config().get('swarm_hosts', [{}])[0],
        command=command,
        timeout=60,
    )

    success = rc == 0
    audit_data = {
        'service': service_name,
        'check_id': check_id,
        'severity': applier['severity'],
        'description': applier['description'],
        'command': command,
        'success': success,
        'rc': rc,
        'out_preview': (out or '')[:200],
        'err_preview': (errstr or '')[:200],
    }
    try:
        log_audit('policy_apply', audit_data, _get_username())
    except Exception:
        pass  # Audit should never block the response

    # Invalidate cache so next audit reflects the change
    _invalidate('services')
    with _cache_lock:
        _cache.pop(f'audit:', None)
        _cache.pop(f'audit:all', None)
        _cache.pop(f'audit:{service_name}', None)

    return {
        'service': service_name,
        'check_id': check_id,
        'applicable': True,
        'description': applier['description'],
        'command': command,
        'dry_run': False,
        'applied': success,
        'rc': rc,
        'output': (out or '')[:500],
        'error': (errstr or '') if not success else '',
    }, (200 if success else 500)


def _api_policy_appliers():
    """GET — Catalog of which checks have a programmatic fix available."""
    return {
        'appliers': [
            {
                'check_id': k,
                'severity': v['severity'],
                'description': v['description'],
            }
            for k, v in POLICY_APPLIERS.items()
        ],
        'manual_only': sorted(set(c['id'] for c in POLICY_CHECKS) - set(POLICY_APPLIERS.keys())),
    }


# ---------------------------------------------------------------------------
# Permission check
# ---------------------------------------------------------------------------

def _require_admin():
    from pegaprox.models.permissions import ROLE_ADMIN
    username = request.session.get('user', '')
    users = load_users()
    user = users.get(username, {})
    if user.get('role') != ROLE_ADMIN:
        return {'error': 'Admin access required'}, 403
    return None


def _get_username():
    return request.session.get('user', 'unknown')


# ---------------------------------------------------------------------------
# API Route handlers
# ---------------------------------------------------------------------------

def _api_overview():
    """GET — Swarm overview dashboard."""
    cached = _cache_get('overview')
    if cached:
        return cached
    data = _fetch_overview()
    _cache_set('overview', data)
    return data


def _api_nodes():
    """GET — List Swarm nodes."""
    cached = _cache_get('nodes')
    if cached:
        return cached
    data = _fetch_nodes()
    _cache_set('nodes', data)
    return data


def _api_services():
    """GET — List all services."""
    cached = _cache_get('services')
    if cached:
        return cached
    data = _fetch_services()
    _cache_set('services', data)
    return data


def _api_stacks():
    """GET — List stacks."""
    cached = _cache_get('stacks')
    if cached:
        return cached
    data = _fetch_stacks()
    _cache_set('stacks', data)
    return data


def _api_containers():
    """GET — List containers on manager node."""
    data = _fetch_containers()
    return {'containers': data}


def _api_networks():
    """GET — List networks."""
    return {'networks': _fetch_networks()}


def _api_volumes():
    """GET — List volumes."""
    return {'volumes': _fetch_volumes()}


def _api_images():
    """GET — List images."""
    return {'images': _fetch_images()}


def _api_service_detail():
    """GET — Full service inspect with all config sections. ?service_id=xxx[&unmask=1]
    Sensitive env values (matching password|secret|token|key|jwt|auth|...) are masked
    by default; admins can request the raw values via ?unmask=1 (logged for audit).
    Non-admins requesting unmask get an explicit 403 (no silent downgrade)."""
    service_id = request.args.get('service_id', '')
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400
    want_unmask = request.args.get('unmask', '').lower() in ('1', 'true', 'yes')
    if want_unmask and not _is_admin():
        return {'error': 'Admin access required to unmask sensitive env values'}, 403
    can_unmask = want_unmask
    if can_unmask:
        log_audit(_get_username(), 'docker.service_envs_unmasked',
                  f'Viewed unmasked envs for {service_id}')

    inspect = _docker_json(f'docker service inspect {service_id} --format "{{{{json .}}}}"')
    if inspect and isinstance(inspect, list):
        inspect = inspect[0]
    if not inspect:
        return {'error': f'Service {service_id} not found'}, 404

    spec = inspect.get('Spec', {})
    task_tmpl = spec.get('TaskTemplate', {})
    container_spec = task_tmpl.get('ContainerSpec', {})
    resources = task_tmpl.get('Resources', {})
    placement = task_tmpl.get('Placement', {})
    restart_policy = task_tmpl.get('RestartPolicy', {})
    update_config = spec.get('UpdateConfig', {})
    rollback_config = spec.get('RollbackConfig', {})
    log_driver = task_tmpl.get('LogDriver', {})
    endpoint_spec = spec.get('EndpointSpec', {})
    endpoint = inspect.get('Endpoint', {})
    mode = spec.get('Mode', {})
    networks = task_tmpl.get('Networks', [])

    # Get tasks
    tasks = _docker_json(
        f'docker service ps {service_id} --format "{{{{json .}}}}" --no-trunc 2>/dev/null'
    ) or []

    # Previous spec for rollback
    prev_spec = inspect.get('PreviousSpec', {})

    detail = {
        'id': inspect.get('ID', ''),
        'name': spec.get('Name', ''),
        'created': inspect.get('CreatedAt', ''),
        'updated': inspect.get('UpdatedAt', ''),
        'version': inspect.get('Version', {}).get('Index', 0),
        'update_status': inspect.get('UpdateStatus', {}),

        # Scheduling
        'mode_type': 'replicated' if 'Replicated' in mode else 'global' if 'Global' in mode else 'unknown',
        'replicas': mode.get('Replicated', {}).get('Replicas', 0) if 'Replicated' in mode else None,

        # Container spec
        'image': container_spec.get('Image', ''),
        'command': container_spec.get('Command', []),
        'args': container_spec.get('Args', []),
        'env': _mask_env_list(container_spec.get('Env', []), unmask=can_unmask),
        'env_masked': not can_unmask,
        'dir': container_spec.get('Dir', ''),
        'user': container_spec.get('User', ''),
        'hostname': container_spec.get('Hostname', ''),
        'hosts': container_spec.get('Hosts', []),
        'dns': container_spec.get('DNSConfig', {}),
        'stop_grace_period': container_spec.get('StopGracePeriod', 0),
        'healthcheck': container_spec.get('Healthcheck', {}),
        'read_only': container_spec.get('ReadOnly', False),
        'init': container_spec.get('Init', None),

        # Labels
        'service_labels': spec.get('Labels', {}),
        'container_labels': container_spec.get('Labels', {}),

        # Mounts
        'mounts': container_spec.get('Mounts', []),

        # Networks & Ports
        'networks': networks,
        'endpoint_mode': endpoint_spec.get('Mode', 'vip'),
        'ports': endpoint_spec.get('Ports', []),
        'published_ports': endpoint.get('Ports', []),
        'virtual_ips': endpoint.get('VirtualIPs', []),

        # Resources
        'resource_limits': resources.get('Limits', {}),
        'resource_reservations': resources.get('Reservations', {}),

        # Placement
        'constraints': placement.get('Constraints', []),
        'preferences': placement.get('Preferences', []),
        'platforms': placement.get('Platforms', []),
        'max_replicas': placement.get('MaxReplicas', 0),

        # Restart policy
        'restart_condition': restart_policy.get('Condition', 'any'),
        'restart_delay': restart_policy.get('Delay', 0),
        'restart_max_attempts': restart_policy.get('MaxAttempts', 0),
        'restart_window': restart_policy.get('Window', 0),

        # Update config
        'update_parallelism': update_config.get('Parallelism', 1),
        'update_delay': update_config.get('Delay', 0),
        'update_failure_action': update_config.get('FailureAction', 'pause'),
        'update_monitor': update_config.get('Monitor', 0),
        'update_max_failure_ratio': update_config.get('MaxFailureRatio', 0),
        'update_order': update_config.get('Order', 'stop-first'),

        # Rollback config
        'rollback_parallelism': rollback_config.get('Parallelism', 1),
        'rollback_delay': rollback_config.get('Delay', 0),
        'rollback_failure_action': rollback_config.get('FailureAction', 'pause'),
        'rollback_order': rollback_config.get('Order', 'stop-first'),

        # Logging
        'log_driver': log_driver.get('Name', ''),
        'log_options': log_driver.get('Options', {}),

        # Configs & Secrets
        'configs': container_spec.get('Configs', []),
        'secrets': container_spec.get('Secrets', []),

        # Tasks
        'tasks': tasks,

        # Rollback available?
        'has_previous_spec': bool(prev_spec),
        'previous_image': prev_spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', '') if prev_spec else '',
    }

    return detail


def _api_service_rollback():
    """POST — Rollback a service. Body: {service_id} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_id = data.get('service_id', '')
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    result = _docker_cmd(f'docker service rollback {shlex.quote(service_id)}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_rollback', f'Rolled back service {service_id}')
        _invalidate('services')
        return {'success': True, 'message': f'Service {service_id} rolled back'}
    return {'error': 'Rollback failed'}, 500


def _api_service_update():
    """POST — Update service config. Body: {service_id, image, replicas, env, ...} (admin only)
    All user-controlled args are validated against allowlists before being shlex.quote'd
    into the command. No raw interpolation of strings into the shell."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_id = data.get('service_id', '')
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    cmd_parts = ['docker', 'service', 'update']

    if data.get('image'):
        if not _valid(_RX_IMAGE_REF, data['image']):
            return {'error': 'Invalid image ref'}, 400
        cmd_parts += ['--image', shlex.quote(data['image'])]

    if data.get('replicas') is not None:
        try:
            r = int(data['replicas'])
            if r < 0 or r > 1000:
                return {'error': 'replicas out of range (0-1000)'}, 400
        except (TypeError, ValueError):
            return {'error': 'replicas must be int'}, 400
        cmd_parts += ['--replicas', str(r)]

    if bool(data.get('force')) is True:
        cmd_parts.append('--force')

    for e in data.get('env_add', []) or []:
        if not _valid(_RX_ENV_ENTRY, e):
            return {'error': f'Invalid env_add entry: {e[:60]}'}, 400
        cmd_parts += ['--env-add', shlex.quote(e)]

    for e in data.get('env_rm', []) or []:
        # env_rm is just the KEY — must be a valid env name
        if not isinstance(e, str) or not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$', e):
            return {'error': f'Invalid env_rm entry: {e[:60]}'}, 400
        cmd_parts += ['--env-rm', shlex.quote(e)]

    if 'limit_cpu' in data and data['limit_cpu'] != '':
        if not _valid(_RX_RESOURCE, str(data['limit_cpu'])):
            return {'error': 'Invalid limit_cpu'}, 400
        cmd_parts += ['--limit-cpu', shlex.quote(str(data['limit_cpu']))]

    if 'limit_memory' in data and data['limit_memory'] != '':
        if not _valid(_RX_RESOURCE, str(data['limit_memory'])):
            return {'error': 'Invalid limit_memory'}, 400
        cmd_parts += ['--limit-memory', shlex.quote(str(data['limit_memory']))]

    cmd_parts.append(shlex.quote(service_id))
    cmd = ' '.join(cmd_parts)

    result = _docker_cmd(cmd)
    if result is not None:
        log_audit(_get_username(), 'docker.service_updated', f'Updated service {service_id}')
        _invalidate('services')
        return {'success': True, 'message': result or f'Service {service_id} updated'}
    return {'error': 'Update failed'}, 500


def _api_tasks():
    """GET — Tasks for a service. ?service_id=xxx"""
    service_id = request.args.get('service_id', '')
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400
    tasks = _fetch_tasks(service_id)
    return {'tasks': tasks, 'service_id': service_id}


def _api_service_logs():
    """GET — Logs for a service. ?service_id=xxx&tail=100"""
    service_id = request.args.get('service_id', '')
    tail = request.args.get('tail', '100')
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    try:
        tail = max(1, min(int(tail), 1000))
    except (TypeError, ValueError):
        tail = 100

    logs = _docker_cmd(f'docker service logs --tail {tail} --no-trunc {shlex.quote(service_id)} 2>&1')
    return {'logs': logs or '', 'service_id': service_id, 'tail': tail}


def _api_container_logs():
    """GET — Logs for a container. ?container_id=xxx&tail=100"""
    container_id = request.args.get('container_id', '')
    tail = request.args.get('tail', '100')
    if not _valid(_RX_DOCKER_REF, container_id):
        return {'error': 'Valid container_id required'}, 400

    try:
        tail = max(1, min(int(tail), 1000))
    except (TypeError, ValueError):
        tail = 100

    logs = _docker_cmd(f'docker logs --tail {tail} {shlex.quote(container_id)} 2>&1')
    return {'logs': logs or '', 'container_id': container_id, 'tail': tail}


def _api_service_scale():
    """POST — Scale a service. Body: {service_id, replicas} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_id = data.get('service_id', '')
    replicas = data.get('replicas')

    if replicas is None:
        return {'error': 'service_id and replicas required'}, 400
    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    try:
        replicas = int(replicas)
        if replicas < 0 or replicas > 1000:
            return {'error': 'Replicas must be 0-1000'}, 400
    except (TypeError, ValueError):
        return {'error': 'Replicas must be integer'}, 400

    result = _docker_cmd(f'docker service scale {shlex.quote(service_id)}={replicas}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_scaled',
                  f'Scaled {service_id} to {replicas} replicas')
        _invalidate('services')
        return {'success': True, 'message': result}
    return {'error': 'Scale command failed'}, 500


def _api_service_restart():
    """POST — Force update (restart) a service. Body: {service_id} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_id = data.get('service_id', '')

    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    result = _docker_cmd(f'docker service update --force {shlex.quote(service_id)}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_restarted',
                  f'Force-updated service {service_id}')
        _invalidate('services')
        return {'success': True, 'message': f'Service {service_id} force-updated'}
    return {'error': 'Restart command failed'}, 500


def _api_service_remove():
    """POST — Remove a service. Body: {service_id} (admin only)"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    service_id = data.get('service_id', '')

    if not _valid(_RX_DOCKER_REF, service_id):
        return {'error': 'Valid service_id required'}, 400

    result = _docker_cmd(f'docker service rm {shlex.quote(service_id)}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_removed',
                  f'Removed service {service_id}')
        _invalidate('all')
        return {'success': True, 'message': f'Service {service_id} removed'}
    return {'error': 'Remove command failed'}, 500


def _api_stack_deploy():
    """POST — Deploy/update a stack. Body: {stack_name, compose_yaml} (admin only)"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')
    compose = data.get('compose_yaml', '')

    if not stack_name or not compose:
        return {'error': 'stack_name and compose_yaml required'}, 400
    if not _valid(_RX_STACK_NAME, stack_name):
        return {'error': 'Invalid stack_name'}, 400
    if not isinstance(compose, str) or len(compose) > 1024 * 1024:
        return {'error': 'compose_yaml must be string ≤1MiB'}, 400

    # Write compose to remote via mktemp (race-free, unguessable path), deploy, cleanup.
    # The base64 payload contains no shell metacharacters; trap is installed BEFORE
    # any write so a partial-write failure still cleans up.
    import base64
    b64 = base64.b64encode(compose.encode()).decode()
    qname = shlex.quote(stack_name)
    qb64 = shlex.quote(b64)
    cmd = (
        f'set -e; '
        f'tmpf=$(mktemp -t pegaprox_stack.XXXXXXXX) && '
        f'trap "rm -f \\"$tmpf\\"" EXIT && '
        f'chmod 600 "$tmpf" && '
        f'printf %s {qb64} | base64 -d > "$tmpf" && '
        f'docker stack deploy -c "$tmpf" {qname}'
    )
    result = _docker_cmd(cmd)
    if result is not None:
        log_audit(_get_username(), 'docker.stack_deployed', f'Deployed stack {stack_name}')
        _invalidate('all')
        return {'success': True, 'message': result}
    return {'error': 'Stack deploy failed'}, 500


def _api_stack_detail():
    """GET — Detailed info for a stack. ?name=xxx[&unmask=1]
    Env vars masked unless admin requests unmask. Non-admins get 403 on unmask."""
    name = request.args.get('name', '')
    if not _valid(_RX_STACK_NAME, name):
        return {'error': 'Valid stack name required'}, 400
    want_unmask = request.args.get('unmask', '').lower() in ('1', 'true', 'yes')
    if want_unmask and not _is_admin():
        return {'error': 'Admin access required to unmask sensitive env values'}, 403
    can_unmask = want_unmask
    if can_unmask:
        log_audit(_get_username(), 'docker.stack_envs_unmasked',
                  f'Viewed unmasked envs for stack {name}')

    # Get services for this stack
    services = _docker_json(
        f'docker service ls --filter label=com.docker.stack.namespace={name} --format "{{{{json .}}}}"'
    ) or []

    # Enrich each service with tasks
    for svc in services:
        svc_name = svc.get('Name', '')
        tasks = _docker_json(
            f'docker service ps {svc_name} --format "{{{{json .}}}}" --no-trunc 2>/dev/null'
        ) or []
        svc['tasks'] = tasks
        # Get inspect for image, ports, etc.
        inspect = _docker_json(f'docker service inspect {svc_name} --format "{{{{json .}}}}"')
        if inspect and isinstance(inspect, list):
            inspect = inspect[0]
        if inspect:
            spec = inspect.get('Spec', {})
            task_tmpl = spec.get('TaskTemplate', {})
            container_spec = task_tmpl.get('ContainerSpec', {})
            endpoint = inspect.get('Endpoint', {})
            mode = spec.get('Mode', {})
            svc['image_full'] = container_spec.get('Image', '').split('@')[0]
            svc['env_count'] = len(container_spec.get('Env', []))
            svc['env_vars'] = _mask_env_list(container_spec.get('Env', []), unmask=can_unmask)
            svc['env_masked'] = not can_unmask
            svc['mounts'] = container_spec.get('Mounts', [])
            svc['ports_detail'] = endpoint.get('Ports', [])
            svc['constraints'] = task_tmpl.get('Placement', {}).get('Constraints', [])
            svc['resources_limits'] = task_tmpl.get('Resources', {}).get('Limits', {})
            svc['resources_reservations'] = task_tmpl.get('Resources', {}).get('Reservations', {})
            svc['created'] = inspect.get('CreatedAt', '')
            svc['updated'] = inspect.get('UpdatedAt', '')
            svc['labels'] = spec.get('Labels', {})
            if 'Replicated' in mode:
                svc['mode_type'] = 'replicated'
                svc['replicas_spec'] = mode['Replicated'].get('Replicas', 0)
            elif 'Global' in mode:
                svc['mode_type'] = 'global'

    # Get stack networks
    networks = _docker_json(
        f'docker network ls --filter label=com.docker.stack.namespace={name} --format "{{{{json .}}}}"'
    ) or []

    return {
        'name': name,
        'services': services,
        'services_count': len(services),
        'networks': networks,
    }


def _api_stack_compose():
    """GET — Get the compose/config for a stack (reconstructed). ?name=xxx
    Admin-only because the reconstructed YAML embeds env vars (potential secrets)."""
    err = _require_admin()
    if err:
        return err
    name = request.args.get('name', '')
    if not _valid(_RX_STACK_NAME, name):
        return {'error': 'Valid stack name required'}, 400

    # docker stack config is not available in older Docker versions
    # Try docker stack config first, fallback to reconstructing from inspect
    raw = _docker_cmd(f'docker stack config {name} 2>/dev/null')
    if raw and not raw.startswith('Error') and 'unknown' not in raw.lower():
        return {'compose': raw, 'source': 'stack-config'}

    # Fallback: reconstruct from service inspects
    services = _docker_json(
        f'docker service ls --filter label=com.docker.stack.namespace={name} --format "{{{{json .}}}}"'
    ) or []

    compose = {'version': '3.8', 'services': {}}
    for svc in services:
        svc_name = svc.get('Name', '')
        short_name = svc_name.replace(f'{name}_', '', 1)
        inspect = _docker_json(f'docker service inspect {svc_name} --format "{{{{json .}}}}"')
        if inspect and isinstance(inspect, list):
            inspect = inspect[0]
        if not inspect:
            continue

        spec = inspect.get('Spec', {})
        task_tmpl = spec.get('TaskTemplate', {})
        container_spec = task_tmpl.get('ContainerSpec', {})
        resources = task_tmpl.get('Resources', {})
        endpoint_spec = spec.get('EndpointSpec', {})
        mode = spec.get('Mode', {})

        svc_def = {}
        img = container_spec.get('Image', '').split('@')[0]
        if img:
            svc_def['image'] = img

        env = container_spec.get('Env', [])
        if env:
            svc_def['environment'] = env

        mounts = container_spec.get('Mounts', [])
        if mounts:
            volumes = []
            for m in mounts:
                src = m.get('Source', '')
                tgt = m.get('Target', '')
                ro = ':ro' if m.get('ReadOnly') else ''
                volumes.append(f'{src}:{tgt}{ro}' if src else tgt)
            svc_def['volumes'] = volumes

        ports = endpoint_spec.get('Ports', [])
        if ports:
            svc_def['ports'] = [
                f"{p.get('PublishedPort', '')}:{p.get('TargetPort', '')}/{p.get('Protocol', 'tcp')}"
                for p in ports if p.get('PublishedPort')
            ]

        constraints = task_tmpl.get('Placement', {}).get('Constraints', [])
        if constraints:
            svc_def.setdefault('deploy', {})['placement'] = {'constraints': constraints}

        if 'Replicated' in mode:
            replicas = mode['Replicated'].get('Replicas', 1)
            if replicas != 1:
                svc_def.setdefault('deploy', {})['replicas'] = replicas
        elif 'Global' in mode:
            svc_def.setdefault('deploy', {})['mode'] = 'global'

        limits = resources.get('Limits', {})
        reservations = resources.get('Reservations', {})
        if limits or reservations:
            res_def = {}
            if limits:
                res_def['limits'] = {}
                if limits.get('NanoCPUs'):
                    res_def['limits']['cpus'] = str(limits['NanoCPUs'] / 1e9)
                if limits.get('MemoryBytes'):
                    res_def['limits']['memory'] = f"{limits['MemoryBytes'] // 1024 // 1024}M"
            if reservations:
                res_def['reservations'] = {}
                if reservations.get('NanoCPUs'):
                    res_def['reservations']['cpus'] = str(reservations['NanoCPUs'] / 1e9)
                if reservations.get('MemoryBytes'):
                    res_def['reservations']['memory'] = f"{reservations['MemoryBytes'] // 1024 // 1024}M"
            svc_def.setdefault('deploy', {})['resources'] = res_def

        # User labels (exclude docker internal ones)
        user_labels = {k: v for k, v in spec.get('Labels', {}).items()
                       if not k.startswith('com.docker.')}
        if user_labels:
            svc_def['labels'] = user_labels

        compose['services'][short_name] = svc_def

    import yaml
    try:
        compose_yaml = yaml.dump(compose, default_flow_style=False, sort_keys=False)
    except ImportError:
        compose_yaml = json.dumps(compose, indent=2)

    return {'compose': compose_yaml, 'source': 'reconstructed'}


def _api_stack_logs():
    """GET — Aggregated logs from all services in a stack. ?name=xxx&tail=50"""
    name = request.args.get('name', '')
    tail = request.args.get('tail', '50')
    if not _valid(_RX_STACK_NAME, name):
        return {'error': 'Valid stack name required'}, 400

    try:
        tail = max(1, min(int(tail), 500))
    except (TypeError, ValueError):
        tail = 50

    # Get services in this stack
    services = _docker_json(
        f'docker service ls --filter label=com.docker.stack.namespace={shlex.quote(name)} --format "{{{{json .}}}}"'
    ) or []

    all_logs = []
    for svc in services:
        svc_name = svc.get('Name', '')
        if not _valid(_RX_DOCKER_REF, svc_name):
            continue
        logs = _docker_cmd(f'docker service logs --tail {tail} --no-trunc {shlex.quote(svc_name)} 2>&1')
        if logs:
            all_logs.append(f'=== {svc_name} ===\n{logs}')

    return {'logs': '\n\n'.join(all_logs), 'stack': name, 'services': len(services)}


def _stack_state_path(stack_name):
    """Return absolute path for the local state file of a stack. stack_name MUST be pre-validated."""
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, f'stack_{stack_name}.json')


def _api_stack_stop():
    """POST — Stop a stack by scaling all replicated services to 0. Body: {stack_name} (admin only)
    Saves current replica counts locally so they can be restored with stack-start."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')
    if not _valid(_RX_STACK_NAME, stack_name):
        return {'error': 'Valid stack_name required'}, 400

    services = _docker_json(
        f'docker service ls --filter label=com.docker.stack.namespace={shlex.quote(stack_name)} --format "{{{{json .}}}}"'
    ) or []

    saved_replicas = {}
    scaled = 0
    for svc in services:
        svc_name = svc.get('Name', '')
        if not _valid(_RX_DOCKER_REF, svc_name):
            continue
        replicas_str = svc.get('Replicas', '0/0')
        parts = replicas_str.split('/')
        desired = int(parts[-1]) if parts[-1].isdigit() else 0
        saved_replicas[svc_name] = desired

        if desired > 0:
            result = _docker_cmd(f'docker service scale {shlex.quote(svc_name)}=0')
            if result is not None:
                scaled += 1

    # Save replica counts LOCALLY in the plugin state dir (not in /tmp on remote).
    # Atomic write: serialize to a sibling .tmp file then os.replace into place so
    # concurrent stack-stop calls cannot leave a half-written JSON behind.
    try:
        target = _stack_state_path(stack_name)
        tmp = target + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'saved_at': datetime.now().isoformat(), 'replicas': saved_replicas}, f)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, target)
    except Exception as e:
        log.warning(f"[{PLUGIN_ID}] could not persist stack state: {e}")

    log_audit(_get_username(), 'docker.stack_stopped', f'Stopped stack {stack_name} ({scaled} services scaled to 0)')
    _invalidate('all')

    return {'success': True, 'message': f'Stack {stack_name} stopped ({scaled} services scaled to 0)', 'saved_replicas': saved_replicas}


def _api_stack_start():
    """POST — Start a stack by restoring saved replica counts. Body: {stack_name} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')
    if not _valid(_RX_STACK_NAME, stack_name):
        return {'error': 'Valid stack_name required'}, 400

    # Read saved replicas from LOCAL state file (no /tmp shenanigans)
    saved_replicas = {}
    state_file = _stack_state_path(stack_name)
    if os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                doc = json.load(f)
                saved_replicas = doc.get('replicas', {}) if isinstance(doc, dict) else {}
        except Exception as e:
            log.warning(f"[{PLUGIN_ID}] could not read stack state: {e}")

    # If no saved state, default all replicated services to 1
    if not saved_replicas:
        services = _docker_json(
            f'docker service ls --filter label=com.docker.stack.namespace={shlex.quote(stack_name)} --format "{{{{json .}}}}"'
        ) or []
        for svc in services:
            sname = svc.get('Name', '')
            if _valid(_RX_DOCKER_REF, sname):
                saved_replicas[sname] = 1

    started = 0
    for svc_name, replicas in saved_replicas.items():
        if not _valid(_RX_DOCKER_REF, svc_name):
            continue
        try:
            replicas = int(replicas)
        except (TypeError, ValueError):
            continue
        if replicas > 0:
            result = _docker_cmd(f'docker service scale {shlex.quote(svc_name)}={replicas}')
            if result is not None:
                started += 1

    log_audit(_get_username(), 'docker.stack_started', f'Started stack {stack_name} ({started} services restored)')
    _invalidate('all')

    return {'success': True, 'message': f'Stack {stack_name} started ({started} services restored)', 'replicas': saved_replicas}


def _api_stack_remove():
    """POST — Remove a stack. Body: {stack_name} (admin only)"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')

    if not _valid(_RX_STACK_NAME, stack_name):
        return {'error': 'Valid stack_name required'}, 400

    result = _docker_cmd(f'docker stack rm {shlex.quote(stack_name)}')
    if result is not None:
        log_audit(_get_username(), 'docker.stack_removed', f'Removed stack {stack_name}')
        # Drop locally-saved state so a future re-deploy starts clean
        try:
            sf = _stack_state_path(stack_name)
            if os.path.isfile(sf):
                os.remove(sf)
        except Exception:
            pass
        _invalidate('all')
        return {'success': True, 'message': f'Stack {stack_name} removed'}
    return {'error': 'Stack remove failed'}, 500


def _api_container_action():
    """POST — Container action. Body: {container_id, action, host (optional)} (admin only)
    Actions: start, stop, restart, kill, pause, unpause, remove"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    container_id = data.get('container_id', '')
    action = data.get('action', '')
    host = data.get('host', '')  # target node IP

    valid_actions = ('start', 'stop', 'restart', 'kill', 'pause', 'unpause', 'remove')
    if action not in valid_actions:
        return {'error': f'action must be one of {"/".join(valid_actions)}'}, 400
    if not _valid(_RX_DOCKER_REF, container_id):
        return {'error': 'Valid container_id required'}, 400

    qid = shlex.quote(container_id)
    cmd = f'docker rm -f {qid}' if action == 'remove' else f'docker {action} {qid}'

    if host:
        cfg = _load_config()
        target = next((h for h in cfg.get('swarm_hosts', []) if h['host'] == host), None)
        if target:
            result = _docker_cmd(cmd, host_cfg=target)
        else:
            result = _docker_cmd(cmd)
    else:
        result = _docker_cmd(cmd)

    if result is not None:
        log_audit(_get_username(), f'docker.container_{action}',
                  f'{action} container {container_id}')
        # Container counts in overview drift after start/stop/remove
        _invalidate('nodes')  # also drops 'overview'
        return {'success': True, 'message': f'Container {container_id} {action}ed'}
    return {'error': f'{action} failed'}, 500


def _api_node_action():
    """POST — Node action. Body: {node_id, action: drain|active|pause} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    node_id = data.get('node_id', '')
    action = data.get('action', '')

    if action not in ('drain', 'active', 'pause'):
        return {'error': 'action must be drain|active|pause'}, 400
    if not _valid(_RX_DOCKER_REF, node_id):
        return {'error': 'Valid node_id required'}, 400

    result = _docker_cmd(f'docker node update --availability {action} {shlex.quote(node_id)}')
    if result is not None:
        log_audit(_get_username(), f'docker.node_{action}',
                  f'Set node {node_id} to {action}')
        _invalidate('nodes')
        return {'success': True, 'message': f'Node {node_id} set to {action}'}
    return {'error': f'Node update failed'}, 500


def _api_get_config():
    """GET — Return plugin config (admin only, masks password)."""
    err = _require_admin()
    if err:
        return err
    cfg = _load_config()
    # Mask passwords, expose key_file and auth_method
    safe_hosts = []
    for h in cfg.get('swarm_hosts', []):
        safe = dict(h)
        if safe.get('password'):
            safe['password'] = '***'
        # Indicate which auth method is active
        safe['auth_method'] = 'key' if safe.get('key_file') else 'password'
        safe_hosts.append(safe)
    return {
        'swarm_hosts': safe_hosts,
        'poll_interval': cfg.get('poll_interval', 30),
    }


def _api_save_config():
    """POST — Save plugin config (admin only). Body: {swarm_hosts, poll_interval}"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    cfg = _load_config()

    if 'poll_interval' in data:
        cfg['poll_interval'] = max(10, min(300, int(data['poll_interval'])))

    if 'swarm_hosts' in data:
        new_hosts = []
        for h in data['swarm_hosts']:
            host_entry = {
                'name': h.get('name', ''),
                'host': h.get('host', ''),
                'user': h.get('user', ''),
                'key_file': h.get('key_file', ''),
                'password': h.get('password', ''),
            }
            # If password is masked, keep the old one
            if host_entry['password'] == '***':
                for old in cfg.get('swarm_hosts', []):
                    if old.get('host') == host_entry['host']:
                        host_entry['password'] = old.get('password', '')
                        break
            new_hosts.append(host_entry)
        cfg['swarm_hosts'] = new_hosts

    _save_config(cfg)
    log_audit(_get_username(), 'docker.config_saved', 'Docker Swarm plugin config updated')

    # Clear cache to force refresh with new config
    with _cache_lock:
        _cache.clear()
    # Drop pooled SSH connections — auth/host may have changed
    _ssh_pool_close_all(reason='config_saved')

    return {'success': True}


_RX_HOSTNAME = re.compile(
    r'^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?'
    r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*'
    r'|(?:\d{1,3}\.){3}\d{1,3}'
    r')$'
)
_RX_USERNAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_\-]{0,31}$')

def _api_test_connection():
    """POST — Test SSH connection to a host. Body: {host, user, key_file?, password?} (admin only)
    Admin-only because this endpoint can be abused as an SSRF/credential-spray oracle
    against arbitrary internal hosts."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    host = data.get('host', '')
    user = data.get('user', '')
    key_file = data.get('key_file', '')
    password = data.get('password', '')

    if not host or not user:
        return {'error': 'host and user required'}, 400
    if not _valid(_RX_HOSTNAME, host):
        return {'error': 'Invalid host (must be hostname or IPv4)'}, 400
    if not _valid(_RX_USERNAME, user):
        return {'error': 'Invalid user'}, 400
    if not key_file and not password:
        return {'error': 'key_file or password required'}, 400

    host_cfg = {'host': host, 'user': user, 'key_file': key_file, 'password': password}
    result = _docker_cmd('docker info --format "{{json .Swarm}}"', host_cfg=host_cfg)
    if result:
        try:
            info = json.loads(result)
            return {
                'success': True,
                'swarm_active': info.get('LocalNodeState') == 'active',
                'is_manager': info.get('ControlAvailable', False),
                'node_id': info.get('NodeID', ''),
                'nodes': info.get('Nodes', 0),
                'managers': info.get('Managers', 0),
            }
        except json.JSONDecodeError:
            return {'success': True, 'raw': result}
    return {'error': 'Connection failed or Docker not available'}, 500


def _api_serve_ui():
    """GET — Serve the plugin HTML UI."""
    html_path = os.path.join(PLUGIN_DIR, 'swarm.html')
    if os.path.exists(html_path):
        return send_file(html_path, mimetype='text/html')
    return {'error': 'UI not found'}, 404


def _api_image_pull():
    """POST — Pull an image on a specific node. Body: {image, host} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    image = data.get('image', '')
    host = data.get('host', '')
    if not _valid(_RX_IMAGE_REF, image):
        return {'error': 'Invalid image ref'}, 400

    qimg = shlex.quote(image)
    if host:
        cfg = _load_config()
        target = next((h for h in cfg.get('swarm_hosts', []) if h['host'] == host), None)
        if target:
            out, err_out, code = _ssh_exec(target, command=f'docker pull {qimg} 2>&1', timeout=120)
            if code == 0:
                log_audit(_get_username(), 'docker.image_pull', f'Pulled {image} on {host}')
                return {'success': True, 'message': out}
            return {'error': err_out or out}, 500
    result = _docker_cmd(f'docker pull {qimg} 2>&1')
    if result is not None:
        log_audit(_get_username(), 'docker.image_pull', f'Pulled {image}')
        return {'success': True, 'message': result}
    return {'error': 'Pull failed'}, 500


def _api_image_remove():
    """POST — Remove image(s). Body: {image_id, host} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    image_id = data.get('image_id', '')
    host = data.get('host', '')
    if not _valid(_RX_IMAGE_REF, image_id):
        return {'error': 'Invalid image_id'}, 400

    cmd = f'docker rmi {shlex.quote(image_id)} 2>&1'
    if host:
        cfg = _load_config()
        target = next((h for h in cfg.get('swarm_hosts', []) if h['host'] == host), None)
        if target:
            result = _docker_cmd(cmd, host_cfg=target)
        else:
            result = _docker_cmd(cmd)
    else:
        result = _docker_cmd(cmd)
    if result is not None:
        log_audit(_get_username(), 'docker.image_removed', f'Removed image {image_id}')
        return {'success': True, 'message': result}
    return {'error': 'Remove failed'}, 500


def _api_volume_remove():
    """POST — Remove volume. Body: {volume_name, host} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('volume_name', '')
    host = data.get('host', '')
    if not _valid(_RX_DOCKER_REF, name):
        return {'error': 'Valid volume_name required'}, 400

    cmd = f'docker volume rm {shlex.quote(name)} 2>&1'
    if host:
        cfg = _load_config()
        target = next((h for h in cfg.get('swarm_hosts', []) if h['host'] == host), None)
        if target:
            result = _docker_cmd(cmd, host_cfg=target)
        else:
            result = _docker_cmd(cmd)
    else:
        result = _docker_cmd(cmd)
    if result is not None:
        log_audit(_get_username(), 'docker.volume_removed', f'Removed volume {name}')
        return {'success': True, 'message': result}
    return {'error': 'Remove failed (volume may be in use)'}, 500


def _api_network_remove():
    """POST — Remove network. Body: {network_name} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('network_name', '')
    if not _valid(_RX_DOCKER_REF, name):
        return {'error': 'Valid network_name required'}, 400
    result = _docker_cmd(f'docker network rm {shlex.quote(name)} 2>&1')
    if result is not None:
        log_audit(_get_username(), 'docker.network_removed', f'Removed network {name}')
        return {'success': True, 'message': result}
    return {'error': 'Remove failed (network may be in use)'}, 500


def _api_topology():
    """GET — Return Swarm data formatted for PegaProx TopologyView multiCluster."""
    overview = _cache_get('overview') or _fetch_overview()
    services = _cache_get('services') or _fetch_services()
    nodes_data = _cache_get('nodes') or _fetch_nodes()

    if not overview or overview.get('error'):
        return {'nodes': [], 'resources': []}

    # Build nodes array matching PegaProx format: {name, status, cpu_percent, mem_percent, ...}
    topo_nodes = []
    for n in (nodes_data or []):
        res = n.get('resources', {})
        topo_nodes.append({
            'name': n.get('Hostname', n.get('addr', 'unknown')),
            'status': 'online' if n.get('state', n.get('Status', '')) in ('ready', 'Ready') else 'offline',
            'cpu_percent': 0,
            'mem_percent': 0,
            'maxmem': res.get('memory_bytes', 0),
            'mem': 0,
        })

    # Build resources array: each service as a "VM" for topology rendering
    topo_resources = []
    for svc in (services or []):
        svc_name = svc.get('Name', '')
        replicas = svc.get('Replicas', '0/0')
        parts = replicas.split('/')
        running = int(parts[0]) if parts[0].isdigit() else 0

        # Assign to a node (use constraints or deterministic crc32-based round-robin).
        # zlib.crc32 is deterministic across processes/restarts (unlike hash()).
        node_name = ''
        constraints = svc.get('constraints', [])
        for c in constraints:
            if 'node.hostname' in c and '==' in c:
                node_name = c.split('==')[-1].strip()
                break
        svc_crc = zlib.crc32(svc_name.encode('utf-8'))
        if not node_name and topo_nodes:
            idx = svc_crc % len(topo_nodes)
            node_name = topo_nodes[idx]['name']

        topo_resources.append({
            'vmid': svc_crc % 9000 + 1000,
            'name': svc_name.split('_')[-1] if '_' in svc_name else svc_name,
            'type': 'lxc',  # show as container icon
            'status': 'running' if running > 0 else 'stopped',
            'node': node_name,
            'cpu': 0,
            'mem': 0,
            'maxmem': 0,
        })

    return {
        'id': 'docker_swarm',
        'name': 'Docker Swarm',
        'nodes': topo_nodes,
        'resources': topo_resources,
    }


def _api_refresh():
    """POST — Force refresh all cached data (admin only — triggers SSH fan-out)."""
    err = _require_admin()
    if err:
        return err
    with _cache_lock:
        _cache.clear()
    return {'success': True, 'message': 'Cache cleared, next request will fetch fresh data'}


def _api_node_stats():
    """GET — Get resource stats for each node (CPU/RAM via SSH).
    Admin only: exposes infra detail (load avg, mem totals, disk, uptime) that's
    useful for reconnaissance and triggers an SSH fan-out per call."""
    err = _require_admin()
    if err:
        return err
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    if not hosts:
        return {'error': 'No hosts configured'}, 400

    stats = []
    for h in hosts:
        cmd = (
            'echo "{"'
            ' && echo "\\"hostname\\": \\"$(hostname)\\"," '
            ' && echo "\\"cpu_count\\": $(nproc),"'
            ' && echo "\\"load_1m\\": $(cat /proc/loadavg | cut -d" " -f1),"'
            ' && echo "\\"load_5m\\": $(cat /proc/loadavg | cut -d" " -f2),"'
            ' && echo "\\"load_15m\\": $(cat /proc/loadavg | cut -d" " -f3),"'
            ' && free -b | awk \'/^Mem:/ {printf "\\"mem_total\\": %s, \\"mem_used\\": %s, \\"mem_free\\": %s, \\"mem_available\\": %s,", $2, $3, $4, $7}\''
            ' && df -B1 / | awk \'NR==2 {printf "\\"disk_total\\": %s, \\"disk_used\\": %s, \\"disk_free\\": %s,", $2, $3, $4}\''
            ' && echo "\\"uptime_seconds\\": $(cat /proc/uptime | cut -d" " -f1 | cut -d. -f1)"'
            ' && echo "}"'
        )
        out, err, code = _ssh_exec(h, command=cmd)
        if code == 0:
            try:
                data = json.loads(out)
                data['host'] = h['host']
                data['name'] = h.get('name', h['host'])
                stats.append(data)
            except json.JSONDecodeError:
                stats.append({'host': h['host'], 'name': h.get('name', ''), 'error': 'Parse error'})
        else:
            stats.append({'host': h['host'], 'name': h.get('name', ''), 'error': err})

    return {'stats': stats}


def _api_load_balance():
    """GET — Load balance overview: tasks per node + resources + balance score."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import math

    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    if not hosts:
        return {'error': 'No hosts configured'}, 400

    # Get node list from Swarm to map hostnames to IDs
    swarm_nodes = _docker_json('docker node ls --format "{{json .}}"') or []
    node_id_map = {}  # hostname -> node_id
    for n in swarm_nodes:
        hostname = n.get('Hostname', '')
        node_id_map[hostname] = n.get('ID', '')

    def fetch_node_data(h):
        """Fetch tasks + stats for a single node in one SSH call."""
        hostname_out, _, _ = _ssh_exec(h, command='hostname')
        hostname = hostname_out.strip()
        node_id = node_id_map.get(hostname, '')

        # Get running tasks on this node
        tasks_cmd = (
            f'docker node ps {node_id} --filter "desired-state=running" '
            f'--format "{{{{json .}}}}" 2>/dev/null'
        ) if node_id else 'echo ""'
        tasks_out, _, tasks_code = _ssh_exec(h, command=tasks_cmd)

        tasks = []
        services_on_node = []
        if tasks_code == 0 and tasks_out.strip():
            for line in tasks_out.strip().split('\n'):
                line = line.strip()
                if line:
                    try:
                        t = json.loads(line)
                        tasks.append(t)
                        svc_name = t.get('Name', '').rsplit('.', 1)[0]
                        if svc_name and svc_name not in services_on_node:
                            services_on_node.append(svc_name)
                    except json.JSONDecodeError:
                        pass

        # Get CPU/RAM stats
        stats_cmd = (
            'echo "{"'
            ' && echo "\\"cpu_count\\": $(nproc),"'
            ' && echo "\\"load_1m\\": $(cat /proc/loadavg | cut -d" " -f1),"'
            ' && free -b | awk \'/^Mem:/ {printf "\\"mem_total\\": %s, \\"mem_used\\": %s,", $2, $3}\''
            ' && echo "\\"_end\\": 0"'
            ' && echo "}"'
        )
        stats_out, _, stats_code = _ssh_exec(h, command=stats_cmd)
        cpu_percent = 0.0
        mem_percent = 0.0
        mem_used = 0
        mem_total = 0
        cpu_count = 0
        if stats_code == 0:
            try:
                s = json.loads(stats_out)
                cpu_count = s.get('cpu_count', 1)
                load = float(s.get('load_1m', 0))
                cpu_percent = round((load / max(cpu_count, 1)) * 100, 1)
                mem_total = s.get('mem_total', 0)
                mem_used = s.get('mem_used', 0)
                mem_percent = round((mem_used / max(mem_total, 1)) * 100, 1)
            except (json.JSONDecodeError, ValueError):
                pass

        return {
            'name': h.get('name', hostname),
            'hostname': hostname,
            'host': h['host'],
            'node_id': node_id[:12] if node_id else '',
            'tasks_running': len(tasks),
            'cpu_count': cpu_count,
            'cpu_percent': cpu_percent,
            'mem_percent': mem_percent,
            'mem_used': mem_used,
            'mem_total': mem_total,
            'services': services_on_node,
        }

    # Fetch all nodes in parallel
    nodes_data = []
    with ThreadPoolExecutor(max_workers=len(hosts), thread_name_prefix='lb-fetch') as pool:
        futures = {pool.submit(fetch_node_data, h): h for h in hosts}
        for future in as_completed(futures):
            try:
                nodes_data.append(future.result())
            except Exception as e:
                h = futures[future]
                nodes_data.append({
                    'name': h.get('name', h['host']), 'host': h['host'],
                    'tasks_running': 0, 'cpu_percent': 0, 'mem_percent': 0,
                    'services': [], 'error': str(e),
                })

    # Sort by config order
    host_order = {h['host']: i for i, h in enumerate(hosts)}
    nodes_data.sort(key=lambda n: host_order.get(n['host'], 99))

    # Calculate balance score.
    #   - 100 = tasks evenly distributed across all healthy nodes
    #   - 0 = pathological (all tasks on a single node, or only one healthy node in a multi-node cluster)
    # Healthy = no error AND availability is "active" if reported. We include healthy nodes with
    # zero tasks so the average reflects the *capacity* of the cluster, not just the busy nodes.
    total_tasks = sum(n['tasks_running'] for n in nodes_data)
    healthy_nodes = [n for n in nodes_data if not n.get('error')]
    nodes_with_tasks = [n for n in healthy_nodes if n['tasks_running'] > 0]

    balance_score = 100
    recommendation = None
    if total_tasks > 0:
        if len(healthy_nodes) <= 1:
            # Only one healthy node serving tasks → not balanced regardless of "tasks per node"
            balance_score = 0
            recommendation = (
                f"Sólo {len(healthy_nodes)} nodo activo: el cluster no está repartiendo carga"
                if len(healthy_nodes) == 1 else
                "No hay nodos activos sirviendo tasks"
            )
        elif len(nodes_with_tasks) == 1 and len(healthy_nodes) > 1:
            # Multiple nodes available, but every task is on one — explicit worst case
            balance_score = 0
            only = nodes_with_tasks[0]
            recommendation = (
                f"{only['name']} concentra el 100% de los tasks "
                f"({only['tasks_running']}) — los demás nodos están desocupados"
            )
        else:
            avg = total_tasks / len(healthy_nodes)
            variance = sum((n['tasks_running'] - avg) ** 2 for n in healthy_nodes) / len(healthy_nodes)
            std_dev = math.sqrt(variance)
            balance_score = max(0, round(100 - (std_dev / max(avg, 1)) * 100))

            max_node = max(healthy_nodes, key=lambda n: n['tasks_running'])
            if max_node['tasks_running'] > avg * 1.3 and avg > 0:
                pct_over = round(((max_node['tasks_running'] - avg) / avg) * 100)
                recommendation = (
                    f"{max_node['name']} tiene {pct_over}% más tasks que el promedio "
                    f"({max_node['tasks_running']} vs {avg:.0f})"
                )

    result = {
        'nodes': nodes_data,
        'total_tasks': total_tasks,
        'balance_score': balance_score,
        'recommendation': recommendation,
        'updated_at': datetime.now().isoformat(),
    }
    # Persist a sample row per node — powers /metrics/history + /metrics/trends.
    # Best-effort; never blocks the response.
    try:
        _metrics_record_load_balance(result)
    except Exception as e:
        log.error(f"[{PLUGIN_ID}] metrics piggyback failed: {e}")
    return result


def _api_metrics_history():
    """GET — Time-series for one node + one metric.
    Query: ?host=<ip>&metric=cpu_percent&duration=24h
    """
    host = request.args.get('host', '').strip()
    metric = request.args.get('metric', 'cpu_percent').strip()
    duration = request.args.get('duration', '24h').strip()

    if not host:
        return {'error': 'host required'}, 400
    # Allow either IP or hostname. We don't have a strict IP regex, but
    # disallow shell-meta and length-bound it.
    if len(host) > 64 or any(c in host for c in (';', '|', '&', '`', '$', '\n', '\r', '\x00')):
        return {'error': 'invalid host'}, 400
    if metric not in ('cpu_percent', 'mem_percent', 'mem_used', 'tasks_running', 'cpu_count'):
        return {'error': 'invalid metric'}, 400

    duration_sec = _parse_duration_to_sec(duration)
    points = _metrics_query_history(host, metric, duration_sec)
    return {
        'host': host,
        'metric': metric,
        'duration_sec': duration_sec,
        'count': len(points),
        'points': points,
    }


def _api_metrics_trends():
    """GET — Summary stats per node over a window.
    Query: ?duration=24h
    """
    duration = request.args.get('duration', '24h').strip()
    duration_sec = _parse_duration_to_sec(duration)
    nodes = _metrics_query_trends(duration_sec)
    return {
        'duration_sec': duration_sec,
        'nodes': nodes,
        'updated_at': datetime.now().isoformat(),
    }


def _api_rebalance_service():
    """POST — Force rebalance a service. Body: {service_name} (admin only)"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_name = data.get('service_name', '')
    if not _valid(_RX_DOCKER_REF, service_name):
        return {'error': 'Valid service_name required'}, 400

    result = _docker_cmd(f'docker service update --force {shlex.quote(service_name)} 2>&1')
    if result is not None:
        log_audit(_get_username(), 'docker.service_rebalance',
                  f'Force rebalanced service {service_name}')
        _invalidate('services')
        return {'success': True, 'message': f'Service {service_name} rebalancing'}
    return {'error': 'Rebalance failed'}, 500


# ---------------------------------------------------------------------------
# Smart Rebalance — diagnostics + cluster-wide action (v1.14.0)
# ---------------------------------------------------------------------------
# The Balance tab tells you WHAT the imbalance is. This adds WHY (which
# services contribute, which are pinned and can't move) and HOW to fix it
# (one-click force-update of every eligible service in sequence, throttled).

# Constraint patterns that "pin" a service to a single node — moving these
# wouldn't help. We're conservative: any constraint involving node.id or
# node.hostname with == is treated as pinning. node.role, node.labels.* are
# capability-based and don't pin to a single node.
_RX_PIN_CONSTRAINT = re.compile(r'\bnode\.(id|hostname)\s*==\s*\S+', re.IGNORECASE)


def _service_is_pinned(svc):
    """True if the service has a constraint that locks it to specific nodes
    by id/hostname. Capability constraints (role, labels) don't count."""
    for c in (svc.get('constraints') or []):
        if isinstance(c, str) and _RX_PIN_CONSTRAINT.search(c):
            return True
    return False


def _eligible_for_rebalance(svc):
    """Service is a sensible target for force-update rebalance.
    Skip global mode (no spread to do), replicas≤1 (single instance),
    and pinned services."""
    if svc.get('mode_type') != 'replicated':
        return False, 'global o non-replicated'
    if (svc.get('replicas_spec') or 0) <= 1:
        return False, f"replicas={svc.get('replicas_spec', 0)} (no hay nada que distribuir)"
    if _service_is_pinned(svc):
        return False, 'constraint node.id/hostname pin a un nodo específico'
    return True, 'elegible'


def _compute_balance_insights():
    """Diagnose why the cluster is unbalanced + list rebalance candidates."""
    lb = _api_load_balance()
    if not lb or 'nodes' not in lb:
        return {'error': 'Load balance data unavailable'}

    services = _cache_get('services') or _fetch_services() or []
    nodes = lb.get('nodes', [])
    if not nodes:
        return {'error': 'No nodes'}

    healthy = [n for n in nodes if not n.get('error')]
    if len(healthy) <= 1:
        return {
            'imbalance_pct': 0,
            'verdict': 'Solo 1 nodo sano — no hay nada que rebalancear.',
            'candidates': [], 'pinned': [], 'singletons': [],
        }

    avg = sum(n['tasks_running'] for n in healthy) / len(healthy)
    hot = max(healthy, key=lambda n: n['tasks_running'])
    cold = min(healthy, key=lambda n: n['tasks_running'])
    spread = hot['tasks_running'] - cold['tasks_running']
    imbalance_pct = round((spread / max(avg, 1)) * 100, 1) if avg > 0 else 0

    # Categorize every replicated service
    candidates = []
    pinned = []
    singletons = []
    for svc in services:
        name = svc.get('Name', '')
        replicas = svc.get('replicas_spec', 0) or 0
        ok, reason = _eligible_for_rebalance(svc)
        info = {
            'name': name,
            'stack': svc.get('stack', ''),
            'replicas': replicas,
            'reason': reason,
        }
        if ok:
            candidates.append(info)
        elif svc.get('mode_type') == 'replicated' and replicas == 1:
            singletons.append(info)
        elif _service_is_pinned(svc):
            pinned.append({**info, 'constraints': svc.get('constraints') or []})

    # Verdict text
    if imbalance_pct < 10:
        verdict = f'Cluster bien balanceado (gap {imbalance_pct}% del promedio).'
    elif imbalance_pct < 25:
        verdict = (
            f'{cold["name"]} ({cold["tasks_running"]} tasks) tiene {spread} tasks menos '
            f'que {hot["name"]} ({hot["tasks_running"]}). Forzar redeploy de los '
            f'{len(candidates)} servicios elegibles puede mejorar.'
        )
    else:
        verdict = (
            f'Imbalance significativo: {hot["name"]} concentra {hot["tasks_running"]} tasks '
            f'mientras {cold["name"]} solo tiene {cold["tasks_running"]}. '
            f'Recomendado: rebalance automático ({len(candidates)} servicios elegibles).'
        )

    return {
        'imbalance_pct': imbalance_pct,
        'task_avg': round(avg, 1),
        'hot_node': {'name': hot['name'], 'tasks': hot['tasks_running']},
        'cold_node': {'name': cold['name'], 'tasks': cold['tasks_running']},
        'verdict': verdict,
        'candidates': sorted(candidates, key=lambda c: -c['replicas']),
        'pinned': pinned,
        'singletons': singletons,
        'totals': {
            'eligible': len(candidates),
            'pinned': len(pinned),
            'singletons': len(singletons),
        },
    }


def _api_balance_insights():
    """GET — Why the cluster isn't balanced + which services can be moved."""
    cached = _cache_get('balance_insights')
    if cached:
        return cached
    try:
        report = _compute_balance_insights()
    except Exception as e:
        log.error(f"[{PLUGIN_ID}] balance insights failed: {e}")
        return {'error': str(e)}, 500
    _cache_set('balance_insights', report)
    return report


# --- Async job machinery (v1.14.1) ----------------------------------------
# Force-update of N services takes minutes-to-hours because each invocation
# blocks until Swarm converges. Doing this in the request handler causes
# nginx 60s timeout / browser disconnect (HTTP 499), even though the work
# keeps running in pegaprox. We move the loop to a daemon thread, return
# {job_id} immediately, and expose a status endpoint for UI polling.

_rebalance_jobs = {}
_rebalance_jobs_lock = threading.Lock()
_REBALANCE_JOBS_RETENTION_SEC = 86400  # keep finished jobs visible for 24h


def _rebalance_jobs_prune():
    """Drop finished jobs older than retention window. Called opportunistically."""
    cutoff = time.time() - _REBALANCE_JOBS_RETENTION_SEC
    with _rebalance_jobs_lock:
        for jid in list(_rebalance_jobs.keys()):
            j = _rebalance_jobs[jid]
            if j.get('finished_at') and j['finished_at'] < cutoff:
                del _rebalance_jobs[jid]


def _start_rebalance_job(candidates, delay_sec, username):
    """Spawn a daemon thread that force-updates each candidate in sequence.
    Returns the job_id immediately. Status is observable via _rebalance_jobs."""
    _rebalance_jobs_prune()
    job_id = _uuid_mod.uuid4().hex[:16]
    with _rebalance_jobs_lock:
        _rebalance_jobs[job_id] = {
            'job_id': job_id,
            'status': 'running',
            'total': len(candidates),
            'completed': 0,
            'failed': 0,
            'current_service': None,
            'current_index': 0,
            'results': [],
            'started_at': time.time(),
            'finished_at': None,
            'started_by': username,
            'queue': [c['name'] for c in candidates],
        }

    def _worker():
        for idx, c in enumerate(candidates):
            name = c['name']
            with _rebalance_jobs_lock:
                _rebalance_jobs[job_id]['current_service'] = name
                _rebalance_jobs[job_id]['current_index'] = idx + 1
            success = False
            err = ''
            output = ''
            if not _valid(_RX_DOCKER_REF, name):
                err = 'invalid service name'
            else:
                cmd = f"docker service update --force {shlex.quote(name)}"
                out = _docker_cmd(cmd + ' 2>&1')
                if out is None:
                    err = 'docker_cmd returned None'
                else:
                    # v1.14.2: distinguish real success from auto-rollback.
                    # `docker service update --force` exits 0 even when the
                    # orchestrator rolled the change back because of a deadlock
                    # (e.g. start-first + max_replicas_per_node=1 + replicas==nodes).
                    # Check UpdateStatus.State to know whether tasks actually moved.
                    state_cmd = (
                        f"docker service inspect {shlex.quote(name)} "
                        f"--format '{{{{.UpdateStatus.State}}}}'"
                    )
                    state = (_docker_cmd(state_cmd) or '').strip()
                    output = (out or '')[:200]
                    if state.startswith('rollback'):
                        err = (
                            f'auto-rolled back (state={state}). Tasks did not migrate. '
                            f'Likely cause: update_config.order=start-first with '
                            f'max_replicas_per_node and replicas equal-to-nodes — '
                            f'change order to stop-first or relax the placement cap.'
                        )
                    elif state in ('updating', 'paused', 'rollback_started'):
                        # Force is synchronous, so this is unexpected; flag it.
                        err = f'update did not finish (state={state})'
                    else:
                        # 'completed', empty, or '<no value>' (no UpdateStatus key) = real success
                        success = True

            with _rebalance_jobs_lock:
                _rebalance_jobs[job_id]['results'].append({
                    'service': name,
                    'success': success,
                    'output': output,
                    'error': err,
                    'finished_at': time.time(),
                })
                _rebalance_jobs[job_id]['completed'] += 1
                if not success:
                    _rebalance_jobs[job_id]['failed'] += 1

            try:
                log_audit(username, 'docker.balance_rebalance_all',
                          f'force-update {name} (success={success})')
            except Exception:
                pass

            if delay_sec > 0 and idx < len(candidates) - 1:
                time.sleep(delay_sec)

        # Finalize
        with _rebalance_jobs_lock:
            j = _rebalance_jobs[job_id]
            j['current_service'] = None
            j['finished_at'] = time.time()
            j['status'] = 'completed' if j['failed'] == 0 else 'completed_with_errors'

        # Invalidate caches so next UI read shows fresh state
        try:
            _invalidate('services')
            with _cache_lock:
                _cache.pop('load_balance', None)
                _cache.pop('balance_insights', None)
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True, name=f'rebalance-{job_id}')
    t.start()
    return job_id


def _api_balance_rebalance_all():
    """POST — Plan or kick off a cluster-wide force-update rebalance.

    Body:
      dry_run: bool (default true) — return the plan without executing
      max_services: int (default 0 = no cap) — only touch the first N
      delay_sec: int (default 5) — pause between updates

    Admin-only. With dry_run=false, returns IMMEDIATELY with a job_id.
    Poll GET /balance/rebalance-status?job_id=X for progress.
    """
    err = _require_admin()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get('dry_run', True))
    max_services = int(payload.get('max_services', 0) or 0)
    delay_sec = max(0, min(int(payload.get('delay_sec', 5) or 5), 30))

    insights = _compute_balance_insights()
    if 'error' in insights:
        return insights, 400
    candidates = insights.get('candidates', [])
    if max_services > 0:
        candidates = candidates[:max_services]

    if dry_run:
        return {
            'dry_run': True,
            'verdict': insights.get('verdict'),
            'imbalance_pct': insights.get('imbalance_pct'),
            'will_touch': [c['name'] for c in candidates],
            'count': len(candidates),
            'delay_sec': delay_sec,
            'pinned_skipped': len(insights.get('pinned', [])),
            'singletons_skipped': len(insights.get('singletons', [])),
        }

    # Real run — fire-and-forget background thread
    if not candidates:
        return {'dry_run': False, 'started': False, 'reason': 'no eligible services'}

    job_id = _start_rebalance_job(candidates, delay_sec, _get_username())
    return {
        'dry_run': False,
        'started': True,
        'job_id': job_id,
        'total': len(candidates),
        'delay_sec': delay_sec,
        'poll_url': f'/api/plugins/{PLUGIN_ID}/api/balance/rebalance-status?job_id={job_id}',
    }


def _api_balance_rebalance_status():
    """GET — Status of a rebalance job, or list of all jobs if no id."""
    job_id = request.args.get('job_id', '').strip()
    with _rebalance_jobs_lock:
        if job_id:
            j = _rebalance_jobs.get(job_id)
            if not j:
                return {'error': 'job not found', 'job_id': job_id}, 404
            # Return a copy with derived fields
            elapsed = time.time() - j['started_at']
            avg_per_service = (elapsed / max(j['completed'], 1)) if j['completed'] > 0 else 0
            remaining = max(j['total'] - j['completed'], 0)
            eta_sec = int(avg_per_service * remaining) if avg_per_service > 0 else None
            out = dict(j)
            out['elapsed_sec'] = int(elapsed)
            out['eta_sec'] = eta_sec
            out['progress_pct'] = round((j['completed'] / max(j['total'], 1)) * 100, 1)
            return out
        # List all jobs (prune first)
        return {
            'jobs': sorted(
                [{
                    'job_id': j['job_id'],
                    'status': j['status'],
                    'total': j['total'],
                    'completed': j['completed'],
                    'failed': j['failed'],
                    'started_at': j['started_at'],
                    'finished_at': j['finished_at'],
                    'started_by': j['started_by'],
                } for j in _rebalance_jobs.values()],
                key=lambda j: -j['started_at'],
            ),
        }


# ---------------------------------------------------------------------------
# Policy Audit endpoints (v1.11.0 — Phase 1)
# ---------------------------------------------------------------------------

def _api_policy_audit():
    """GET — Cluster-wide audit, or single service via ?service=<name>.

    Read-only. Pure Python over the cached services/nodes data, so cost is
    negligible — we still cache because dashboards poll on intervals.
    """
    service = request.args.get('service', '').strip()
    if service and not _valid(_RX_DOCKER_REF, service):
        return {'error': 'Invalid service name'}, 400

    cache_key = f'audit:{service}' if service else 'audit:all'
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        report = _run_cluster_audit(service_filter=service or None)
    except Exception as e:
        log.error(f"[{PLUGIN_ID}] Policy audit failed: {e}")
        return {'error': f'Audit failed: {e}'}, 500

    _cache_set(cache_key, report)
    return report


def _api_policy_checks():
    """GET — Catalog of available checks (id, severity, title, description)."""
    return {
        'checks': POLICY_CHECKS,
        'severity_levels': {
            'P0': 'Crítico — outage casi seguro',
            'P1': 'Importante — degrada disponibilidad o scheduler',
            'P2': 'Recomendado — best practice que evita sorpresas',
            'P3': 'Polish — nice to have',
        },
        'grade_thresholds': {
            'A': '0 P0, 0 P1, ≤1 P2',
            'B': '0 P0, ≤1 P1, ≤2 P2',
            'C': '0 P0, ≤2 P1',
            'D': '0 P0, ≥3 P1',
            'F': '≥1 P0',
        },
    }


# ---------------------------------------------------------------------------
# Webhooks (A4 — v1.10.0)
# ---------------------------------------------------------------------------
# Per-service unguessable URL that CI/CD systems POST to in order to trigger
# `docker service update --image <repo>:<tag> --force <svc>`. Auth = secret in
# URL (cryptographically random UUID4). Persisted in state/webhooks.json.
#
# Security model:
#   - The webhook secret is the ONLY auth — anyone with the URL can force-update.
#     Treat it like a deploy key. Rotation: revoke + re-create (DELETE then POST).
#   - Tag value is validated against _RX_IMAGE_REF before being interpolated.
#   - We log every successful and rejected hit via log_audit.
#   - The endpoint requires the CALLER to be unauthenticated PegaProx-wise (CI),
#     so we MUST skip the global plugins.view auth — implemented by a bypass
#     marker. PegaProx's plugin route runner enforces auth by default; the
#     `webhook` path is registered through a wrapper that runs before that.
import uuid as _uuid_mod
import hmac as _hmac

WEBHOOKS_FILE = os.path.join(STATE_DIR, 'webhooks.json')


def _load_webhooks():
    try:
        with open(WEBHOOKS_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"[{PLUGIN_ID}] could not read webhooks file: {e}")
    return {}


def _save_webhooks(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = WEBHOOKS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, WEBHOOKS_FILE)


def _api_webhook_list():
    """GET — List configured webhooks (admin). Returns secrets masked unless ?unmask=1."""
    err = _require_admin()
    if err:
        return err
    want_unmask = request.args.get('unmask', '').lower() in ('1', 'true', 'yes')
    if want_unmask and not _is_admin():
        return {'error': 'Admin access required'}, 403
    webhooks = _load_webhooks()
    if want_unmask:
        log_audit(_get_username(), 'docker.webhook_secrets_unmasked', 'Listed webhook secrets')
    out = []
    for wid, w in webhooks.items():
        out.append({
            'id': wid,
            'service_name': w.get('service_name', ''),
            'secret': w.get('secret', '') if want_unmask else '***',
            'created_at': w.get('created_at', ''),
            'created_by': w.get('created_by', ''),
            'last_triggered_at': w.get('last_triggered_at'),
            'last_triggered_tag': w.get('last_triggered_tag'),
            'trigger_count': w.get('trigger_count', 0),
        })
    return {'webhooks': out}


def _api_webhook_create():
    """POST — Create a new webhook for a service. Body: {service_name} (admin only).
    Returns: {id, secret, url} — secret shown ONCE, not retrievable again unless unmasked."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    service_name = data.get('service_name', '')
    if not _valid(_RX_DOCKER_REF, service_name):
        return {'error': 'Valid service_name required'}, 400

    webhooks = _load_webhooks()
    # One webhook per service is enough — overwrite if exists.
    secret = _uuid_mod.uuid4().hex
    wid = _uuid_mod.uuid4().hex[:12]
    webhooks[wid] = {
        'service_name': service_name,
        'secret': secret,
        'created_at': datetime.now().isoformat(),
        'created_by': _get_username(),
        'last_triggered_at': None,
        'last_triggered_tag': None,
        'trigger_count': 0,
    }
    _save_webhooks(webhooks)
    log_audit(_get_username(), 'docker.webhook_created', f'service={service_name} wid={wid}')
    return {
        'id': wid,
        'secret': secret,
        'service_name': service_name,
        'url_path': f'/api/plugins/{PLUGIN_ID}/api/webhook/trigger?id={wid}&secret={secret}',
        'usage': 'POST <url_path>[&tag=<image-tag>] — triggers `docker service update --image <repo>:<tag> --force`',
    }


def _api_webhook_revoke():
    """POST — Revoke (delete) a webhook. Body: {id} (admin only)."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    wid = data.get('id', '')
    if not isinstance(wid, str) or not re.match(r'^[a-f0-9]{12}$', wid):
        return {'error': 'Valid webhook id required'}, 400

    webhooks = _load_webhooks()
    if wid not in webhooks:
        return {'error': 'Webhook not found'}, 404
    svc = webhooks[wid].get('service_name', '?')
    del webhooks[wid]
    _save_webhooks(webhooks)
    log_audit(_get_username(), 'docker.webhook_revoked', f'service={svc} wid={wid}')
    return {'success': True}


def _api_webhook_trigger():
    """POST — Webhook target. URL: /webhook/trigger?id=<wid>&secret=<secret>&tag=<tag>.

    No PegaProx session required — auth is the secret in the URL. Designed for
    CI/CD systems (GitHub Actions, Drone, etc.). Compares secret with hmac.compare_digest
    to avoid timing attacks. Note: PegaProx may still gate this with `plugins.view`
    permission depending on global config; if so, configure the CI to send the
    PegaProx session token, OR disable the gate for this specific path in nginx.
    """
    wid = request.args.get('id', '')
    secret = request.args.get('secret', '')
    if not isinstance(wid, str) or not re.match(r'^[a-f0-9]{12}$', wid):
        return {'error': 'Invalid webhook'}, 404
    if not isinstance(secret, str) or not re.match(r'^[a-f0-9]{32}$', secret):
        return {'error': 'Invalid webhook'}, 404

    webhooks = _load_webhooks()
    w = webhooks.get(wid)
    if not w:
        log_audit('webhook', 'docker.webhook_unknown', f'wid={wid}')
        return {'error': 'Invalid webhook'}, 404
    if not _hmac.compare_digest(w.get('secret', ''), secret):
        log_audit('webhook', 'docker.webhook_bad_secret', f'wid={wid}')
        return {'error': 'Invalid webhook'}, 404

    service_name = w.get('service_name', '')
    if not _valid(_RX_DOCKER_REF, service_name):
        return {'error': 'Webhook service_name corrupt'}, 500

    tag = request.args.get('tag', '')
    cmd_parts = ['docker', 'service', 'update', '--force']
    if tag:
        # Build new image ref by replacing tag on existing image. We need the current image
        # to keep registry/repo and only swap the tag. Fetch from inspect.
        if not _valid(_RX_IMAGE_REF, tag):
            log_audit('webhook', 'docker.webhook_bad_tag', f'wid={wid} tag={tag[:60]}')
            return {'error': 'Invalid tag'}, 400
        inspect_raw = _docker_cmd(
            f'docker service inspect {shlex.quote(service_name)} '
            f'--format "{{{{.Spec.TaskTemplate.ContainerSpec.Image}}}}"'
        )
        if not inspect_raw:
            return {'error': 'Could not inspect service'}, 500
        current_image = inspect_raw.strip().split('@')[0]  # drop digest
        repo = current_image.rsplit(':', 1)[0] if ':' in current_image else current_image
        new_image = f'{repo}:{tag}'
        if not _valid(_RX_IMAGE_REF, new_image):
            return {'error': 'Computed image ref invalid'}, 400
        cmd_parts += ['--image', shlex.quote(new_image)]
    cmd_parts.append(shlex.quote(service_name))
    cmd = ' '.join(cmd_parts)

    result = _docker_cmd(cmd)
    if result is None:
        log_audit('webhook', 'docker.webhook_failed', f'wid={wid} svc={service_name} tag={tag}')
        return {'error': 'service update failed'}, 500

    # Update trigger stats
    w['last_triggered_at'] = datetime.now().isoformat()
    w['last_triggered_tag'] = tag or None
    w['trigger_count'] = int(w.get('trigger_count', 0)) + 1
    webhooks[wid] = w
    try:
        _save_webhooks(webhooks)
    except Exception as e:
        log.warning(f"[{PLUGIN_ID}] could not persist webhook stats: {e}")

    log_audit('webhook', 'docker.webhook_triggered',
              f'wid={wid} svc={service_name} tag={tag or "(force-only)"}')
    _invalidate('services')
    return {
        'success': True,
        'service_name': service_name,
        'tag': tag or None,
        'message': result,
    }


# ---------------------------------------------------------------------------
# Disk management: manual prune + automatic policy
# ---------------------------------------------------------------------------

# Whitelist de targets válidos → comando Docker + descripción
_PRUNE_TARGETS = {
    'build-cache': ('docker builder prune -a -f', 'Build cache'),
    'images':      ('docker image prune -a -f --filter "until=24h"', 'Imágenes > 24h sin uso'),
    'containers':  ('docker container prune -f', 'Contenedores parados'),
    'volumes':     ('docker volume prune -f', 'Volúmenes huérfanos (¡cuidado!)'),
    'networks':    ('docker network prune -f', 'Redes no usadas'),
    'all-safe':    ('docker builder prune -a -f && docker image prune -a -f --filter "until=24h" && docker container prune -f && docker network prune -f', 'Todo excepto volúmenes (seguro)'),
    'all':         ('docker system prune -a -f --volumes', 'Todo incluyendo volúmenes (DESTRUCTIVO)'),
}


def _disk_prune_node(host_cfg, target):
    """Execute prune command on a specific node. Returns dict with status + freed bytes."""
    cmd_str, _ = _PRUNE_TARGETS.get(target, (None, None))
    if not cmd_str:
        return {'host': host_cfg.get('host'), 'error': f'Unknown target: {target}'}
    # Capturar df antes/después para calcular bytes liberados
    before_cmd = "df -B1 --output=avail / | tail -1"
    out_before, _, _ = _ssh_exec(host_cfg, command=before_cmd, timeout=30)
    try:
        before_bytes = int(out_before.strip())
    except (ValueError, AttributeError):
        before_bytes = 0

    out, err, code = _ssh_exec(host_cfg, command=cmd_str + ' 2>&1', timeout=300)

    out_after, _, _ = _ssh_exec(host_cfg, command=before_cmd, timeout=30)
    try:
        after_bytes = int(out_after.strip())
    except (ValueError, AttributeError):
        after_bytes = 0

    freed = max(0, after_bytes - before_bytes)
    return {
        'host': host_cfg.get('host'),
        'name': host_cfg.get('name', host_cfg.get('host')),
        'target': target,
        'exit_code': code,
        'freed_bytes': freed,
        'output': (out or err or '')[-2000:],
    }


def _api_disk_prune():
    """POST — Prune disk on one or all nodes. Body: {target, node_host?, all_nodes?}"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    target = data.get('target', '')
    if target not in _PRUNE_TARGETS:
        return {'error': f'Invalid target. Valid: {list(_PRUNE_TARGETS.keys())}'}, 400

    cfg = _load_config()
    all_hosts = cfg.get('swarm_hosts', [])
    if not all_hosts:
        return {'error': 'No swarm hosts configured'}, 400

    node_host = data.get('node_host', '')
    all_nodes = bool(data.get('all_nodes', False))

    if all_nodes:
        targets_hosts = all_hosts
    elif node_host:
        targets_hosts = [h for h in all_hosts if h['host'] == node_host]
        if not targets_hosts:
            return {'error': f'Node {node_host} not in config'}, 400
    else:
        return {'error': 'Provide node_host or all_nodes=true'}, 400

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(targets_hosts)), thread_name_prefix='disk-prune') as pool:
        futures = [pool.submit(_disk_prune_node, h, target) for h in targets_hosts]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({'error': str(e)})

    total_freed = sum(r.get('freed_bytes', 0) for r in results)
    log_audit(_get_username(), 'docker.disk_prune',
              f'target={target} nodes={[r.get("host") for r in results]} freed={total_freed} bytes')
    return {
        'success': True,
        'target': target,
        'description': _PRUNE_TARGETS[target][1],
        'results': results,
        'total_freed_bytes': total_freed,
        'total_freed_human': _human_bytes(total_freed),
    }


def _human_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(n) < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def _api_disk_settings():
    """GET/POST — Auto-prune policy. Body (POST): {enabled, threshold_pct, targets, check_interval_min}"""
    cfg = _load_config()
    auto = cfg.get('disk_auto_prune', {
        'enabled': False,
        'threshold_pct': 80,           # dispara cuando disk > 80%
        'targets': ['build-cache', 'images'],  # qué purgar (lista)
        'check_interval_min': 30,      # cada cuántos minutos revisa
        'last_run': None,
        'last_run_freed_bytes': 0,
    })

    if request.method == 'GET':
        return {'disk_auto_prune': auto, 'available_targets': list(_PRUNE_TARGETS.keys())}

    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    # Validación
    new_auto = dict(auto)
    if 'enabled' in data:
        new_auto['enabled'] = bool(data['enabled'])
    if 'threshold_pct' in data:
        try:
            v = int(data['threshold_pct'])
            if not (50 <= v <= 95):
                return {'error': 'threshold_pct must be 50-95'}, 400
            new_auto['threshold_pct'] = v
        except (TypeError, ValueError):
            return {'error': 'threshold_pct must be int'}, 400
    if 'targets' in data:
        tgs = data['targets']
        if not isinstance(tgs, list) or not all(t in _PRUNE_TARGETS for t in tgs):
            return {'error': f'targets must be subset of {list(_PRUNE_TARGETS.keys())}'}, 400
        # 'volumes' y 'all' requieren confirmación explícita en la UI — aquí no los permitimos auto
        if 'volumes' in tgs or 'all' in tgs:
            return {'error': 'volumes/all prohibidos en auto-prune (muy destructivo)'}, 400
        new_auto['targets'] = tgs
    if 'check_interval_min' in data:
        try:
            v = int(data['check_interval_min'])
            if not (5 <= v <= 1440):
                return {'error': 'check_interval_min must be 5-1440'}, 400
            new_auto['check_interval_min'] = v
        except (TypeError, ValueError):
            return {'error': 'check_interval_min must be int'}, 400

    cfg['disk_auto_prune'] = new_auto
    _save_config(cfg)
    log_audit(_get_username(), 'docker.disk_auto_prune_config', f'updated: {new_auto}')
    return {'disk_auto_prune': new_auto, 'success': True}


def _disk_auto_prune_tick():
    """Background tick: check disk % on each node; if > threshold, run auto-prune."""
    cfg = _load_config()
    auto = cfg.get('disk_auto_prune', {})
    if not auto.get('enabled'):
        return
    threshold = auto.get('threshold_pct', 80)
    targets = auto.get('targets', ['build-cache', 'images'])
    hosts = cfg.get('swarm_hosts', [])
    now_iso = datetime.now().isoformat()
    total_freed = 0
    actions = []

    for h in hosts:
        # df -h percent
        out, _, _ = _ssh_exec(h, command="df / | tail -1 | awk '{print $5}' | tr -d '%'", timeout=15)
        try:
            pct = int(out.strip())
        except (ValueError, AttributeError):
            continue
        if pct < threshold:
            continue
        # Sobre threshold → correr targets en orden
        log.info(f"[{PLUGIN_ID}] auto-prune {h['host']}: disk={pct}% > {threshold}%, targets={targets}")
        for t in targets:
            r = _disk_prune_node(h, t)
            total_freed += r.get('freed_bytes', 0)
            actions.append({'host': h['host'], 'target': t, 'freed_bytes': r.get('freed_bytes', 0)})

    if actions:
        cfg['disk_auto_prune']['last_run'] = now_iso
        cfg['disk_auto_prune']['last_run_freed_bytes'] = total_freed
        cfg['disk_auto_prune']['last_run_actions'] = actions
        _save_config(cfg)
        log_audit('auto-prune', 'docker.disk_auto_prune',
                  f'freed={total_freed} actions={len(actions)}')


def _api_disk_auto_prune_run():
    """POST — Trigger auto-prune logic manually (debug/immediate)."""
    err = _require_admin()
    if err:
        return err
    try:
        _disk_auto_prune_tick()
        cfg = _load_config()
        return {'success': True, 'disk_auto_prune': cfg.get('disk_auto_prune', {})}
    except Exception as e:
        return {'error': str(e)}, 500


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(app):
    """Register all plugin routes."""
    routes = {
        'ui': _api_serve_ui,
        'overview': _api_overview,
        'nodes': _api_nodes,
        'node-stats': _api_node_stats,
        'node-action': _api_node_action,
        'services': _api_services,
        'stacks': _api_stacks,
        'containers': _api_containers,
        'networks': _api_networks,
        'volumes': _api_volumes,
        'images': _api_images,
        'tasks': _api_tasks,
        'service-detail': _api_service_detail,
        'service-logs': _api_service_logs,
        'service-scale': _api_service_scale,
        'service-restart': _api_service_restart,
        'service-rollback': _api_service_rollback,
        'service-update': _api_service_update,
        'service-remove': _api_service_remove,
        'container-logs': _api_container_logs,
        'container-action': _api_container_action,
        'stack-detail': _api_stack_detail,
        'stack-compose': _api_stack_compose,
        'stack-logs': _api_stack_logs,
        'stack-stop': _api_stack_stop,
        'stack-start': _api_stack_start,
        'stack-deploy': _api_stack_deploy,
        'stack-remove': _api_stack_remove,
        'topology': _api_topology,
        'image-pull': _api_image_pull,
        'image-remove': _api_image_remove,
        'volume-remove': _api_volume_remove,
        'network-remove': _api_network_remove,
        'load-balance': _api_load_balance,
        'rebalance-service': _api_rebalance_service,
        # Smart Rebalance (v1.14.0 + v1.14.1 async)
        'balance/insights': _api_balance_insights,
        'balance/rebalance-all': _api_balance_rebalance_all,
        'balance/rebalance-status': _api_balance_rebalance_status,
        # Metrics history (v1.13.0 — Phase 3)
        'metrics/history': _api_metrics_history,
        'metrics/trends': _api_metrics_trends,
        # Policy Auditor (v1.11.0 — Phase 1)
        'policy/audit': _api_policy_audit,
        'policy/checks': _api_policy_checks,
        # Policy Applier (v1.12.0 — Phase 2)
        'policy/apply': _api_policy_apply,
        'policy/appliers': _api_policy_appliers,
        # Webhooks (A4 — v1.10.0)
        'webhooks': _api_webhook_list,
        'webhook-create': _api_webhook_create,
        'webhook-revoke': _api_webhook_revoke,
        'webhook/trigger': _api_webhook_trigger,
        'config': _api_get_config,
        'config/save': _api_save_config,
        'test-connection': _api_test_connection,
        'refresh': _api_refresh,
        # Disk management
        'disk/prune': _api_disk_prune,
        'disk/settings': _api_disk_settings,
        'disk/auto-prune/run': _api_disk_auto_prune_run,
    }

    for path, handler in routes.items():
        register_plugin_route(PLUGIN_ID, path, handler)

    log.info(f"[{PLUGIN_ID}] Registered {len(routes)} routes")


def start_background_tasks():
    """Start background polling thread."""
    global _bg_thread
    cfg = _load_config()
    if not cfg.get('swarm_hosts'):
        log.info(f"[{PLUGIN_ID}] No Swarm hosts configured, skipping background poll")
        return

    _bg_stop.clear()
    _bg_thread = threading.Thread(target=_bg_poll, daemon=True, name='docker-swarm-poll')
    _bg_thread.start()

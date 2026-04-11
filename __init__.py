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
import json
import time
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
log = logging.getLogger(f'plugin.{PLUGIN_ID}')

# In-memory cache
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 8  # seconds — short for near-realtime feel

# Background thread
_bg_thread = None
_bg_stop = threading.Event()


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


def _ssh_exec(host, user, password, command, timeout=15):
    """Execute command on remote host via SSH, return (stdout, stderr, exit_code)"""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=22, username=user, password=password,
                       timeout=timeout, banner_timeout=timeout,
                       auth_timeout=timeout, allow_agent=False,
                       look_for_keys=False)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        return out, err, exit_code
    except Exception as e:
        return '', str(e), -1
    finally:
        client.close()


def _docker_cmd(command, host_cfg=None):
    """Run a docker command on the first available Swarm manager.
    Returns parsed JSON or raw string."""
    cfg = _load_config()
    hosts = cfg.get('swarm_hosts', [])
    if host_cfg:
        hosts = [host_cfg]

    for h in hosts:
        out, err, code = _ssh_exec(
            h['host'], h['user'], h['password'], command
        )
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
    """Fetch node details with resource usage."""
    nodes = _docker_json('docker node ls --format "{{json .}}"') or []
    detailed = []

    for node in nodes:
        node_id = node.get('ID', '')
        # Get full node inspect for resources
        inspect = _docker_json(f'docker node inspect {node_id} --format "{{{{json .}}}}"')
        if inspect and isinstance(inspect, list):
            inspect = inspect[0]

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
    """Fetch all services with details."""
    services = _docker_json('docker service ls --format "{{json .}}"') or []
    detailed = []

    for svc in services:
        svc_name = svc.get('Name', svc.get('ID', ''))
        # Inspect for full config — use name (more reliable than short ID)
        inspect = _docker_json(f'docker service inspect {svc_name} --format "{{{{json .}}}}"')
        if inspect and isinstance(inspect, list):
            inspect = inspect[0]

        if inspect:
            spec = inspect.get('Spec', {})
            task_tmpl = spec.get('TaskTemplate', {})
            container_spec = task_tmpl.get('ContainerSpec', {})
            resources_spec = task_tmpl.get('Resources', {})
            endpoint = inspect.get('Endpoint', {})
            mode = spec.get('Mode', {})

            svc['image_full'] = container_spec.get('Image', '')
            svc['env'] = len(container_spec.get('Env', []))
            svc['mounts'] = len(container_spec.get('Mounts', []))
            svc['constraints'] = task_tmpl.get('Placement', {}).get('Constraints', [])
            svc['labels'] = spec.get('Labels', {})
            svc['ports_detail'] = endpoint.get('Ports', [])
            svc['vip'] = [v.get('Addr', '') for v in endpoint.get('VirtualIPs', [])]
            svc['resources_limits'] = resources_spec.get('Limits', {})
            svc['resources_reservations'] = resources_spec.get('Reservations', {})
            svc['created'] = inspect.get('CreatedAt', '')
            svc['updated'] = inspect.get('UpdatedAt', '')
            svc['update_status'] = inspect.get('UpdateStatus', {})

            # Determine if replicated or global
            if 'Replicated' in mode:
                svc['mode_type'] = 'replicated'
                svc['replicas_spec'] = mode['Replicated'].get('Replicas', 0)
            elif 'Global' in mode:
                svc['mode_type'] = 'global'
                svc['replicas_spec'] = 'global'
            else:
                svc['mode_type'] = 'unknown'

            # Get stack name from label
            svc['stack'] = svc['labels'].get('com.docker.stack.namespace', '')

        detailed.append(svc)

    return detailed


def _fetch_stacks():
    """Fetch stacks (docker stack ls)."""
    stacks = _docker_json('docker stack ls --format "{{json .}}"')
    if stacks is None:
        # Fallback: derive stacks from service labels
        services = _docker_json('docker service ls --format "{{json .}}"') or []
        stack_map = {}
        for svc in services:
            inspect = _docker_json(
                f'docker service inspect {svc.get("ID","")} --format "{{{{json .Spec.Labels}}}}"'
            )
            if inspect and isinstance(inspect, dict):
                ns = inspect.get('com.docker.stack.namespace', '')
            elif inspect and isinstance(inspect, list) and inspect:
                ns = inspect[0].get('com.docker.stack.namespace', '') if isinstance(inspect[0], dict) else ''
            else:
                ns = ''
            if ns:
                if ns not in stack_map:
                    stack_map[ns] = {'Name': ns, 'Services': 0}
                stack_map[ns]['Services'] += 1
        return list(stack_map.values())
    return stacks or []


def _fetch_containers():
    """Fetch all containers across the swarm (runs on current node only for now)."""
    containers = _docker_json(
        'docker ps -a --format "{{json .}}"'
    ) or []
    return containers


def _fetch_tasks(service_id):
    """Fetch tasks for a specific service."""
    tasks = _docker_json(
        f'docker service ps {service_id} --format "{{{{json .}}}}" --no-trunc'
    ) or []
    return tasks


def _fetch_networks():
    """Fetch Docker networks."""
    networks = _docker_json('docker network ls --format "{{json .}}"') or []
    return networks


def _fetch_volumes():
    """Fetch Docker volumes."""
    volumes = _docker_json('docker volume ls --format "{{json .}}"') or []
    return volumes


def _fetch_images():
    """Fetch Docker images."""
    images = _docker_json('docker image ls --format "{{json .}}"') or []
    return images


# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

def _bg_poll():
    """Background thread that refreshes cache periodically."""
    cfg = _load_config()
    interval = cfg.get('poll_interval', 30)
    log.info(f"[{PLUGIN_ID}] Background poll started (interval={interval}s)")

    while not _bg_stop.is_set():
        try:
            overview = _fetch_overview()
            _cache_set('overview', overview)

            nodes = _fetch_nodes()
            _cache_set('nodes', nodes)

            services = _fetch_services()
            _cache_set('services', services)

            stacks = _fetch_stacks()
            _cache_set('stacks', stacks)
        except Exception as e:
            log.error(f"[{PLUGIN_ID}] Background poll error: {e}")

        _bg_stop.wait(interval)


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


def _api_tasks():
    """GET — Tasks for a service. ?service_id=xxx"""
    service_id = request.args.get('service_id', '')
    if not service_id:
        return {'error': 'service_id required'}, 400
    tasks = _fetch_tasks(service_id)
    return {'tasks': tasks, 'service_id': service_id}


def _api_service_logs():
    """GET — Logs for a service. ?service_id=xxx&tail=100"""
    service_id = request.args.get('service_id', '')
    tail = request.args.get('tail', '100')
    if not service_id:
        return {'error': 'service_id required'}, 400

    # Sanitize service_id to prevent injection
    if not all(c.isalnum() or c in '-_.' for c in service_id):
        return {'error': 'Invalid service_id'}, 400

    try:
        tail = min(int(tail), 1000)
    except ValueError:
        tail = 100

    logs = _docker_cmd(f'docker service logs --tail {tail} --no-trunc {service_id} 2>&1')
    return {'logs': logs or '', 'service_id': service_id, 'tail': tail}


def _api_container_logs():
    """GET — Logs for a container. ?container_id=xxx&tail=100"""
    container_id = request.args.get('container_id', '')
    tail = request.args.get('tail', '100')
    if not container_id:
        return {'error': 'container_id required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in container_id):
        return {'error': 'Invalid container_id'}, 400

    try:
        tail = min(int(tail), 1000)
    except ValueError:
        tail = 100

    logs = _docker_cmd(f'docker logs --tail {tail} {container_id} 2>&1')
    return {'logs': logs or '', 'container_id': container_id, 'tail': tail}


def _api_service_scale():
    """POST — Scale a service. Body: {service_id, replicas}"""
    data = request.get_json() or {}
    service_id = data.get('service_id', '')
    replicas = data.get('replicas')

    if not service_id or replicas is None:
        return {'error': 'service_id and replicas required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in service_id):
        return {'error': 'Invalid service_id'}, 400

    try:
        replicas = int(replicas)
        if replicas < 0 or replicas > 100:
            return {'error': 'Replicas must be 0-100'}, 400
    except ValueError:
        return {'error': 'Replicas must be integer'}, 400

    result = _docker_cmd(f'docker service scale {service_id}={replicas}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_scaled',
                  f'Scaled {service_id} to {replicas} replicas')
        # Invalidate cache
        with _cache_lock:
            _cache.pop('services', None)
            _cache.pop('overview', None)
        return {'success': True, 'message': result}
    return {'error': 'Scale command failed'}, 500


def _api_service_restart():
    """POST — Force update (restart) a service. Body: {service_id}"""
    data = request.get_json() or {}
    service_id = data.get('service_id', '')

    if not service_id:
        return {'error': 'service_id required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in service_id):
        return {'error': 'Invalid service_id'}, 400

    result = _docker_cmd(f'docker service update --force {service_id}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_restarted',
                  f'Force-updated service {service_id}')
        with _cache_lock:
            _cache.pop('services', None)
        return {'success': True, 'message': f'Service {service_id} force-updated'}
    return {'error': 'Restart command failed'}, 500


def _api_service_remove():
    """POST — Remove a service. Body: {service_id}"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    service_id = data.get('service_id', '')

    if not service_id:
        return {'error': 'service_id required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in service_id):
        return {'error': 'Invalid service_id'}, 400

    result = _docker_cmd(f'docker service rm {service_id}')
    if result is not None:
        log_audit(_get_username(), 'docker.service_removed',
                  f'Removed service {service_id}')
        with _cache_lock:
            _cache.pop('services', None)
            _cache.pop('stacks', None)
            _cache.pop('overview', None)
        return {'success': True, 'message': f'Service {service_id} removed'}
    return {'error': 'Remove command failed'}, 500


def _api_stack_deploy():
    """POST — Deploy/update a stack. Body: {stack_name, compose_yaml}"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')
    compose = data.get('compose_yaml', '')

    if not stack_name or not compose:
        return {'error': 'stack_name and compose_yaml required'}, 400

    if not all(c.isalnum() or c in '-_' for c in stack_name):
        return {'error': 'Invalid stack_name'}, 400

    # Write compose to temp file on remote, deploy, cleanup
    import base64
    b64 = base64.b64encode(compose.encode()).decode()
    cmd = (
        f'echo "{b64}" | base64 -d > /tmp/_pegaprox_stack_{stack_name}.yml && '
        f'docker stack deploy -c /tmp/_pegaprox_stack_{stack_name}.yml {stack_name} && '
        f'rm -f /tmp/_pegaprox_stack_{stack_name}.yml'
    )
    result = _docker_cmd(cmd)
    if result is not None:
        log_audit(_get_username(), 'docker.stack_deployed', f'Deployed stack {stack_name}')
        with _cache_lock:
            _cache.clear()
        return {'success': True, 'message': result}
    return {'error': 'Stack deploy failed'}, 500


def _api_stack_detail():
    """GET — Detailed info for a stack. ?name=xxx"""
    name = request.args.get('name', '')
    if not name or not all(c.isalnum() or c in '-_' for c in name):
        return {'error': 'Valid stack name required'}, 400

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
            svc['env_vars'] = container_spec.get('Env', [])
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
    """GET — Get the compose/config for a stack (reconstructed). ?name=xxx"""
    name = request.args.get('name', '')
    if not name or not all(c.isalnum() or c in '-_' for c in name):
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
    if not name or not all(c.isalnum() or c in '-_' for c in name):
        return {'error': 'Valid stack name required'}, 400

    try:
        tail = min(int(tail), 500)
    except ValueError:
        tail = 50

    # Get services in this stack
    services = _docker_json(
        f'docker service ls --filter label=com.docker.stack.namespace={name} --format "{{{{json .}}}}"'
    ) or []

    all_logs = []
    for svc in services:
        svc_name = svc.get('Name', '')
        logs = _docker_cmd(f'docker service logs --tail {tail} --no-trunc {svc_name} 2>&1')
        if logs:
            all_logs.append(f'=== {svc_name} ===\n{logs}')

    return {'logs': '\n\n'.join(all_logs), 'stack': name, 'services': len(services)}


def _api_stack_remove():
    """POST — Remove a stack. Body: {stack_name}"""
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    stack_name = data.get('stack_name', '')

    if not stack_name:
        return {'error': 'stack_name required'}, 400

    if not all(c.isalnum() or c in '-_' for c in stack_name):
        return {'error': 'Invalid stack_name'}, 400

    result = _docker_cmd(f'docker stack rm {stack_name}')
    if result is not None:
        log_audit(_get_username(), 'docker.stack_removed', f'Removed stack {stack_name}')
        with _cache_lock:
            _cache.clear()
        return {'success': True, 'message': f'Stack {stack_name} removed'}
    return {'error': 'Stack remove failed'}, 500


def _api_container_action():
    """POST — Container action. Body: {container_id, action: start|stop|restart}"""
    data = request.get_json() or {}
    container_id = data.get('container_id', '')
    action = data.get('action', '')

    if not container_id or action not in ('start', 'stop', 'restart'):
        return {'error': 'container_id and action (start/stop/restart) required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in container_id):
        return {'error': 'Invalid container_id'}, 400

    result = _docker_cmd(f'docker {action} {container_id}')
    if result is not None:
        log_audit(_get_username(), f'docker.container_{action}',
                  f'{action} container {container_id}')
        return {'success': True, 'message': f'Container {container_id} {action}ed'}
    return {'error': f'{action} failed'}, 500


def _api_node_action():
    """POST — Node action. Body: {node_id, action: drain|active|pause}"""
    data = request.get_json() or {}
    node_id = data.get('node_id', '')
    action = data.get('action', '')

    if not node_id or action not in ('drain', 'active', 'pause'):
        return {'error': 'node_id and action (drain/active/pause) required'}, 400

    if not all(c.isalnum() or c in '-_.' for c in node_id):
        return {'error': 'Invalid node_id'}, 400

    result = _docker_cmd(f'docker node update --availability {action} {node_id}')
    if result is not None:
        log_audit(_get_username(), f'docker.node_{action}',
                  f'Set node {node_id} to {action}')
        with _cache_lock:
            _cache.pop('nodes', None)
        return {'success': True, 'message': f'Node {node_id} set to {action}'}
    return {'error': f'Node update failed'}, 500


def _api_get_config():
    """GET — Return plugin config (admin only, masks password)."""
    err = _require_admin()
    if err:
        return err
    cfg = _load_config()
    # Mask passwords
    safe_hosts = []
    for h in cfg.get('swarm_hosts', []):
        safe = dict(h)
        if safe.get('password'):
            safe['password'] = '***'
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

    return {'success': True}


def _api_test_connection():
    """POST — Test SSH connection to a host. Body: {host, user, password}"""
    data = request.get_json() or {}
    host = data.get('host', '')
    user = data.get('user', '')
    password = data.get('password', '')

    if not host or not user or not password:
        return {'error': 'host, user, password required'}, 400

    host_cfg = {'host': host, 'user': user, 'password': password}
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


def _api_refresh():
    """POST — Force refresh all cached data."""
    with _cache_lock:
        _cache.clear()
    return {'success': True, 'message': 'Cache cleared, next request will fetch fresh data'}


def _api_node_stats():
    """GET — Get resource stats for each node (CPU/RAM via SSH)."""
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
        out, err, code = _ssh_exec(h['host'], h['user'], h['password'], cmd)
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
        'service-logs': _api_service_logs,
        'service-scale': _api_service_scale,
        'service-restart': _api_service_restart,
        'service-remove': _api_service_remove,
        'container-logs': _api_container_logs,
        'container-action': _api_container_action,
        'stack-detail': _api_stack_detail,
        'stack-compose': _api_stack_compose,
        'stack-logs': _api_stack_logs,
        'stack-deploy': _api_stack_deploy,
        'stack-remove': _api_stack_remove,
        'config': _api_get_config,
        'config/save': _api_save_config,
        'test-connection': _api_test_connection,
        'refresh': _api_refresh,
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

"""Test bootstrap.

Loads `__init__.py` as an importable module under the name `docker_swarm` while
stubbing the PegaProx host imports it relies on. This lets the security-critical
helpers (regex validators, env masking) be unit-tested in CI without a live
PegaProx install.
"""
import importlib.util
import os
import sys
import types


def _stub(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# Stub the PegaProx host modules touched at import time.
_stub("pegaprox")
_stub("pegaprox.api")
_pl = _stub("pegaprox.api.plugins")
_pl.register_plugin_route = lambda *a, **kw: None
_au = _stub("pegaprox.utils")
_stub("pegaprox.utils")
_auth = _stub("pegaprox.utils.auth")
_auth.load_users = lambda: {}
_audit = _stub("pegaprox.utils.audit")
_audit.log_audit = lambda *a, **kw: None
_perms = _stub("pegaprox.models")
_stub("pegaprox.models")
_perm_mod = _stub("pegaprox.models.permissions")
_perm_mod.ROLE_ADMIN = "admin"

# Stub flask just enough — __init__.py only imports symbols at module level.
_flask = _stub("flask")
_flask.request = types.SimpleNamespace(session={}, args={}, json={})
_flask.jsonify = lambda *a, **kw: ("json", a, kw)
_flask.send_file = lambda *a, **kw: ("file", a, kw)


PLUGIN_INIT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "__init__.py",
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("docker_swarm", PLUGIN_INIT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["docker_swarm"] = mod
    spec.loader.exec_module(mod)
    return mod


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def plugin():
    return _load_plugin()

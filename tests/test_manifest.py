"""Smoke test for manifest.json — fields required by PegaProx 0.9.9.3+."""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def test_manifest_has_required_fields():
    for field in ("name", "version", "author", "description",
                  "min_pegaprox", "license", "has_frontend", "frontend_route"):
        assert field in MANIFEST, f"missing manifest field: {field}"


def test_min_pegaprox_at_least_0_9_9_3():
    parts = tuple(int(x) for x in MANIFEST["min_pegaprox"].split(".") if x.isdigit())
    assert parts >= (0, 9, 9, 3), \
        f"min_pegaprox must be >= 0.9.9.3 (native plugin frontend hook), got {parts}"


def test_has_frontend_true():
    assert MANIFEST["has_frontend"] is True


def test_frontend_route_matches_register():
    # Plugin registers /api/plugins/docker_swarm/api/<route>; manifest must
    # declare the same route (relative form 'ui' or absolute '/api/plugins/docker_swarm/api/ui').
    route = MANIFEST["frontend_route"]
    assert route == "ui" or route.endswith("/api/ui"), \
        f"frontend_route must be 'ui' or '/api/plugins/docker_swarm/api/ui', got {route!r}"


def test_version_semver():
    parts = MANIFEST["version"].split(".")
    assert len(parts) == 3, f"version must be MAJOR.MINOR.PATCH, got {MANIFEST['version']!r}"
    assert all(p.isdigit() for p in parts), MANIFEST["version"]

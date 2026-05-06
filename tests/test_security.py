"""Security-critical unit tests.

Covers the input validators and the sensitive-env masking — the two
defences against shell command injection and credential leakage that
v1.9.4 introduced. A regression here turns straight into a CVE, so
these are guarded explicitly rather than left implicit in code review.
"""
import pytest


# ---------------------------------------------------------------------------
# _RX_DOCKER_REF — used to gate every service/container/volume/network id
# before it lands inside an SSH-shelled `docker …` command.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    "myservice",
    "myservice.123",
    "stack_web-1",
    "abc123_-.test",
    "a" * 255,
])
def test_docker_ref_accepts_valid(plugin, ref):
    assert plugin._valid(plugin._RX_DOCKER_REF, ref)


@pytest.mark.parametrize("ref", [
    "",
    "_leading_underscore",
    ".leading_dot",
    "-leading_dash",
    "name with space",
    "name;rm -rf /",
    "name`whoami`",
    "name$(id)",
    "name|cat",
    "name\nrm",
    "name&id",
    "a" * 256,                        # over length cap
    "name'; DROP TABLE",
    None,
    123,
    # Trailing newline — Python's `$` lets these slip past unless the
    # regex anchors with \Z. Regression-guarded after the v1.15.0 fix.
    "name\n",
    "name\r",
    "name\x00",
])
def test_docker_ref_rejects_injection(plugin, ref):
    assert not plugin._valid(plugin._RX_DOCKER_REF, ref)


# ---------------------------------------------------------------------------
# _RX_STACK_NAME — stricter, used in filenames + docker-stack ops.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "idkpuntual",
    "idk-microtk",
    "stack_name_42",
    "a" * 63,
])
def test_stack_name_accepts_valid(plugin, name):
    assert plugin._valid(plugin._RX_STACK_NAME, name)


@pytest.mark.parametrize("name", [
    "",
    ".dotleading",
    "stack.with.dot",         # dots disallowed in stack names
    "stack/slash",
    "stack;evil",
    "a" * 64,
    "../etc/passwd",
])
def test_stack_name_rejects_invalid(plugin, name):
    assert not plugin._valid(plugin._RX_STACK_NAME, name)


# ---------------------------------------------------------------------------
# _RX_IMAGE_REF — registry/path:tag@sha256:… broad but no shell metas.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("img", [
    "alpine",
    "alpine:3.19",
    "ghcr.io/org/repo:1.2.3",
    "registry.example.com:5000/team/app:v1@sha256:abc123",
    "nginx:1.27.0-alpine",
])
def test_image_ref_accepts_valid(plugin, img):
    assert plugin._valid(plugin._RX_IMAGE_REF, img)


@pytest.mark.parametrize("img", [
    "img; rm -rf /",
    "img`whoami`",
    "img$(id)",
    "img|cat /etc/passwd",
    "img'evil",
    "img\nname",
    "",
    "_underscore_lead",
])
def test_image_ref_rejects_injection(plugin, img):
    assert not plugin._valid(plugin._RX_IMAGE_REF, img)


# ---------------------------------------------------------------------------
# _RX_RESOURCE — CPU / memory limit values.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v", ["0.5", "1", "512m", "2G", "1.5GiB"])
def test_resource_accepts_valid(plugin, v):
    assert plugin._valid(plugin._RX_RESOURCE, v)


@pytest.mark.parametrize("v", ["1g; rm", "$(id)", "1G|cat", "abc", "", "1\n"])
def test_resource_rejects_invalid(plugin, v):
    assert not plugin._valid(plugin._RX_RESOURCE, v)


# ---------------------------------------------------------------------------
# _RX_ENV_ENTRY — KEY=value where value can be anything except newline/null.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("e", [
    "FOO=bar",
    "FOO=",
    "FOO",
    "DB_URL=postgres://u:p@h:5432/d",
    "_FOO=bar",
    "FOO=value with spaces and 'quotes' and \"more\"",
])
def test_env_entry_accepts_valid(plugin, e):
    assert plugin._valid(plugin._RX_ENV_ENTRY, e)


@pytest.mark.parametrize("e", [
    "1FOO=bar",                       # key cannot start with digit
    "-FOO=bar",
    "FOO\n=bar",                      # newline in key
    "FOO=line1\nline2",               # newline in value
    "FOO=null\x00byte",               # null byte
    "",
])
def test_env_entry_rejects_invalid(plugin, e):
    assert not plugin._valid(plugin._RX_ENV_ENTRY, e)


# ---------------------------------------------------------------------------
# _mask_env_list — server-side masking of credentials before they reach
# the wire. Regression here means DevTools shows real secrets.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _RX_HOSTNAME and _RX_USERNAME — gate /test-connection inputs.
# These were also missing the \Z anchor (caught in the v1.15.0 audit
# pass after the first batch of regex fixes). Tests pin both.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("h", [
    "manager-1",
    "swarm.example.com",
    "ias01.idkmanager.local",
    "192.168.1.10",
    "10.0.0.1",
])
def test_hostname_accepts_valid(plugin, h):
    assert plugin._valid(plugin._RX_HOSTNAME, h)


@pytest.mark.parametrize("h", [
    "host\n",
    "host\r",
    "host;evil",
    "host with space",
    "999.999.999.999\n",
    "",
    "host`whoami`",
])
def test_hostname_rejects_invalid(plugin, h):
    assert not plugin._valid(plugin._RX_HOSTNAME, h)


@pytest.mark.parametrize("u", ["alfonso", "_root", "ci-bot", "user_42"])
def test_username_accepts_valid(plugin, u):
    assert plugin._valid(plugin._RX_USERNAME, u)


@pytest.mark.parametrize("u", [
    "user\n",
    "1user",
    "-user",
    "user; rm",
    "a" * 33,
    "",
])
def test_username_rejects_invalid(plugin, u):
    assert not plugin._valid(plugin._RX_USERNAME, u)


class TestMaskEnvList:
    def test_masks_password_key(self, plugin):
        out = plugin._mask_env_list(["DB_PASSWORD=hunter2"])
        assert out == ["DB_PASSWORD=***"]

    def test_masks_case_insensitive(self, plugin):
        out = plugin._mask_env_list([
            "Password=a", "API_KEY=b", "jwt_TOKEN=c", "MY_SECRET=d",
        ])
        assert out == [
            "Password=***", "API_KEY=***", "jwt_TOKEN=***", "MY_SECRET=***",
        ]

    @pytest.mark.parametrize("k", [
        "password", "secret", "token", "apikey", "api_key", "jwt",
        "bearer", "auth", "private", "credential", "dsn", "passwd",
        "passphrase",
    ])
    def test_each_sensitive_keyword_triggers_mask(self, plugin, k):
        out = plugin._mask_env_list([f"X_{k}_X=plain"])
        assert out[0].endswith("=***"), f"keyword {k!r} did not trigger masking"

    def test_keeps_non_sensitive(self, plugin):
        out = plugin._mask_env_list(["LOG_LEVEL=info", "PORT=8080"])
        assert out == ["LOG_LEVEL=info", "PORT=8080"]

    def test_unmask_returns_original(self, plugin):
        envs = ["DB_PASSWORD=hunter2", "FOO=bar"]
        assert plugin._mask_env_list(envs, unmask=True) == envs

    def test_handles_value_with_equals(self, plugin):
        out = plugin._mask_env_list(["DB_DSN=postgres://u:p=q@h/d"])
        # Sensitive key → masked regardless of inner =
        assert out == ["DB_DSN=***"]

    def test_handles_empty_list(self, plugin):
        assert plugin._mask_env_list([]) == []

    def test_passes_through_malformed_entries(self, plugin):
        out = plugin._mask_env_list(["NOEQUALS", None, 42])
        assert out == ["NOEQUALS", None, 42]

    def test_no_partial_match_in_unrelated_keys(self, plugin):
        # "auth" appears in "AUTHOR" — but auth IS in the keyword list,
        # so this should mask. Document the intentional over-mask behaviour
        # so anyone changing the regex sees it.
        out = plugin._mask_env_list(["AUTHOR=alfonso"])
        assert out == ["AUTHOR=***"], (
            "AUTHOR matches /auth/ — over-masking is intentional; "
            "if changed, update _RX_SENSITIVE_ENV with word boundaries"
        )

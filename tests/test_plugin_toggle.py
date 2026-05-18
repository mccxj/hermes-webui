"""
Tests for plugin enable/disable toggle (POST /api/plugins/toggle).

Verifies:
- POST /api/plugins/toggle with valid name and enabled=true enables a plugin
- POST /api/plugins/toggle with valid name and enabled=false disables a plugin
- POST /api/plugins/toggle with missing fields returns 400
- POST /api/plugins/toggle with non-existent plugin returns error
- GET /api/plugins returns read_only: False
- Idempotent toggles return unchanged: True

Note: toggling writes to config.yaml but the in-memory PluginManager state
is not refreshed until next session start (matching CLI behavior:
"Takes effect on next session"). Tests verify POST response, not GET state.
"""
import json, urllib.error, urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── GET /api/plugins no longer read-only ──────────────────────────

def test_plugins_get_not_read_only():
    """GET /api/plugins should return read_only: False now that toggle is available."""
    data = get("/api/plugins")
    assert "read_only" in data
    assert data["read_only"] is False, f"Expected read_only=False, got {data['read_only']}"


# ── POST /api/plugins/toggle validation ───────────────────────────

def test_plugins_toggle_requires_name():
    """POST /api/plugins/toggle without name returns 400."""
    data, status = post("/api/plugins/toggle", {"enabled": True})
    assert status == 400


def test_plugins_toggle_requires_enabled():
    """POST /api/plugins/toggle without enabled returns 400."""
    data, status = post("/api/plugins/toggle", {"name": "some_plugin"})
    assert status == 400


def test_plugins_toggle_nonexistent_plugin():
    """POST /api/plugins/toggle with non-existent plugin returns error."""
    data, status = post("/api/plugins/toggle", {"name": "zzz_nonexistent_plugin_xyz", "enabled": True})
    assert status == 400 or (status == 200 and data.get("ok") is False), \
        f"Expected 400 or ok=False for non-existent plugin, got {status}: {data}"


# ── POST /api/plugins/toggle roundtrip ────────────────────────────

def test_plugins_toggle_enable_returns_ok():
    """Enabling a known plugin returns {ok: True, name: ..., unchanged: bool}."""
    data = get("/api/plugins")
    plugins = data.get("plugins", [])
    if not plugins:
        return

    target = plugins[0]
    plugin_key = target["key"]

    # Enable it
    result, status = post("/api/plugins/toggle", {"name": plugin_key, "enabled": True})
    assert status == 200, f"Toggle enable failed: {status} {result}"
    assert result.get("ok") is True, f"Toggle enable returned ok=False: {result}"
    assert result.get("name") == plugin_key

    # Restore: set back to original state
    post("/api/plugins/toggle", {"name": plugin_key, "enabled": target.get("enabled", True)})


def test_plugins_toggle_disable_returns_ok():
    """Disabling a known plugin returns {ok: True, name: ..., unchanged: bool}."""
    data = get("/api/plugins")
    plugins = data.get("plugins", [])
    if not plugins:
        return

    target = plugins[0]
    plugin_key = target["key"]

    # Disable it
    result, status = post("/api/plugins/toggle", {"name": plugin_key, "enabled": False})
    assert status == 200, f"Toggle disable failed: {status} {result}"
    assert result.get("ok") is True, f"Toggle disable returned ok=False: {result}"
    assert result.get("name") == plugin_key

    # Restore
    post("/api/plugins/toggle", {"name": plugin_key, "enabled": target.get("enabled", True)})


def test_plugins_toggle_idempotent():
    """Toggling a plugin to its current state should return unchanged: True."""
    data = get("/api/plugins")
    plugins = data.get("plugins", [])
    if not plugins:
        return

    target = plugins[0]
    plugin_key = target["key"]
    current_state = target["enabled"]

    # First toggle to a known state (enable)
    result, status = post("/api/plugins/toggle", {"name": plugin_key, "enabled": True})
    assert status == 200

    # Toggle again to same state (should be idempotent)
    result, status = post("/api/plugins/toggle", {"name": plugin_key, "enabled": True})
    assert status == 200
    assert result.get("ok") is True
    assert result.get("unchanged") is True, f"Expected unchanged=True for idempotent toggle, got {result}"

    # Restore
    post("/api/plugins/toggle", {"name": plugin_key, "enabled": current_state})


def test_plugins_toggle_enabled_must_be_bool():
    """POST /api/plugins/toggle with non-boolean enabled returns 400."""
    data, status = post("/api/plugins/toggle", {"name": "some_plugin", "enabled": "yes"})
    assert status == 400

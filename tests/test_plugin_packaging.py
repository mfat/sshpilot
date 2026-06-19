"""Guard: every shipped plugin (built-in or example) that has a plugin.json must
be declared in pyproject's [tool.setuptools.package-data], or it would be missing
from the wheel (setuptools doesn't glob package-data keys). Catches the common
"added a built-in but forgot the packaging entry" mistake."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

tomllib = pytest.importorskip("tomllib")  # py3.11+

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _package_data():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["setuptools"]["package-data"]


def _plugin_dirs():
    """(package_key, has_plugin_json) for each plugin dir under builtin/examples."""
    out = []
    for tier in ("builtin", "examples"):
        base = os.path.join(ROOT, "sshpilot", "plugins", tier)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if name.startswith("__") or not os.path.isdir(d):
                continue
            if os.path.isfile(os.path.join(d, "plugin.json")):
                out.append((f"sshpilot.plugins.{tier}.{name}", d))
    return out


def test_every_plugin_is_declared_in_package_data():
    pkg_data = _package_data()
    missing = []
    for key, _d in _plugin_dirs():
        entries = pkg_data.get(key)
        if not entries or "plugin.json" not in entries:
            missing.append(key)
    assert not missing, (
        "plugin.json not declared in [tool.setuptools.package-data] for: "
        + ", ".join(missing))


def test_found_at_least_the_known_builtins():
    keys = {k for k, _ in _plugin_dirs()}
    for proto in ("ssh", "telnet", "serial", "docker", "kubernetes", "mosh"):
        assert f"sshpilot.plugins.builtin.{proto}_protocol" in keys


def test_loader_parses_permissions():
    from sshpilot.plugins.loader import discover_plugins
    infos = {i.plugin_id: i for i in discover_plugins()}
    assert infos["ssh"].permissions == ["process"]
    assert infos["mosh"].permissions == ["process", "network"]


def test_shipped_manifests_match_schema():
    import json
    jsonschema = pytest.importorskip("jsonschema")
    with open(os.path.join(ROOT, "docs", "plugins", "plugin.schema.json")) as fh:
        schema = json.load(fh)
    for _key, d in _plugin_dirs():
        with open(os.path.join(d, "plugin.json")) as fh:
            manifest = json.load(fh)
        jsonschema.validate(manifest, schema)  # raises on mismatch

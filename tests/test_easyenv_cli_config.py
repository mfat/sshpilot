"""The easyenv CLI's config file."""

import os
import stat

import pytest

import importlib.util as _ilu
import os as _os
import sys as _sys

from sshpilot.plugins import registry as _registry

# The plugin ships as an example beside the SDK; its modules are loaded from
# there, the way sshPilot's loader loads a user plugin.
_ROOT = _os.path.join(_os.path.dirname(_registry.__file__), "examples", "easyenv_workspaces")


def load(name):
    spec = _ilu.spec_from_file_location(f"easyenv_example_{name}",
                                        _os.path.join(_ROOT, f"{name}.py"))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    package = "sshpilot_easyenv_example_under_test"
    if package in _sys.modules:
        return _sys.modules[package]
    spec = _ilu.spec_from_file_location(package, _os.path.join(_ROOT, "__init__.py"),
                                        submodule_search_locations=[_ROOT])
    module = _ilu.module_from_spec(spec)
    _sys.modules[package] = module
    spec.loader.exec_module(module)
    return module

cfg = load("cli_config")


def test_the_config_is_the_clis_file_and_readable_by_it(tmp_path):
    path = str(tmp_path / "easyenv" / "config.yaml")
    cfg.write_config({"server": "https://api.easyenv.io", "service_token": 'a"b',
                     "default_account": "acct"}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    values = cfg.read_config(path)
    assert values["service_token"] == 'a"b'
    assert values["default_account"] == "acct"


def test_writing_keeps_what_the_cli_put_there(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("server: https://staging.example\noutput: json\n"
                    "watch_interval: 2s\nservice_token: old\n")
    cfg.write_config({"service_token": "new"}, str(path))
    text = path.read_text()
    assert "output: json" in text and "watch_interval: 2s" in text
    assert cfg.read_config(str(path))["service_token"] == "new"
    assert cfg.read_config(str(path))["server"] == "https://staging.example"


def test_signing_out_removes_the_token_and_keeps_the_rest(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('server: "https://x"\nservice_token: "t"\n')
    cfg.write_config({"service_token": None}, str(path))
    assert "service_token" not in path.read_text()
    assert cfg.read_config(str(path))["server"] == "https://x"


def test_environment_overrides_the_file_as_the_cli_does(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("service_token: from-file\n")
    monkeypatch.setenv("EASYENV_TOKEN", "from-env")
    assert cfg.read_config(str(path))["service_token"] == "from-env"


def test_a_missing_file_means_the_default_server(tmp_path):
    values = cfg.read_config(str(tmp_path / "absent.yaml"))
    assert values["server"] == cfg.DEFAULT_SERVER
    assert not values.get("service_token")


@pytest.mark.parametrize("line,value", [
    ("service_token: plain", "plain"),
    ('service_token: "quoted # not a comment"', "quoted # not a comment"),
    ("service_token: 'single ''quoted'''", "single 'quoted'"),
    ("service_token: bare # a comment", "bare"),
])
def test_yaml_scalars_the_go_encoder_writes(tmp_path, line, value):
    path = tmp_path / "config.yaml"
    path.write_text(line + "\n")
    assert cfg.read_config(str(path))["service_token"] == value

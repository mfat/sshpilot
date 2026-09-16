"""Recipe logos on disk."""

import io

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

logos = load("logos")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_logo_is_downloaded_once_and_then_read_from_disk(tmp_path):
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        return Resp(b"<svg/>")

    url = "https://static.easyenv.io/recipe/base/ubuntu.svg"
    path = logos.fetch(url, str(tmp_path), opener)
    assert path.endswith(".svg") and open(path, "rb").read() == b"<svg/>"
    assert logos.fetch(url, str(tmp_path), opener) == path
    assert calls == [url]


def test_only_web_urls_and_small_files(tmp_path):
    def big(req, timeout):
        return Resp(b"x" * (logos.MAX_BYTES + 1))

    def broken(req, timeout):
        raise OSError("offline")

    assert logos.fetch("file:///etc/passwd", str(tmp_path)) is None
    assert logos.fetch("https://h/big.svg", str(tmp_path), big) is None
    assert logos.fetch("https://h/a.svg", str(tmp_path), broken) is None


def test_the_loader_asks_once_however_many_cards_want_it(tmp_path, monkeypatch):
    got, calls, pending = [], [], []

    class Held:
        def __init__(self, target, args, daemon, name):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

    def opener(req, timeout):
        calls.append(1)
        return Resp(b"<svg/>")

    monkeypatch.setattr(logos.threading, "Thread", Held)
    loader = logos.Loader(lambda cb, path: cb(path), str(tmp_path), opener)
    loader.get("https://h/a.svg", got.append)
    loader.get("https://h/a.svg", got.append)
    assert len(pending) == 1
    pending[0].target(*pending[0].args)
    loader.get("https://h/a.svg", got.append)
    assert len(got) == 3 and len(set(got)) == 1 and calls == [1]

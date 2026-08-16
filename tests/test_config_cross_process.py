"""Cross-process regression coverage for frontend/daemon settings writes."""

from __future__ import annotations

import copy
import json
import multiprocessing

from sshpilot.config import Config


def _write_preference(path, key, value, barrier):
    # Construct only the non-GTK state needed by Config.save_json_config. Both
    # workers intentionally load before either writes, reproducing the stale
    # frontend cache/daemon mutation sequence.
    config = Config.__new__(Config)
    config.config_file = str(path)
    config.config_data = json.loads(path.read_text(encoding="utf-8"))
    config._config_snapshot = copy.deepcopy(config.config_data)
    barrier.wait(5)
    current = config.config_data
    section, name = key.split(".", 1)
    current.setdefault(section, {})[name] = value
    if not config.save_json_config():
        raise AssertionError("cross-process settings save failed")


def test_stale_frontend_tree_cannot_delete_daemon_setting(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "config_version": 3,
                "plugins": {},
                "ui": {"language": ""},
            }
        ),
        encoding="utf-8",
    )
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    workers = [
        context.Process(
            target=_write_preference,
            args=(path, "ui.language", "fa", barrier),
        ),
        context.Process(
            target=_write_preference,
            args=(path, "plugins.docker_enabled", True, barrier),
        ),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["ui"]["language"] == "fa"
    assert result["plugins"]["docker_enabled"] is True

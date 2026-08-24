"""A stored "detach" tab-close policy is retired once, then left alone.

Detaching orphaned a RUNNING daemon session per closed tab and kept the host
showing as connected (GH #1176), so existing configs carrying it are rewritten
to terminate. The rewrite is one-shot: detach is no longer offered in
Preferences, but it stays a supported value for anyone who sets it again by
hand, and a later launch must not undo that choice.
"""

from __future__ import annotations

import json

from sshpilot.config import CONFIG_VERSION, Config
from sshpilot.core.settings.migration import ensure_config_defaults
from sshpilot.daemon_terminal_policy import (
    TerminalClosePolicy,
    resolve_tab_close_policy,
)


class FakeConfig:
    """Reads dotted keys out of a nested config tree, like Config does."""

    def __init__(self, tree):
        self.tree = tree

    def get_setting(self, key, default=None):
        node = self.tree
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _policy_of(config):
    return resolve_tab_close_policy(FakeConfig(config))


def test_stored_detach_is_rewritten_to_terminate():
    config, updated = ensure_config_defaults(
        {'terminal': {'daemon_tab_close_policy': 'detach'}}
    )

    assert updated is True
    assert config['terminal']['daemon_tab_close_policy'] == 'terminate'
    assert _policy_of(config) is TerminalClosePolicy.TERMINATE


def test_migration_runs_only_once_so_a_deliberate_detach_survives():
    config, _ = ensure_config_defaults(
        {'terminal': {'daemon_tab_close_policy': 'detach'}}
    )
    assert config['terminal']['daemon_tab_close_policy'] == 'terminate'

    # The user sets it back by hand; the next launch must leave it alone.
    config['terminal']['daemon_tab_close_policy'] = 'detach'
    config, _ = ensure_config_defaults(config)

    assert config['terminal']['daemon_tab_close_policy'] == 'detach'
    assert _policy_of(config) is TerminalClosePolicy.DETACH


def test_other_stored_policies_are_untouched():
    for policy in ('terminate', 'ask'):
        config, _ = ensure_config_defaults(
            {'terminal': {'daemon_tab_close_policy': policy}}
        )
        assert config['terminal']['daemon_tab_close_policy'] == policy


def test_config_without_the_key_still_terminates():
    config, _ = ensure_config_defaults({'terminal': {}})

    assert 'daemon_tab_close_policy' not in config['terminal']
    assert _policy_of(config) is TerminalClosePolicy.TERMINATE


def test_migration_is_marked_even_when_nothing_was_stored():
    """Otherwise a later hand-set detach would be rewritten on the next launch."""
    config, _ = ensure_config_defaults({'terminal': {}})
    assert config['terminal']['daemon_tab_close_policy_migrated'] is True

    config['terminal']['daemon_tab_close_policy'] = 'detach'
    config, _ = ensure_config_defaults(config)

    assert config['terminal']['daemon_tab_close_policy'] == 'detach'


def test_non_string_policy_is_left_for_the_resolver_to_reject():
    config, _ = ensure_config_defaults(
        {'terminal': {'daemon_tab_close_policy': 42}}
    )

    assert config['terminal']['daemon_tab_close_policy'] == 42
    # Unrecognized values fall back to the terminate default.
    assert _policy_of(config) is TerminalClosePolicy.TERMINATE


def test_existing_install_is_migrated_on_load(tmp_path, monkeypatch):
    """End to end through Config: the file on disk is rewritten."""
    monkeypatch.setenv('HOME', str(tmp_path))
    config_dir = tmp_path / '.config' / 'sshpilot'
    config_dir.mkdir(parents=True)
    config_file = config_dir / 'config.json'
    config_file.write_text(
        json.dumps(
            {
                'config_version': CONFIG_VERSION,
                'terminal': {'daemon_tab_close_policy': 'detach'},
            }
        )
    )

    cfg = Config.__new__(Config)
    cfg.config_file = str(config_file)
    cfg.get_default_config = Config.get_default_config.__get__(cfg, Config)
    cfg.save_json_config = Config.save_json_config.__get__(cfg, Config)
    cfg.config_data = Config.load_json_config(cfg)
    cfg.use_gsettings = False

    on_disk = json.loads(config_file.read_text())
    assert on_disk['terminal']['daemon_tab_close_policy'] == 'terminate'
    assert (
        Config.get_setting(cfg, 'terminal.daemon_tab_close_policy') == 'terminate'
    )
    assert resolve_tab_close_policy(cfg) is TerminalClosePolicy.TERMINATE

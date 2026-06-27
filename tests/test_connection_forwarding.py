"""Tests for SSH port-forwarding across all three types (local, remote, dynamic).

Forwarding is not done with per-tunnel ``ssh -L/-R/-D`` subprocesses anymore;
``~/.ssh/config`` is the source of truth (see CLAUDE.md / AGENTS.md). A rule
travels two code paths, both covered here for every type:

  1. UI -> rule dict:   ``ConnectionDialog._save_rule_from_editor`` turns the
     rule-editor widgets into the ``forwarding_rules`` entry.
  2. rule dict -> config: ``ConnectionManager.format_ssh_config_entry`` writes
     the ``LocalForward`` / ``RemoteForward`` / ``DynamicForward`` directive.
"""

from __future__ import annotations

import importlib


from sshpilot.connection_manager import ConnectionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.rules = []
    cm.ssh_config_path = str(tmp_path / "config")
    return cm


def _forward_lines(entry: str):
    prefixes = ("LocalForward", "RemoteForward", "DynamicForward")
    return [ln.strip() for ln in entry.splitlines() if ln.strip().startswith(prefixes)]


def _entry(cm, rules):
    return cm.format_ssh_config_entry({
        "nickname": "fwd",
        "hostname": "fwd.example.com",
        "forwarding_rules": rules,
    })


# ---------------------------------------------------------------------------
# rule dict -> ~/.ssh/config (ConnectionManager.format_ssh_config_entry)
# ---------------------------------------------------------------------------

class TestForwardingConfigOutput:
    def test_local_forward_exact_line(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{
            "type": "local", "listen_addr": "localhost", "listen_port": 8080,
            "remote_host": "localhost", "remote_port": 80,
        }])
        assert _forward_lines(entry) == ["LocalForward localhost:8080 localhost:80"]

    def test_remote_forward_exact_line(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{
            "type": "remote", "listen_addr": "localhost", "listen_port": 2222,
            "local_host": "localhost", "local_port": 22,
        }])
        assert _forward_lines(entry) == ["RemoteForward localhost:2222 localhost:22"]

    def test_remote_forward_socks_single_arg(self, tmp_path):
        """A RemoteForward with no destination is the SOCKS (single-argument) form."""
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{
            "type": "remote", "listen_addr": "localhost", "listen_port": 9999,
        }])
        assert _forward_lines(entry) == ["RemoteForward localhost:9999"]

    def test_dynamic_forward_exact_line(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{
            "type": "dynamic", "listen_addr": "localhost", "listen_port": 1080,
        }])
        assert _forward_lines(entry) == ["DynamicForward localhost:1080"]

    def test_ipv6_bind_address_is_bracketed(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{
            "type": "local", "listen_addr": "::1", "listen_port": 8080,
            "remote_host": "localhost", "remote_port": 80,
        }])
        assert _forward_lines(entry) == ["LocalForward [::1]:8080 localhost:80"]

    def test_all_three_types_written_together(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [
            {"type": "local", "listen_addr": "localhost", "listen_port": 8080,
             "remote_host": "web", "remote_port": 80},
            {"type": "remote", "listen_addr": "localhost", "listen_port": 2222,
             "local_host": "localhost", "local_port": 22},
            {"type": "dynamic", "listen_addr": "localhost", "listen_port": 1080},
        ])
        assert _forward_lines(entry) == [
            "LocalForward localhost:8080 web:80",
            "RemoteForward localhost:2222 localhost:22",
            "DynamicForward localhost:1080",
        ]

    def test_rule_without_listen_port_is_skipped(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{"type": "local", "listen_addr": "localhost",
                             "remote_host": "localhost", "remote_port": 80}])
        assert _forward_lines(entry) == []

    def test_remote_with_dest_host_but_no_port_falls_back_to_socks(self, tmp_path):
        """A remote rule missing its destination port must never emit "host:"."""
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{"type": "remote", "listen_addr": "localhost",
                             "listen_port": 9999, "local_host": "localhost",
                             "local_port": 0}])
        assert _forward_lines(entry) == ["RemoteForward localhost:9999"]

    def test_socks_remote_round_trips_through_parser(self, tmp_path):
        """Parse a single-arg RemoteForward and write it back unchanged.

        The config omits the bind address, so it must NOT be coerced to localhost.
        """
        cm = _make_cm(tmp_path)
        parsed = cm.parse_host_config(
            {"host": "h", "hostname": "h", "remoteforward": "9999"}, source="user"
        )
        rule = parsed["forwarding_rules"][0]
        assert rule.get("socks") is True
        assert not rule.get("listen_addr")  # bind preserved as empty, not localhost
        assert _forward_lines(_entry(cm, [rule])) == ["RemoteForward 9999"]

    def test_remote_empty_bind_omits_host_prefix(self, tmp_path):
        """An empty remote bind address writes just the port (no localhost prefix)."""
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{"type": "remote", "listen_addr": "", "listen_port": 2222,
                             "local_host": "localhost", "local_port": 22}])
        assert _forward_lines(entry) == ["RemoteForward 2222 localhost:22"]

    def test_remote_empty_bind_socks_omits_host_prefix(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{"type": "remote", "listen_addr": "", "listen_port": 9999,
                             "socks": True}])
        assert _forward_lines(entry) == ["RemoteForward 9999"]

    def test_remote_explicit_bind_is_kept(self, tmp_path):
        cm = _make_cm(tmp_path)
        entry = _entry(cm, [{"type": "remote", "listen_addr": "0.0.0.0", "listen_port": 2222,
                             "local_host": "localhost", "local_port": 22}])
        assert _forward_lines(entry) == ["RemoteForward 0.0.0.0:2222 localhost:22"]

    def test_omitted_bind_round_trips_per_type(self, tmp_path):
        """Parser keeps localhost for local/dynamic but empty for remote, and
        each round-trips through the writer."""
        cm = _make_cm(tmp_path)
        parsed = cm.parse_host_config({
            "host": "h", "hostname": "h",
            "localforward": "8080 localhost:80",
            "dynamicforward": "1080",
            "remoteforward": "2222 localhost:22",
        }, source="user")
        rules = {r["type"]: r for r in parsed["forwarding_rules"]}
        assert rules["local"]["listen_addr"] == "localhost"
        assert rules["dynamic"]["listen_addr"] == "localhost"
        assert rules["remote"]["listen_addr"] == ""  # remote bind preserved empty
        assert _forward_lines(_entry(cm, parsed["forwarding_rules"])) == [
            "LocalForward localhost:8080 localhost:80",
            "RemoteForward 2222 localhost:22",
            "DynamicForward localhost:1080",
        ]


# ---------------------------------------------------------------------------
# rule editor widgets -> rule dict (ConnectionDialog._save_rule_from_editor)
# ---------------------------------------------------------------------------

class _Combo:
    def __init__(self, selected: int):
        self._selected = selected

    def get_selected(self) -> int:
        return self._selected


class _Entry:
    def __init__(self, text: str = ""):
        self._text = text

    def get_text(self) -> str:
        return self._text


class _NoConflictChecker:
    def get_port_conflicts(self, ports, addr):
        return []

    def find_available_port(self, port, addr):
        return None


def _make_dialog(monkeypatch):
    """A ConnectionDialog built via __new__, with the GTK/port-checker bits stubbed."""
    cd_mod = importlib.import_module("sshpilot.connection_dialog")
    monkeypatch.setattr(cd_mod, "get_port_checker", lambda: _NoConflictChecker())
    dialog = cd_mod.ConnectionDialog.__new__(cd_mod.ConnectionDialog)
    dialog.forwarding_rules = []
    dialog.load_port_forwarding_rules = lambda: None
    dialog._save_errors = []
    dialog.show_error = lambda msg: dialog._save_errors.append(msg)
    return dialog


def _save(dialog, *, kind, listen_addr="localhost", listen_port="8080",
          dest_host="localhost", dest_port="22"):
    """Drive _save_rule_from_editor for a given type with fake editor widgets."""
    type_idx = {"local": 0, "remote": 1, "dynamic": 2}[kind]
    dialog._save_rule_from_editor(
        None,
        _Combo(type_idx),
        _Entry(listen_addr),
        _Entry(listen_port),
        _Entry(dest_host),
        _Entry(dest_port),
    )


class TestSaveRuleFromEditor:
    def test_local_rule_shape(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="local", listen_port="8080", dest_host="web", dest_port="80")
        assert dialog.forwarding_rules == [{
            "type": "local", "enabled": True,
            "listen_addr": "localhost", "listen_port": 8080,
            "remote_host": "web", "remote_port": 80,
        }]
        assert dialog._save_errors == []

    def test_remote_rule_shape(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="remote", listen_port="2222", dest_host="localhost", dest_port="22")
        assert dialog.forwarding_rules == [{
            "type": "remote", "enabled": True,
            "listen_addr": "localhost", "listen_port": 2222,
            "local_host": "localhost", "local_port": 22,
        }]
        assert dialog._save_errors == []

    def test_dynamic_rule_shape(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="dynamic", listen_port="1080")
        # Dynamic carries no destination host/port keys.
        assert dialog.forwarding_rules == [{
            "type": "dynamic", "enabled": True,
            "listen_addr": "localhost", "listen_port": 1080,
        }]
        assert dialog._save_errors == []

    def test_invalid_listen_port_is_rejected(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="local", listen_port="0")
        assert dialog.forwarding_rules == []
        assert dialog._save_errors  # an error was surfaced to the user

    def test_remote_empty_bind_is_preserved(self, monkeypatch):
        """Leaving the (optional) remote bind address blank stores it empty."""
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="remote", listen_addr="", listen_port="2222",
              dest_host="localhost", dest_port="22")
        assert dialog.forwarding_rules == [{
            "type": "remote", "enabled": True,
            "listen_addr": "", "listen_port": 2222,
            "local_host": "localhost", "local_port": 22,
        }]
        assert dialog._save_errors == []

    def test_remote_with_empty_destination_is_socks(self, monkeypatch):
        """A remote rule with a blank destination is the SOCKS single-arg form."""
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="remote", listen_port="9999", dest_host="", dest_port="")
        assert dialog.forwarding_rules == [{
            "type": "remote", "enabled": True,
            "listen_addr": "localhost", "listen_port": 9999, "socks": True,
        }]
        assert dialog._save_errors == []

    def test_remote_invalid_destination_port_is_rejected(self, monkeypatch):
        """A remote rule with a destination host but a bad port is rejected, not corrupted."""
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="remote", listen_port="9999", dest_host="db", dest_port="0")
        assert dialog.forwarding_rules == []
        assert dialog._save_errors

    def test_local_invalid_destination_port_is_rejected(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        _save(dialog, kind="local", listen_port="8080", dest_host="web", dest_port="0")
        assert dialog.forwarding_rules == []
        assert dialog._save_errors

    def test_editing_socks_rule_preserves_it(self, monkeypatch):
        """Opening a parsed SOCKS rule (blank destination) and saving keeps it SOCKS.

        Mirrors the dialog's populate step, which leaves the destination fields
        blank for a single-arg rule.
        """
        dialog = _make_dialog(monkeypatch)
        socks = {"type": "remote", "enabled": True, "listen_addr": "localhost",
                 "listen_port": 9999, "socks": True}
        dialog.forwarding_rules = [socks]
        dialog._save_rule_from_editor(
            socks, _Combo(1), _Entry("localhost"), _Entry("9999"),
            _Entry(""), _Entry(""),
        )
        assert dialog.forwarding_rules == [{
            "type": "remote", "enabled": True,
            "listen_addr": "localhost", "listen_port": 9999, "socks": True,
        }]
        assert dialog._save_errors == []

    def test_editing_existing_rule_replaces_in_place(self, monkeypatch):
        dialog = _make_dialog(monkeypatch)
        existing = {"type": "local", "enabled": True, "listen_addr": "localhost",
                    "listen_port": 8080, "remote_host": "old", "remote_port": 80}
        other = {"type": "dynamic", "enabled": True, "listen_addr": "localhost",
                 "listen_port": 1080}
        dialog.forwarding_rules = [existing, other]

        dialog._save_rule_from_editor(
            existing, _Combo(0), _Entry("localhost"), _Entry("8080"),
            _Entry("new"), _Entry("443"),
        )

        assert len(dialog.forwarding_rules) == 2
        assert dialog.forwarding_rules[0]["remote_host"] == "new"
        assert dialog.forwarding_rules[0]["remote_port"] == 443
        assert dialog.forwarding_rules[1] is other  # untouched

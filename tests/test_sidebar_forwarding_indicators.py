"""Sidebar port-forwarding indicator source: per-connection daemon fetch.

The connection store's secret-free summary never carries forwarding rules,
so the sidebar L/R/D badges are fed by fetching ``get_connection_editor``
for SSH connections and caching the rules per store generation. These tests
drive that fetch/cache/attach logic through a fake bridge and client.
"""

from types import SimpleNamespace

from sshpilot.api.models.connections import (
    AuthenticationMethod,
    ConnectionEditorDetails,
    ConnectionSummary,
    ForwardingRule,
)

local_rule = ForwardingRule(
    type="local", listen_port=8080, remote_host="localhost", remote_port=80
)
dynamic_rule = ForwardingRule(type="dynamic", listen_port=1080)
LOCAL_RULE_DICT = {
    'type': 'local',
    'listen_addr': '',
    'listen_port': 8080,
    'enabled': True,
    'remote_host': 'localhost',
    'remote_port': 80,
}


def _connection(connection_id, protocol="ssh"):
    return ConnectionSummary(
        id=connection_id,
        nickname=connection_id,
        host="host",
        hostname="host.example",
        username="alice",
        port=22,
        protocol=protocol,
    )


def _editor(connection_id, *rules):
    return ConnectionEditorDetails(
        id=connection_id,
        nickname=connection_id,
        host="host",
        hostname="host.example",
        username="alice",
        port=22,
        authentication_method=AuthenticationMethod.KEY,
        forwarding_rule_count=len(rules),
        forwarding_rules=rules,
    )


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


class _Bridge:
    def __init__(self):
        self.submits = []

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.submits.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=lambda: None)


class _Client:
    def __init__(self, editor=None):
        self.editor = editor
        self.editor_calls = 0

    def get_connection_editor(self, connection_id):
        self.editor_calls += 1
        if isinstance(self.editor, BaseException):
            raise self.editor
        return self.editor


class _Row:
    def __init__(self):
        self.refreshes = 0

    def _update_forwarding_indicators(self):
        self.refreshes += 1


def _window(bridge=None, client=None, config=None, generation=1, rows=None):
    from sshpilot.window import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.config = config if config is not None else _Config()
    window.client_bridge = bridge
    window.client = client
    window.connection_manager = SimpleNamespace(generation=generation)
    window.connection_rows = dict(rows or {})
    window._forwarding_rules_cache = {}
    window._forwarding_rules_pending = set()
    return window


def test_uses_ssh_gate():
    assert _sidebar_ssh(_connection("a"))
    assert not _sidebar_ssh(_connection("b", protocol="telnet"))


def _sidebar_ssh(connection):
    from sshpilot.window import MainWindow

    return MainWindow._sidebar_connection_uses_ssh(connection)


def test_attach_applies_cached_rules_for_current_generation():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    window = _window(generation=3)
    window._forwarding_rules_cache["alpha"] = (3, (LOCAL_RULE_DICT,))

    MainWindow._attach_sidebar_forwarding_rules(window, [alpha])

    assert alpha.forwarding_rules == (LOCAL_RULE_DICT,)


def test_attach_skips_stale_generation_and_non_ssh():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    beta = _connection("beta", protocol="telnet")
    window = _window(generation=3)
    window._forwarding_rules_cache["alpha"] = (1, (LOCAL_RULE_DICT,))
    window._forwarding_rules_cache["beta"] = (3, (LOCAL_RULE_DICT,))

    MainWindow._attach_sidebar_forwarding_rules(window, [alpha, beta])

    assert not hasattr(alpha, "forwarding_rules")
    assert not hasattr(beta, "forwarding_rules")


def test_attach_tolerates_missing_cache():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    window = _window(generation=3)

    MainWindow._attach_sidebar_forwarding_rules(window, [alpha])

    assert not hasattr(alpha, "forwarding_rules")


def test_refresh_submits_only_uncached_ssh_connections():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    beta = _connection("beta", protocol="telnet")
    bridge = _Bridge()
    client = _Client(editor=_editor("alpha"))
    window = _window(bridge=bridge, client=client, generation=1)

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha, beta])

    assert len(bridge.submits) == 1
    operation = bridge.submits[0][0]
    assert operation() == client.editor
    assert client.editor_calls == 1


def test_refresh_respects_disabled_preference():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    config = _Config({"ui.sidebar_show_port_forwarding": False})
    window = _window(bridge=bridge, client=_Client(), config=config)

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])

    assert bridge.submits == []


def test_refresh_requires_attached_client_and_bridge():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=None)

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    assert bridge.submits == []

    other = _window(bridge=None, client=_Client())
    MainWindow._refresh_sidebar_forwarding_rules(other, [alpha])


def test_refresh_skips_cached_entries_for_same_generation():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(), generation=1)
    window._forwarding_rules_cache["alpha"] = (1, (LOCAL_RULE_DICT,))

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])

    assert bridge.submits == []


def test_refresh_refetches_stale_cached_entries():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(editor=_editor("alpha")), generation=3)
    window._forwarding_rules_cache["alpha"] = (1, (LOCAL_RULE_DICT,))

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])

    assert len(bridge.submits) == 1


def test_refresh_deduplicates_in_flight_fetches():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(), generation=1)
    window._forwarding_rules_pending.add("alpha")

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])

    assert bridge.submits == []


def test_success_caches_attaches_and_refreshes_rows():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    row = _Row()
    bridge = _Bridge()
    editor = _editor("alpha", local_rule, dynamic_rule)
    window = _window(
        bridge=bridge,
        client=_Client(editor=editor),
        generation=7,
        rows={alpha: [row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    operation, on_success, _on_error = bridge.submits[0]
    details = operation()
    on_success(details)

    rtype = {r["type"] for r in window._forwarding_rules_cache["alpha"][1]}
    assert rtype == {"local", "dynamic"}
    assert window._forwarding_rules_cache["alpha"][0] == 7
    assert alpha.forwarding_rules == window._forwarding_rules_cache["alpha"][1]
    assert row.refreshes == 1
    assert "alpha" not in window._forwarding_rules_pending


def test_success_refreshes_rows_after_rebuild_by_key():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    rebuilt = _connection("alpha")
    row = _Row()
    bridge = _Bridge()
    editor = _editor("alpha", local_rule)
    window = _window(
        bridge=bridge,
        client=_Client(editor=editor),
        generation=1,
        rows={rebuilt: [row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    operation, on_success, _on_error = bridge.submits[0]
    on_success(operation())

    assert rebuilt.forwarding_rules == (LOCAL_RULE_DICT,)
    assert row.refreshes == 1


def test_error_caches_miss_and_drops_pending():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(), generation=2)

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    _operation, _on_success, on_error = bridge.submits[0]
    on_error(RuntimeError("boom"))

    assert window._forwarding_rules_cache["alpha"] == (2, ())
    assert "alpha" not in window._forwarding_rules_pending
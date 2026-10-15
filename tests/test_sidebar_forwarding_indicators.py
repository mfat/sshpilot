"""Sidebar port-forwarding indicator source: batched daemon fetch.

The connection store's secret-free summary never carries forwarding rules,
so the sidebar forwarding indicator is fed by fetching ``get_connection_editor``
for SSH connections (several per bridge request) and caching the rules per
store generation. These tests drive that fetch/cache/attach logic through a
fake bridge and client.
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
    assert operation() == [(client.editor, None)]
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


def test_error_drops_pending_without_caching_miss():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(), generation=2)

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    _operation, _on_success, on_error = bridge.submits[0]
    on_error(RuntimeError("boom"))

    assert "alpha" not in window._forwarding_rules_cache
    assert "alpha" not in window._forwarding_rules_pending


def test_stale_success_is_discarded_and_current_generation_refetched():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    row = _Row()
    bridge = _Bridge()
    editor = _editor("alpha", local_rule)
    window = _window(
        bridge=bridge,
        client=_Client(editor=editor),
        generation=1,
        rows={alpha: [row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    stale_operation, stale_success, _stale_error = bridge.submits[0]
    assert stale_operation() == [(editor, None)]

    window.connection_manager.generation = 2
    stale_success(stale_operation())

    assert "alpha" not in window._forwarding_rules_cache
    assert not hasattr(alpha, "forwarding_rules")
    assert row.refreshes == 0
    assert "alpha" in window._forwarding_rules_pending
    assert len(bridge.submits) == 2

    current_operation, current_success, _current_error = bridge.submits[1]
    current_success(current_operation())

    assert window._forwarding_rules_cache["alpha"][0] == 2
    assert alpha.forwarding_rules == (LOCAL_RULE_DICT,)
    assert row.refreshes == 1
    assert "alpha" not in window._forwarding_rules_pending


def test_later_refresh_retries_after_failure():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=_Client(editor=_editor("alpha")),
        generation=2,
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    _operation, _on_success, on_error = bridge.submits[0]
    on_error(RuntimeError("boom"))
    assert "alpha" not in window._forwarding_rules_cache

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])

    assert len(bridge.submits) == 2
    assert "alpha" in window._forwarding_rules_pending


def test_stale_error_triggers_current_generation_refetch():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=_Client(editor=_editor("alpha")),
        generation=1,
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    _operation, _on_success, on_error = bridge.submits[0]

    window.connection_manager.generation = 2
    on_error(RuntimeError("boom"))

    assert "alpha" not in window._forwarding_rules_cache
    assert "alpha" in window._forwarding_rules_pending
    assert len(bridge.submits) == 2

    current_operation, current_success, _current_error = bridge.submits[1]
    current_success(current_operation())

    assert window._forwarding_rules_cache["alpha"][0] == 2
    assert "alpha" not in window._forwarding_rules_pending


def test_same_generation_error_does_not_auto_retry():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=_Client(editor=_editor("alpha")),
        generation=2,
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    _operation, _on_success, on_error = bridge.submits[0]
    on_error(RuntimeError("boom"))

    assert len(bridge.submits) == 1
    assert "alpha" not in window._forwarding_rules_pending
    assert "alpha" not in window._forwarding_rules_cache


class _PerIdClient:
    """Editor details per connection id; an exception value raises."""

    def __init__(self, editors):
        self.editors = dict(editors)
        self.calls = []

    def get_connection_editor(self, connection_id):
        self.calls.append(connection_id)
        editor = self.editors[connection_id]
        if isinstance(editor, BaseException):
            raise editor
        return editor


def test_refresh_fetches_many_connections_in_chunked_requests():
    from sshpilot import window as window_module
    from sshpilot.window import MainWindow

    chunk = window_module._SIDEBAR_FORWARDING_FETCH_CHUNK
    connections = [_connection(f"host-{index}") for index in range(chunk + 3)]
    client = _PerIdClient({c.id: _editor(c.id) for c in connections})
    bridge = _Bridge()
    window = _window(bridge=bridge, client=client, generation=1)

    MainWindow._refresh_sidebar_forwarding_rules(window, connections)

    assert len(bridge.submits) == 2
    first, second = (operation() for operation, _s, _e in bridge.submits)
    assert len(first) == chunk and len(second) == 3
    assert client.calls == [c.id for c in connections]


def test_one_failed_connection_keeps_the_rest_of_its_chunk():
    from sshpilot.window import MainWindow

    alpha, beta = _connection("alpha"), _connection("beta")
    alpha_row, beta_row = _Row(), _Row()
    client = _PerIdClient({
        "alpha": RuntimeError("boom"),
        "beta": _editor("beta", local_rule),
    })
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=client,
        generation=4,
        rows={alpha: [alpha_row], beta: [beta_row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha, beta])
    operation, on_success, _on_error = bridge.submits[0]
    on_success(operation())

    assert "alpha" not in window._forwarding_rules_cache
    assert window._forwarding_rules_cache["beta"] == (4, (LOCAL_RULE_DICT,))
    assert beta.forwarding_rules == (LOCAL_RULE_DICT,)
    assert (alpha_row.refreshes, beta_row.refreshes) == (0, 1)
    assert window._forwarding_rules_pending == set()


def test_rows_without_rules_are_not_refreshed_for_an_empty_result():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    row = _Row()
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=_Client(editor=_editor("alpha")),
        generation=1,
        rows={alpha: [row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    operation, on_success, _on_error = bridge.submits[0]
    on_success(operation())

    assert window._forwarding_rules_cache["alpha"] == (1, ())
    assert alpha.forwarding_rules == ()
    assert row.refreshes == 0


def test_rules_removed_since_the_last_fetch_refresh_the_row():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha")
    object.__setattr__(alpha, "forwarding_rules", (LOCAL_RULE_DICT,))
    row = _Row()
    bridge = _Bridge()
    window = _window(
        bridge=bridge,
        client=_Client(editor=_editor("alpha")),
        generation=2,
        rows={alpha: [row]},
    )

    MainWindow._refresh_sidebar_forwarding_rules(window, [alpha])
    operation, on_success, _on_error = bridge.submits[0]
    on_success(operation())

    assert alpha.forwarding_rules == ()
    assert row.refreshes == 1


def test_scheduled_refresh_runs_once_on_a_low_priority_idle(monkeypatch):
    from sshpilot import window as window_module
    from sshpilot.window import MainWindow

    scheduled = []
    monkeypatch.setattr(
        window_module.GLib,
        "idle_add",
        lambda callback, *args, **kwargs: scheduled.append((callback, kwargs)),
    )
    alpha = _connection("alpha")
    bridge = _Bridge()
    window = _window(bridge=bridge, client=_Client(editor=_editor("alpha")))
    window.connection_manager.get_connections = lambda: [alpha]
    window._sidebar_forwarding_refresh_scheduled = False

    MainWindow._schedule_sidebar_forwarding_refresh(window)
    MainWindow._schedule_sidebar_forwarding_refresh(window)

    assert len(scheduled) == 1
    callback, kwargs = scheduled[0]
    assert kwargs.get("priority") == window_module.GLib.PRIORITY_LOW
    assert bridge.submits == []

    callback()

    assert len(bridge.submits) == 1
    assert window._sidebar_forwarding_refresh_scheduled is False

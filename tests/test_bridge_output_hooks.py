"""Multiple-subscriber output-hook dispatch for the embedded PyXterm backend.

Runs under the stubbed-gi harness: it exercises only the pure-Python hook list
(via ``object.__new__`` to skip the WebKit-dependent ``__init__``).
"""
from sshpilot.terminal_backends import PyXtermBridgeBackend


def _make_backend():
    b = object.__new__(PyXtermBridgeBackend)
    b._output_hooks = []
    b._recent_output = ""
    b._js_ready = True
    b._preready_output = []
    b._preready_bytes = 0
    b._write_to_term = lambda text: None      # skip evaluate_javascript
    return b


def test_multiple_hooks_all_fire_in_order():
    b = _make_backend()
    calls = []
    h1 = lambda: calls.append("autofill")
    h2 = lambda: calls.append("evidence")
    b.add_output_hook(h1)
    b.add_output_hook(h2)
    b.add_output_hook(h1)          # duplicate ignored
    b._on_pty_output("data")
    assert calls == ["autofill", "evidence"]


def test_remove_one_hook_leaves_the_other():
    b = _make_backend()
    calls = []
    h1 = lambda: calls.append("autofill")
    h2 = lambda: calls.append("evidence")
    b.add_output_hook(h1)
    b.add_output_hook(h2)
    b.remove_output_hook(h1)
    b._on_pty_output("data")
    assert calls == ["evidence"]
    # removing a hook that isn't registered is a no-op
    b.remove_output_hook(h1)


def test_hook_exception_does_not_break_others():
    b = _make_backend()
    calls = []

    def boom():
        raise RuntimeError("hook error")

    b.add_output_hook(boom)
    b.add_output_hook(lambda: calls.append("ok"))
    b._on_pty_output("data")       # must not raise
    assert calls == ["ok"]

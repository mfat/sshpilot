"""Unit tests for HostInfoTab pending JS flush without constructing WebKit."""

from __future__ import annotations

from sshpilot.host_info_tab import HostInfoTab


def _fake_tab():
    """Minimal stand-in with the bridge methods bound from HostInfoTab."""

    tab = object.__new__(object)

    class _Tab:
        pass

    tab = _Tab()
    tab._closed = False
    tab._webview = object()
    tab._js_ready = False
    tab._pending_host_info = None
    tab._pending_live = None
    tab.evaluated = []
    tab._evaluate = lambda script, _tab=tab, **_kw: _tab.evaluated.append(script)
    tab._run_javascript = HostInfoTab._run_javascript.__get__(tab, _Tab)
    tab._mark_js_ready = HostInfoTab._mark_js_ready.__get__(tab, _Tab)
    return tab


def test_live_pending_does_not_clobber_host_info_pending():
    tab = _fake_tab()
    tab._run_javascript("window.applyHostInfo({status:'ready'});", kind="host_info")
    tab._run_javascript("window.applyLive({cpu:1});", kind="live")
    assert tab._pending_host_info is not None
    assert tab._pending_live is not None
    assert "applyHostInfo" in tab._pending_host_info
    assert "applyLive" in tab._pending_live

    tab._mark_js_ready()

    assert tab.evaluated == [
        "window.applyHostInfo({status:'ready'});",
        "window.applyLive({cpu:1});",
    ]
    assert tab._pending_host_info is None
    assert tab._pending_live is None


def test_ready_twice_does_not_reevaluate_empty_pending():
    tab = _fake_tab()
    tab._run_javascript("window.applyHostInfo({status:'ready'});", kind="host_info")
    tab._mark_js_ready()
    tab._mark_js_ready()
    assert tab.evaluated == ["window.applyHostInfo({status:'ready'});"]


def test_push_after_ready_evaluates_immediately():
    tab = _fake_tab()
    tab._js_ready = True
    tab._run_javascript("window.applyLive({cpu:2});", kind="live")
    assert tab.evaluated == ["window.applyLive({cpu:2});"]
    assert tab._pending_live is None

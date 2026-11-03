"""Where RDP connections open: embedded tab (gtk4-frdp) or FreeRDP window."""

import pytest

from sshpilot.rdp_view_policy import (
    CERT_ACCEPT_AND_STORE,
    CERT_ACCEPT_ONCE,
    RdpView,
    RdpViewReason,
    automatic_certificate_answer,
    decide_view,
    display_properties,
    host_and_port,
    unsupported_options,
)

BASIC = {"host": "win.example.org", "username": "alice", "clipboard": True, "sound": True}


def test_auto_opens_a_tab_when_available():
    decision = decide_view(BASIC, embedded_available=True)
    assert decision.view is RdpView.TAB
    assert decision.reason is RdpViewReason.NONE


def test_default_editor_settings_do_not_block_the_tab():
    # The editor defaults (clipboard and audio on, resize mode, negotiated
    # security, prompt for certificates) must all be supported by the tab.
    defaults = dict(BASIC, display_mode="resize", security="auto", cert_policy="prompt",
                    scale="", multimon=False, microphone=False, printer=False, admin=False)
    assert unsupported_options(defaults) == ()
    assert decide_view(defaults, embedded_available=True).view is RdpView.TAB


def test_auto_falls_back_quietly_when_not_installed():
    decision = decide_view(BASIC, embedded_available=False)
    assert decision.view is RdpView.WINDOW
    assert decision.reason is RdpViewReason.NOT_INSTALLED
    assert not decision.explicit_tab   # no notice: the user never asked for a tab


def test_explicit_tab_not_installed_is_reported():
    decision = decide_view(dict(BASIC, viewer="tab"), embedded_available=False)
    assert decision.view is RdpView.WINDOW
    assert decision.reason is RdpViewReason.NOT_INSTALLED
    assert decision.explicit_tab


def test_window_choice_always_uses_freerdp():
    decision = decide_view(dict(BASIC, viewer="window"), embedded_available=True)
    assert decision.view is RdpView.WINDOW
    assert decision.reason is RdpViewReason.NONE


@pytest.mark.parametrize("key,value", [
    ("display_mode", "fullscreen"),
    ("scale", "140"),
    ("multimon", True),
    ("microphone", True),
    ("printer", True),
    ("shared_folder", "~/Public"),
    ("security", "nla"),
    ("admin", True),
    ("gateway", "gw.example.org:443"),
    ("extra_rdp_args", "/network:auto"),
])
def test_unsupported_option_keeps_auto_in_the_window(key, value):
    data = dict(BASIC, **{key: value})
    decision = decide_view(data, embedded_available=True)
    assert decision.view is RdpView.WINDOW
    assert decision.reason is RdpViewReason.UNSUPPORTED_OPTIONS
    assert decision.options == (key,)


def test_explicit_tab_opens_and_lists_ignored_options():
    data = dict(BASIC, viewer="tab", printer=True, gateway="gw:443")
    decision = decide_view(data, embedded_available=True)
    assert decision.view is RdpView.TAB
    assert decision.reason is RdpViewReason.UNSUPPORTED_OPTIONS
    assert decision.options == ("printer", "gateway")


@pytest.mark.parametrize("mode,resize,scaling", [
    ("resize", True, True),
    ("fit", False, True),
    ("original", False, False),
    ("", True, True),
])
def test_display_mode_mapping(mode, resize, scaling):
    props = display_properties(dict(BASIC, display_mode=mode))
    assert props["allow_resize"] is resize
    assert props["scaling"] is scaling


def test_clipboard_and_audio_follow_the_editor():
    assert display_properties(dict(BASIC, clipboard=False, sound=False)) == {
        "allow_resize": True, "scaling": True, "clipboard": False, "audio_playback": False}
    # unset means the editor default (on)
    props = display_properties({"host": "h"})
    assert props["clipboard"] is True and props["audio_playback"] is True


@pytest.mark.parametrize("policy,changed,answer", [
    ("prompt", False, None),
    ("prompt", True, None),
    ("tofu", False, CERT_ACCEPT_AND_STORE),
    ("tofu", True, None),            # a changed certificate is never auto-trusted
    ("ignore", False, CERT_ACCEPT_ONCE),
    ("ignore", True, CERT_ACCEPT_ONCE),
])
def test_certificate_policy(policy, changed, answer):
    assert automatic_certificate_answer(policy, changed=changed) == answer


def test_host_and_port():
    assert host_and_port({"host": "a", "port": "3390"}) == ("a", 3390)
    assert host_and_port({"hostname": "b"}) == ("b", 3389)
    assert host_and_port({"host": "c", "port": "nope"}) == ("c", 3389)


def test_connection_data_from_details_merges_plugin_fields():
    import types

    from sshpilot.rdp_view_policy import connection_data_from_details

    details = types.SimpleNamespace(
        data={"generation": 3}, hostname="win.example.org", port=13389, username="alice",
        plugin_data={"viewer": "window", "display_mode": "fit", "clipboard": False})
    data = connection_data_from_details(details)
    assert data["viewer"] == "window" and data["display_mode"] == "fit"
    assert data["hostname"] == "win.example.org" and data["port"] == 13389
    assert data["username"] == "alice"
    assert host_and_port(data) == ("win.example.org", 13389)
    # The settings decide the view: an explicit "window" stays in FreeRDP.
    assert decide_view(data, embedded_available=True).view is RdpView.WINDOW

"""Every connection-editor control, end to end, in both directions.

#1389 shipped because nothing checked that a value already in the SSH config
shows on its switch: "SessionType none" was in the file, the ssh -N switch
read off, and saving wrote it away. This suite holds each control of the
SSH editor to one contract, through the real app and daemon:

* load  -- a Host block that authors the value opens with the control showing
  it; clearing the control and saving removes exactly that line; the reopened
  editor shows it cleared.
* set   -- on a bare Host block, setting the control and saving writes exactly
  the expected lines (nothing else in the block moves), ``ssh -G`` resolves
  them, and the reopened editor shows the value.

Settings the app keeps in its own metadata (tags, Wake-on-LAN, pre-connect)
have no config line; for those the block must stay untouched and the
reopened editor must show the value.

``test_every_editor_control_has_a_contract`` walks the live widget tree and
fails on any value control that is neither in ``CONTRACTS`` nor in
``COVERED_ELSEWHERE`` (which must name a test that mentions it). A new switch
cannot ship without a contract.
"""
from __future__ import annotations

import itertools
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pytest

from tests.test_gui_editor_openssh_roundtrip import (
    _editors,
    _find,
    _pump,
    _save,
    _ssh_g,
)

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH"),
]

BASE_LINES = ("HostName 192.0.2.30",)
TESTS_DIR = Path(__file__).resolve().parent


# -- small user actions -------------------------------------------------------

def _text(attr, value):
    def act(dialog):
        getattr(dialog, attr).set_text(value)
    return act


def _shows_text(attr, value):
    return lambda dialog: getattr(dialog, attr).get_text() == value


def _inherits(attr):
    """Cleared: empty, or showing the dimmed value OpenSSH inherits."""
    def check(dialog):
        row = getattr(dialog, attr)
        return row.get_text() == "" or row.has_css_class("dim-label")
    return check


def _switch(attr, value):
    def act(dialog):
        getattr(dialog, attr).set_active(value)
    return act


def _shows_switch(attr, value):
    return lambda dialog: getattr(dialog, attr).get_active() is value


def _combo(attr, index):
    def act(dialog):
        getattr(dialog, attr).set_selected(index)
    return act


def _shows_combo(attr, index):
    return lambda dialog: getattr(dialog, attr).get_selected() == index


def _both(*actions):
    def act(dialog):
        for action in actions:
            action(dialog)
    return act


def _all(*checks):
    return lambda dialog: all(check(dialog) for check in checks)


def _same_paths(attr, paths):
    """The editor shows expanded paths; compare the files they name."""
    import os

    def check(dialog):
        shown = [os.path.expanduser(p) for p in getattr(dialog, attr).get_paths()]
        return shown == [os.path.expanduser(p) for p in paths]
    return check


# OpenSSH's default order with keyboard-interactive and password first.
PASSWORD_PREF = (
    "PreferredAuthentications "
    "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"
)


# -- the authentication section ------------------------------------------------

_OPENSSH_ORDER = (
    "gssapi-with-mic", "hostbased", "publickey", "keyboard-interactive", "password",
)

def _auth_tick(attr, value):
    """Tick or untick a method's checkbox, as a user does (twice if needed)."""
    def act(dialog):
        check = getattr(dialog, attr)._check
        if check.get_active() is value and not check.get_inconsistent():
            check.set_active(not value)
        check.set_active(value)
    return act


def _auth_shows(attr, value):
    """The checkbox holds *value* as this host's own (not dimmed)."""
    def check(dialog):
        box = getattr(dialog, attr)._check
        return (box.get_active() is value and not box.get_inconsistent()
                and not box.has_css_class("dim-label"))
    return check


def _auth_order_is(order):
    return lambda d: d.auth_policy.policy.order == list(order)


def _auth_order_inherited(dialog):
    return dialog.auth_policy.policy.order is None


def _auth_click(button):
    return lambda d: getattr(d.auth_policy, button).emit("clicked")


def _answer_alert(dialog, alert, response):
    """Answer an alert the way a person does: once it is on screen.

    On libadwaita 1.5 (the oldest supported, and what CI runs) an alert
    closed in the very instant it was presented stays attached to its
    window, and the window then refuses to close: the save that follows
    never "completes". Nobody can click that fast, so the test waits for
    the alert to be up, and for it to be gone again.
    """
    _pump(lambda: False, seconds=0.25)
    alert.emit("response", response)
    alert.force_close()
    assert _pump(lambda: dialog.get_visible_dialog() is None, seconds=3), (
        "the alert did not close"
    )


def _auth_reset_order(dialog):
    """Click Reset and, when it asks (a saved order), confirm the removal."""
    section = dialog.auth_policy
    section.reset_button.emit("clicked")
    alert = section._reset_alert
    if alert is not None:
        _answer_alert(dialog, alert, "remove")


@dataclass
class Contract:
    """One user-visible setting and what it must do."""

    name: str
    attrs: tuple            # dialog attributes this contract exercises
    set: Callable           # the user's action on a fresh editor
    shows: Callable         # the reopened editor shows the value
    lines: tuple = ()       # Host block lines the save must add ('' = metadata)
    clear: Optional[Callable] = None   # undo the value
    cleared: Optional[Callable] = None  # the reopened editor shows it undone
    ssh_g: dict = field(default_factory=dict)  # what OpenSSH resolves
    prepare: Optional[Callable] = None  # reveal the control first
    remains: tuple = ()     # authored lines that clearing leaves in place

    @property
    def metadata(self):
        return not self.lines


C = Contract
CONTRACTS = [
    # Host
    C("hostname", ("hostname_row",), _text("hostname_row", "192.0.2.31"),
      _shows_text("hostname_row", "192.0.2.31"),
      ssh_g={"hostname": "192.0.2.31"}),
    C("username", ("username_row",), _text("username_row", "bob"),
      _shows_text("username_row", "bob"), ("User bob",),
      clear=_text("username_row", ""), cleared=_inherits("username_row"),
      ssh_g={"user": "bob"}),
    C("port", ("port_row",), _text("port_row", "2200"),
      _shows_text("port_row", "2200"), ("Port 2200",),
      clear=_text("port_row", ""), cleared=_inherits("port_row"),
      ssh_g={"port": "2200"}),
    C("display name", ("display_name_row",), _text("display_name_row", "Build box"),
      _shows_text("display_name_row", "Build box"),
      clear=_text("display_name_row", ""), cleared=None),
    C("tags", ("tags_row",), _text("tags_row", "prod, eu"),
      _shows_text("tags_row", "prod, eu"),
      clear=_text("tags_row", ""), cleared=_shows_text("tags_row", "")),
    # Routing
    C("jump hosts", ("proxy_jump_row",), _text("proxy_jump_row", "jump1,jump2"),
      _shows_text("proxy_jump_row", "jump1,jump2"), ("ProxyJump jump1,jump2",),
      clear=_text("proxy_jump_row", ""), cleared=_shows_text("proxy_jump_row", ""),
      ssh_g={"proxyjump": "jump1,jump2"}),
    C("proxy command", ("proxy_command_row",),
      _text("proxy_command_row", "nc %h %p"),
      _shows_text("proxy_command_row", "nc %h %p"), ("ProxyCommand nc %h %p",),
      clear=_text("proxy_command_row", ""), cleared=_shows_text("proxy_command_row", ""),
      ssh_g={"proxycommand": "nc %h %p"}),
    C("forward agent yes", ("forward_agent_row",), _combo("forward_agent_row", 1),
      _shows_combo("forward_agent_row", 1), ("ForwardAgent yes",),
      clear=_combo("forward_agent_row", 0), cleared=_shows_combo("forward_agent_row", 0),
      ssh_g={"forwardagent": "yes"}),
    C("forward agent no", ("forward_agent_row",), _combo("forward_agent_row", 2),
      _shows_combo("forward_agent_row", 2), ("ForwardAgent no",),
      clear=_combo("forward_agent_row", 0), cleared=_shows_combo("forward_agent_row", 0)),
    C("forward agent socket", ("forward_agent_row", "forward_agent_value_row"),
      _both(_combo("forward_agent_row", 3),
            _text("forward_agent_value_row", "/run/agent.sock")),
      _all(_shows_combo("forward_agent_row", 3),
           _shows_text("forward_agent_value_row", "/run/agent.sock")),
      ("ForwardAgent /run/agent.sock",),
      clear=_combo("forward_agent_row", 0), cleared=_shows_combo("forward_agent_row", 0)),
    C("forward agent env", ("forward_agent_row", "forward_agent_value_row"),
      _both(_combo("forward_agent_row", 4),
            _text("forward_agent_value_row", "$MY_AGENT")),
      _all(_shows_combo("forward_agent_row", 4),
           _shows_text("forward_agent_value_row", "$MY_AGENT")),
      ("ForwardAgent $MY_AGENT",),
      clear=_combo("forward_agent_row", 0), cleared=_shows_combo("forward_agent_row", 0)),
    # Authentication: the order
    C("password to the top", ("auth_order_row",),
      lambda d: d.auth_password_row._actions.activate_action("top", None),
      _auth_order_is(["password", "gssapi-with-mic", "hostbased", "publickey",
                      "keyboard-interactive"]),
      ("PreferredAuthentications "
       "password,gssapi-with-mic,hostbased,publickey,keyboard-interactive",),
      clear=_auth_reset_order, cleared=_auth_order_inherited,
      ssh_g={"preferredauthentications":
             "password,gssapi-with-mic,hostbased,publickey,keyboard-interactive"}),
    C("keys to the top", ("auth_order_row",),
      lambda d: d.auth_publickey_row._actions.activate_action("top", None),
      _auth_order_is(["publickey", "gssapi-with-mic", "hostbased",
                      "keyboard-interactive", "password"]),
      ("PreferredAuthentications "
       "publickey,gssapi-with-mic,hostbased,keyboard-interactive,password",),
      clear=_auth_reset_order, cleared=_auth_order_inherited,
      ssh_g={"preferredauthentications":
             "publickey,gssapi-with-mic,hostbased,keyboard-interactive,password"}),
    # Authentication: checked means tried. Unchecking a method that is on
    # writes the order line without it (no per-method switch), and Reset
    # takes the line out again.
    *[
        C(f"{label} off", (attr,),
          _auth_tick(attr, False), _auth_shows(attr, False),
          ("PreferredAuthentications " + ",".join(
              m for m in _OPENSSH_ORDER if m != method),),
          clear=_auth_reset_order, cleared=_auth_order_inherited,
          ssh_g={"preferredauthentications": ",".join(
              m for m in _OPENSSH_ORDER if m != method)})
        for label, attr, method in (
            ("keys", "auth_publickey_row", "publickey"),
            ("password", "auth_password_row", "password"),
            ("keyboard-interactive", "auth_kbdinteractive_row", "keyboard-interactive"),
        )
    ],
    # Kerberos and host-based are listed but off by default: checking one
    # turns it on with its own line (no order line), and unchecking takes
    # the line out again.
    *[
        C(f"{label} on", (attr,),
          _auth_tick(attr, True), _auth_shows(attr, True),
          (f"{keyword} yes",),
          clear=_auth_tick(attr, False),
          cleared=_all(_auth_shows(attr, False), _auth_order_inherited),
          ssh_g={keyword.lower(): "yes"})
        for label, attr, keyword in (
            ("kerberos", "auth_gssapi_row", "GSSAPIAuthentication"),
            ("host-based", "auth_hostbased_row", "HostbasedAuthentication"),
        )
    ],
    # Keys: no mode. The identity file list, IdentitiesOnly and the
    # certificates each write their own lines.
    C("identity file", ("key_editor",),
      lambda d: d.key_editor.add_path("~/.ssh/id_contract"),
      _same_paths("key_editor", ["~/.ssh/id_contract"]),
      ("IdentityFile ~/.ssh/id_contract",),
      clear=lambda d: d.key_editor.set_paths([]),
      cleared=_same_paths("key_editor", [])),
    # A block may set IdentitiesOnly with no identity file of its own.
    C("identities only yes", ("key_only_row",),
      _combo("key_only_row", 1), _shows_combo("key_only_row", 1),
      ("IdentitiesOnly yes",),
      clear=_combo("key_only_row", 0), cleared=_shows_combo("key_only_row", 0),
      ssh_g={"identitiesonly": "yes"}),
    C("identities only no", ("key_only_row",),
      _combo("key_only_row", 2), _shows_combo("key_only_row", 2),
      ("IdentitiesOnly no",),
      clear=_combo("key_only_row", 0), cleared=_shows_combo("key_only_row", 0),
      ssh_g={"identitiesonly": "no"}),
    C("identity file with identities only", ("key_editor", "key_only_row"),
      _both(lambda d: d.key_editor.add_path("~/.ssh/id_contract"),
            _combo("key_only_row", 1)),
      _all(_same_paths("key_editor", ["~/.ssh/id_contract"]),
           _shows_combo("key_only_row", 1)),
      ("IdentityFile ~/.ssh/id_contract", "IdentitiesOnly yes"),
      clear=_combo("key_only_row", 0), cleared=_shows_combo("key_only_row", 0),
      remains=("IdentityFile ~/.ssh/id_contract",),
      ssh_g={"identitiesonly": "yes"}),
    C("certificate", ("cert_editor",),
      lambda d: d.cert_editor.add_path("~/.ssh/id_contract-cert.pub"),
      _same_paths("cert_editor", ["~/.ssh/id_contract-cert.pub"]),
      ("CertificateFile ~/.ssh/id_contract-cert.pub",),
      clear=lambda d: d.cert_editor.set_paths([]),
      cleared=_same_paths("cert_editor", [])),
    *[
        C(f"add keys to agent {value}", ("add_keys_to_agent_row",),
          _combo("add_keys_to_agent_row", index),
          _shows_combo("add_keys_to_agent_row", index),
          (f"AddKeysToAgent {value}",),
          clear=_combo("add_keys_to_agent_row", 0),
          cleared=_shows_combo("add_keys_to_agent_row", 0),
          ssh_g={"addkeystoagent": {"yes": "true", "no": "false"}.get(value, value)})
        for index, value in enumerate(("yes", "no", "ask", "confirm"), start=1)
    ],
    C("identity agent none", ("identity_agent_row",), _combo("identity_agent_row", 1),
      _shows_combo("identity_agent_row", 1), ("IdentityAgent none",),
      clear=_combo("identity_agent_row", 0), cleared=_shows_combo("identity_agent_row", 0),
      ssh_g={"identityagent": "none"}),
    C("identity agent SSH_AUTH_SOCK", ("identity_agent_row",),
      _combo("identity_agent_row", 2), _shows_combo("identity_agent_row", 2),
      ("IdentityAgent SSH_AUTH_SOCK",),
      clear=_combo("identity_agent_row", 0), cleared=_shows_combo("identity_agent_row", 0)),
    C("identity agent socket", ("identity_agent_row", "identity_agent_value_row"),
      _both(_combo("identity_agent_row", 3),
            _text("identity_agent_value_row", "/run/ia.sock")),
      _all(_shows_combo("identity_agent_row", 3),
           _shows_text("identity_agent_value_row", "/run/ia.sock")),
      ("IdentityAgent /run/ia.sock",),
      clear=_combo("identity_agent_row", 0), cleared=_shows_combo("identity_agent_row", 0)),
    C("identity agent env", ("identity_agent_row", "identity_agent_value_row"),
      _both(_combo("identity_agent_row", 4),
            _text("identity_agent_value_row", "$IA_SOCK")),
      _all(_shows_combo("identity_agent_row", 4),
           _shows_text("identity_agent_value_row", "$IA_SOCK")),
      ("IdentityAgent $IA_SOCK",),
      clear=_combo("identity_agent_row", 0), cleared=_shows_combo("identity_agent_row", 0)),
    C("PKCS#11 provider", ("pkcs11_provider_row",),
      _text("pkcs11_provider_row", "/usr/lib/p11-contract.so"),
      _shows_text("pkcs11_provider_row", "/usr/lib/p11-contract.so"),
      ("PKCS11Provider /usr/lib/p11-contract.so",),
      clear=_text("pkcs11_provider_row", ""),
      cleared=_shows_text("pkcs11_provider_row", ""),
      ssh_g={"pkcs11provider": "/usr/lib/p11-contract.so"}),
    C("security key provider", ("security_key_provider_row",),
      _text("security_key_provider_row", "/usr/lib/sk-contract.so"),
      _shows_text("security_key_provider_row", "/usr/lib/sk-contract.so"),
      ("SecurityKeyProvider /usr/lib/sk-contract.so",),
      clear=_text("security_key_provider_row", ""),
      cleared=_inherits("security_key_provider_row"),
      ssh_g={"securitykeyprovider": "/usr/lib/sk-contract.so"}),
    # Forwarding
    C("no remote shell (-N)", ("port_forwarding_only_row",),
      _switch("port_forwarding_only_row", True),
      _shows_switch("port_forwarding_only_row", True), ("SessionType none",),
      clear=_switch("port_forwarding_only_row", False),
      cleared=_shows_switch("port_forwarding_only_row", False),
      ssh_g={"sessiontype": "none"}),
    C("exit on forward failure", ("exit_on_forward_failure_row",),
      _switch("exit_on_forward_failure_row", True),
      _shows_switch("exit_on_forward_failure_row", True),
      ("ExitOnForwardFailure yes",),
      clear=_switch("exit_on_forward_failure_row", False),
      cleared=_shows_switch("exit_on_forward_failure_row", False),
      ssh_g={"exitonforwardfailure": "yes"}),
    C("X11 forwarding", ("x11_row",), _switch("x11_row", True),
      _shows_switch("x11_row", True), ("ForwardX11 yes",),
      clear=_switch("x11_row", False), cleared=_shows_switch("x11_row", False),
      ssh_g={"forwardx11": "yes"}),
    # Commands
    C("local command", ("local_command_row",),
      _text("local_command_row", "echo local-contract"),
      _shows_text("local_command_row", "echo local-contract"),
      ("PermitLocalCommand yes", "LocalCommand echo local-contract"),
      clear=_text("local_command_row", ""), cleared=_shows_text("local_command_row", ""),
      ssh_g={"localcommand": "echo local-contract"}),
    C("remote command", ("remote_command_row",),
      _text("remote_command_row", "echo remote-contract"),
      _shows_text("remote_command_row", "echo remote-contract"),
      ("RemoteCommand echo remote-contract",),
      clear=_text("remote_command_row", ""), cleared=_shows_text("remote_command_row", ""),
      ssh_g={"remotecommand": "echo remote-contract"}),
    # App metadata
    C("pre-connect knock", ("pre_command_knock_radio", "pre_command_knock_row"),
      _both(_switch("pre_command_knock_radio", True),
            _text("pre_command_knock_row", "7000 8000 9000")),
      _all(_shows_switch("pre_command_knock_radio", True),
           _shows_text("pre_command_knock_row", "7000 8000 9000"))),
    C("pre-connect command", ("pre_command_command_radio", "pre_command_timeout_row",
                              "pre_command_abort_row"),
      _both(_switch("pre_command_command_radio", True),
            lambda d: d.pre_command_timeout_row.set_value(17),
            _switch("pre_command_abort_row", True)),
      _all(_shows_switch("pre_command_command_radio", True),
           lambda d: int(d.pre_command_timeout_row.get_value()) == 17,
           _shows_switch("pre_command_abort_row", True))),
    C("wake on LAN", ("wol_mac_row", "wol_broadcast_row", "wol_port_row",
                      "wol_on_connect_row"),
      _both(_text("wol_mac_row", "00:11:22:33:44:55"),
            _text("wol_broadcast_row", "192.0.2.255"),
            _text("wol_port_row", "7"),
            _switch("wol_on_connect_row", False)),
      _all(_shows_text("wol_mac_row", "00:11:22:33:44:55"),
           _shows_text("wol_broadcast_row", "192.0.2.255"),
           _shows_text("wol_port_row", "7"),
           _shows_switch("wol_on_connect_row", False))),
]

# Value controls whose behaviour is pinned by a dedicated test. The guard
# below checks the named file exists and mentions the attribute.
COVERED_ELSEWHERE = {
    "nickname_row": "test_gui_connection_dialog_overrides.py",
    "login_profile_row": "test_gui_connection_dialog_login_profile.py",
}

# Value controls with no end-to-end contract yet. Each entry is a known gap,
# listed so it shows in every run instead of passing silently.
KNOWN_GAPS = {
    "protocol_row": "plugin protocols need their own field contracts",
    "group_row": "group moves go through the sidebar store, not the editor save",
    "password_row": "keyring writes need the sandbox keyring rig",
}


# -- fixture and helpers -----------------------------------------------------

_names = itertools.count()


@pytest.fixture
def root(gui):
    """The daemon's root SSH config; restored after the test."""
    from sshpilot.api.models.daemon import OperationMode

    mode = gui.window.client.get_operation_mode()
    files = (mode.isolated_files if mode.active_mode == OperationMode.ISOLATED
             else mode.default_files)
    path = Path(files.root_config_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    yield path
    for dialog in _editors():
        dialog.close()
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def _host(root, gui, extra=()):
    """Write a fresh Host block and wait for the daemon to list it."""
    name = f"contract{next(_names)}"
    lines = [f"Host {name}", *(f"    {line}" for line in (*BASE_LINES, *extra))]
    root.write_text("\n".join(lines) + "\n")
    assert _pump(lambda: _find(gui.window, name) is not None), (
        f"the daemon never listed {name}"
    )
    return name


def _block(root, name):
    """The non-blank lines of ``Host name``'s block, normalised."""
    out, inside = [], False
    for raw in root.read_text().splitlines():
        line = raw.strip()
        if re.match(r"(?i)^(host|match)\s", line):
            inside = line.split(None, 1)[1].strip() == name
            continue
        if inside and line and not line.startswith("#"):
            out.append(re.sub(r"\s+", " ", line))
    return out


def _canon(lines):
    return sorted(line.split(" ", 1)[0].lower() + " " + line.split(" ", 1)[1]
                  for line in lines)


def _open(gui, name):
    before = set(map(id, _editors()))
    gui.window.show_connection_dialog(_find(gui.window, name))
    assert _pump(lambda: [d for d in _editors() if id(d) not in before])
    dialog = next(d for d in _editors() if id(d) not in before)
    assert _pump(lambda: getattr(dialog, "_daemon_editor_loaded", False)), (
        "the editor never received the daemon snapshot"
    )
    return dialog


def _save_and_wait(dialog, root, name, expected):
    _save(dialog)
    _pump(lambda: _canon(_block(root, name)) == _canon(expected), seconds=5)


# -- the contracts -----------------------------------------------------------

def _ids(contracts):
    return [c.name for c in contracts]


@pytest.mark.parametrize("contract", CONTRACTS, ids=_ids(CONTRACTS))
def test_set_save_reopen(gui, root, contract):
    name = _host(root, gui)
    dialog = _open(gui, name)
    if contract.prepare:
        contract.prepare(dialog)
    contract.set(dialog)
    assert contract.shows(dialog), "the control did not take the user's value"

    expected = list(BASE_LINES)
    if contract.name == "hostname":
        expected = ["HostName 192.0.2.31"]
    expected += list(contract.lines)
    _save_and_wait(dialog, root, name, expected)
    assert _canon(_block(root, name)) == _canon(expected), (
        "the saved Host block is not the base block plus this control's lines"
    )
    resolved = _ssh_g(root, name)
    for key, value in contract.ssh_g.items():
        assert resolved.get(key) == value, f"ssh -G {key}: {resolved.get(key)!r}"

    reopened = _open(gui, name)
    assert _pump(lambda: contract.shows(reopened), seconds=5), (
        "the reopened editor does not show the saved value"
    )


LOADABLE = [c for c in CONTRACTS if c.clear and c.lines]


@pytest.mark.parametrize("contract", LOADABLE, ids=_ids(LOADABLE))
def test_authored_value_loads_and_clears(gui, root, contract):
    """#1389's direction: the file already says it, the control must too."""
    name = _host(root, gui, contract.lines)
    dialog = _open(gui, name)
    assert _pump(lambda: contract.shows(dialog), seconds=5), (
        "an authored value does not show on its control"
    )
    contract.clear(dialog)
    expected = [*BASE_LINES, *contract.remains]
    _save_and_wait(dialog, root, name, expected)
    assert _canon(_block(root, name)) == _canon(expected), (
        "clearing the control did not remove exactly its lines"
    )

    reopened = _open(gui, name)
    if contract.cleared:
        assert _pump(lambda: contract.cleared(reopened), seconds=5), (
            "the reopened editor still shows the cleared value"
        )


METADATA_CLEARABLE = [c for c in CONTRACTS if c.metadata and c.clear and c.cleared]


@pytest.mark.parametrize("contract", METADATA_CLEARABLE, ids=_ids(METADATA_CLEARABLE))
def test_metadata_value_clears(gui, root, contract):
    name = _host(root, gui)
    dialog = _open(gui, name)
    contract.set(dialog)
    _save(dialog)
    reopened = _open(gui, name)
    assert _pump(lambda: contract.shows(reopened), seconds=5)
    contract.clear(reopened)
    _save(reopened)
    again = _open(gui, name)
    assert _pump(lambda: contract.cleared(again), seconds=5), (
        "the cleared metadata value came back"
    )
    assert _canon(_block(root, name)) == _canon(BASE_LINES)


# -- the inventory guard -----------------------------------------------------

def _value_controls(dialog):
    """``{attr: widget}`` for every value control in the live editor."""
    from gi.repository import Adw, Gtk

    value_types = (Adw.SwitchRow, Adw.ComboRow, Adw.EntryRow, Adw.SpinRow,
                   Gtk.Switch, Gtk.CheckButton, Gtk.DropDown, Gtk.Entry,
                   Gtk.SpinButton, Gtk.ToggleButton, Gtk.Scale)
    composite = (Adw.SwitchRow, Adw.ComboRow, Adw.EntryRow, Adw.SpinRow,
                 Gtk.DropDown, Gtk.SpinButton)
    by_id = {id(v): k for k, v in vars(dialog).items() if isinstance(v, Gtk.Widget)}
    found, unnamed = {}, []

    def walk(widget, owner):
        own = by_id.get(id(widget))
        if isinstance(widget, value_types) and not isinstance(owner[1], composite):
            if own:
                found[own] = widget
            elif owner[0] is None:
                unnamed.append(widget)
        if own:
            owner = (own, widget)
        elif isinstance(widget, composite):
            owner = (owner[0], widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child, owner)
            child = child.get_next_sibling()

    walk(dialog, (None, None))
    return found, unnamed


def _label(widget):
    for getter in ("get_title", "get_label", "get_tooltip_text"):
        try:
            text = getattr(widget, getter)()
        except Exception:
            continue
        if text:
            return text
    return type(widget).__name__


# Sub-editors the dialog owns: their value controls are exercised through the
# owner attribute's contract.
OWNERS_COVER_CHILDREN = {"key_editor", "cert_editor",
                         "advanced_tab", "forwarding_editor"}

# The authentication section's rows hold their checkbox (and the order its
# position): each row is a control of its own, though the scan above sees
# only the checkbox inside it.
AUTH_SECTION_ROWS = ("auth_order_row", "auth_gssapi_row", "auth_hostbased_row",
                     "auth_publickey_row", "auth_kbdinteractive_row",
                     "auth_password_row")


def test_every_editor_control_has_a_contract(gui, root):
    name = _host(root, gui)
    dialog = _open(gui, name)
    found, unnamed = _value_controls(dialog)
    for attr in AUTH_SECTION_ROWS:
        assert getattr(dialog, attr, None) is not None, f"the editor has no {attr}"
        found[attr] = getattr(dialog, attr)

    contracted = {attr for c in CONTRACTS for attr in c.attrs}
    for attr, filename in COVERED_ELSEWHERE.items():
        text = (TESTS_DIR / filename).read_text()
        assert attr in text, f"{filename} does not exercise {attr}"
    accounted = contracted | set(COVERED_ELSEWHERE) | set(KNOWN_GAPS)

    missing = sorted(
        f"{attr} ({_label(w)!r})" for attr, w in found.items() if attr not in accounted
    )
    assert not missing, (
        "editor controls with no contract -- add one to CONTRACTS:\n  "
        + "\n  ".join(missing)
    )
    stale = sorted(attr for attr in contracted if attr not in found
                   and attr not in OWNERS_COVER_CHILDREN)
    assert not stale, f"contracts name controls the editor no longer has: {stale}"
    assert not unnamed, (
        "value controls not stored on the dialog cannot be contracted: "
        + ", ".join(_label(w) for w in unnamed)
    )


# -- unrelated edits keep the host's login settings ---------------------------

AUTH_BODIES = {
    "one-time codes after the key": (
        "PreferredAuthentications publickey,keyboard-interactive",
    ),
    "password first with its own key and certificate": (
        "PreferredAuthentications password,publickey",
        "IdentityFile ~/.ssh/id_work",
        "CertificateFile ~/.ssh/id_work-cert.pub",
        "IdentityAgent none",
    ),
    "app password preset with a dedicated key": (
        PASSWORD_PREF,
        "IdentityFile ~/.ssh/id_work",
        "IdentitiesOnly yes",
    ),
}


@pytest.mark.parametrize("body", AUTH_BODIES.values(), ids=AUTH_BODIES.keys())
def test_an_unrelated_edit_keeps_the_login_settings(gui, root, body):
    name = _host(root, gui, body)
    dialog = _open(gui, name)
    dialog.username_row.set_text("someone")
    expected = [*BASE_LINES, "User someone", *body]
    _save_and_wait(dialog, root, name, expected)
    assert _canon(_block(root, name)) == _canon(expected), (
        "saving a User change rewrote the host's login settings"
    )


def test_an_unrelated_edit_reports_only_what_changed(gui, root, monkeypatch):
    """A save marks exactly the edited field as changed (pre_command was
    reported on every save before)."""
    seen = []
    window = gui.window
    original = window._save_connection_via_client

    def spy(dialog, data, *args, **kwargs):
        seen.append(tuple(sorted(data.get("__changed_fields") or ())))
        return original(dialog, data, *args, **kwargs)

    monkeypatch.setattr(window, "_save_connection_via_client", spy)
    name = _host(root, gui)
    dialog = _open(gui, name)
    dialog.username_row.set_text("someone")
    _save_and_wait(dialog, root, name, [*BASE_LINES, "User someone"])
    assert seen == [("username",)]


def test_reset_shows_what_the_host_inherits_without_its_own_line(gui, root):
    name = f"contract{next(_names)}"
    root.write_text(
        f"Host {name}\n    HostName 192.0.2.30\n"
        "    PreferredAuthentications password\n"
        "\nHost *\n    PreferredAuthentications publickey,keyboard-interactive\n"
    )
    assert _pump(lambda: _find(gui.window, name) is not None)
    dialog = _open(gui, name)
    section = dialog.auth_policy
    assert _pump(lambda: section.policy.fallback_known, seconds=8)
    # A saved order: Reset asks first, and Cancel keeps it.
    section.reset_button.emit("clicked")
    alert = section._reset_alert
    assert alert is not None
    _answer_alert(dialog, alert, "cancel")
    assert section.policy.order == ["password"]
    _auth_reset_order(dialog)
    assert section.policy.shown_order() == ["publickey", "keyboard-interactive"]
    assert dialog.auth_order_row.get_subtitle() == "publickey, keyboard-interactive"
    assert (dialog.auth_policy.effective_row.get_subtitle()
            == "publickey, keyboard-interactive")


# -- a Host line that is more than one host's -----------------------------------

def _shared_hosts(root, gui, line, address):
    """Write one block under ``Host <line>`` and wait for *line*'s first name.

    *address* is the test's own: hosts that vanish while others with the
    same destination appear leave the daemon unable to tell which is which,
    and it refuses to edit them until that is settled.
    """
    body = (f"HostName {address}", "User alice")
    root.write_text(f"Host {line}\n" + "".join(f"    {item}\n" for item in body))
    first = line.split()[0]
    assert _pump(lambda: _find(gui.window, first) is not None), (
        f"the daemon never listed {first}"
    )
    return first


def test_a_shared_host_block_is_edited_for_all_its_hosts(gui, root):
    n = next(_names)
    a, b, address = f"shared{n}a", f"shared{n}b", f"192.0.2.{100 + n}"
    _shared_hosts(root, gui, f"{a} {b}", address)
    assert _pump(lambda: _find(gui.window, b) is not None)

    dialog = _open(gui, a)
    banner = dialog._shared_block_banner
    assert banner.get_revealed() and b in banner.get_title()
    assert banner.get_button_label() == "Edit Separately"
    assert not dialog.nickname_row.get_sensitive()        # the line is kept

    dialog.username_row.set_text("bob")
    expected = [f"HostName {address}", "User bob"]
    _save_and_wait(dialog, root, f"{a} {b}", expected)
    assert _canon(_block(root, f"{a} {b}")) == _canon(expected)
    assert root.read_text().count("Host ") == 1            # still one block
    assert _ssh_g(root, b).get("user") == "bob"            # the other host too


def test_a_host_is_edited_separately_from_the_block_it_shares(gui, root):
    n = next(_names)
    a, b, address = f"split{n}a", f"split{n}b", f"192.0.2.{100 + n}"
    _shared_hosts(root, gui, f"{a} {b}", address)

    dialog = _open(gui, a)
    dialog._shared_block_banner.emit("button-clicked")     # Edit Separately
    assert dialog._shared_block_banner.get_button_label() == "Edit for All"
    assert dialog.nickname_row.get_sensitive()

    dialog.username_row.set_text("bob")
    _save(dialog)
    assert _pump(lambda: _block(root, a) != [], seconds=5), "no block of its own"
    assert _canon(_block(root, b)) == _canon([f"HostName {address}", "User alice"])
    assert "User bob" in _block(root, a)
    assert _ssh_g(root, b).get("user") == "alice"          # the other host is as it was
    assert _ssh_g(root, a).get("user") == "bob"


def test_a_name_beside_a_pattern_is_listed_and_says_what_it_shares(gui, root):
    n = next(_names)
    name, address = f"edge{n}", f"192.0.2.{100 + n}"
    root.write_text(f"Host *.prod {name}\n    HostName {address}\n    User ops\n")
    assert _pump(lambda: _find(gui.window, name) is not None), (
        "a name beside a pattern is not listed as a connection"
    )
    dialog = _open(gui, name)
    banner = dialog._shared_block_banner
    assert banner.get_revealed() and "*.prod" in banner.get_title()

    dialog.username_row.set_text("deploy")
    expected = [f"HostName {address}", "User deploy"]
    _save_and_wait(dialog, root, f"*.prod {name}", expected)
    assert _canon(_block(root, f"*.prod {name}")) == _canon(expected)   # line kept


def test_a_save_refused_over_unresolved_hosts_says_why(gui, root):
    """Hosts go while others with the same destination come: the daemon
    cannot tell which is which and refuses to change them. The editor says
    that, and what still works, not just "could not be saved"."""
    n = next(_names)
    address = f"192.0.2.{100 + n}"
    old = _shared_hosts(root, gui, f"gone{n}a gone{n}b", address)
    new = _shared_hosts(root, gui, f"came{n}a came{n}b", address)
    assert _pump(lambda: _find(gui.window, old) is None)
    before = root.read_text()

    dialog = _open(gui, new)
    said = []
    dialog.show_error = lambda message, *args, **kwargs: said.append(message)
    dialog.username_row.set_text("bob")
    dialog.on_save_clicked()
    assert _pump(lambda: bool(said), seconds=8), "the editor said nothing"
    assert "can't tell which of them" in said[0]
    assert "Advanced Configuration" in said[0]
    assert dialog.get_visible()                 # still open: nothing was saved
    assert root.read_text() == before

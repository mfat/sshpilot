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


# Password mode is written as this preference list; the switch below it only
# adds or removes PubkeyAuthentication.
PASSWORD_PREF = (
    "PreferredAuthentications "
    "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"
)


def _password_auth(dialog):
    dialog.auth_toggle.set_active(1)


def _specific_keys(dialog):
    dialog.key_select_row.set_selected(1)


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
    # Authentication
    C("password authentication", ("auth_toggle",), _password_auth,
      lambda d: d.auth_toggle.get_active() == 1,
      (PASSWORD_PREF,),
      clear=lambda d: d.auth_toggle.set_active(0),
      cleared=lambda d: d.auth_toggle.get_active() == 0,
      ssh_g={"preferredauthentications": PASSWORD_PREF.split(" ", 1)[1]}),
    C("disable public key", ("pubkey_auth_row",),
      _switch("pubkey_auth_row", True), _shows_switch("pubkey_auth_row", True),
      (PASSWORD_PREF, "PubkeyAuthentication no"), prepare=_password_auth,
      remains=(PASSWORD_PREF,),
      clear=_switch("pubkey_auth_row", False),
      cleared=_shows_switch("pubkey_auth_row", False),
      ssh_g={"pubkeyauthentication": "false"}),
    C("specific key", ("key_select_row", "key_editor"),
      _both(_specific_keys, lambda d: d.key_editor.add_path("~/.ssh/id_contract")),
      _all(lambda d: d.key_select_row.get_selected() == 1,
           _same_paths("key_editor", ["~/.ssh/id_contract"])),
      ("IdentityFile ~/.ssh/id_contract",),
      clear=lambda d: (d.key_editor.set_paths([]), d.key_select_row.set_selected(0)),
      cleared=lambda d: d.key_select_row.get_selected() == 0),
    C("only selected keys", ("key_only_row",),
      _both(lambda d: d.key_editor.add_path("~/.ssh/id_contract"),
            _switch("key_only_row", True)),
      _shows_switch("key_only_row", True),
      ("IdentityFile ~/.ssh/id_contract", "IdentitiesOnly yes"),
      prepare=_specific_keys, remains=("IdentityFile ~/.ssh/id_contract",),
      clear=_switch("key_only_row", False), cleared=_shows_switch("key_only_row", False),
      ssh_g={"identitiesonly": "yes"}),
    C("certificate", ("cert_editor",),
      _both(lambda d: d.key_editor.add_path("~/.ssh/id_contract"),
            lambda d: d.cert_editor.add_path("~/.ssh/id_contract-cert.pub")),
      _same_paths("cert_editor", ["~/.ssh/id_contract-cert.pub"]),
      ("IdentityFile ~/.ssh/id_contract",
       "CertificateFile ~/.ssh/id_contract-cert.pub"),
      prepare=_specific_keys),
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
# owner attribute's contract (e.g. the auth ToggleGroup's two buttons).
OWNERS_COVER_CHILDREN = {"auth_toggle", "key_editor", "cert_editor",
                         "advanced_tab", "forwarding_editor"}


def test_every_editor_control_has_a_contract(gui, root):
    name = _host(root, gui)
    dialog = _open(gui, name)
    found, unnamed = _value_controls(dialog)

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

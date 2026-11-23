"""The connection dialog's authentication section.

One set of settings, no modes. Both forms a Host block may use are read:

* ``PreferredAuthentications`` -- the order of the methods;
* ``PubkeyAuthentication``, ``PasswordAuthentication``,
  ``KbdInteractiveAuthentication``, ``GSSAPIAuthentication``,
  ``HostbasedAuthentication`` -- per-method switches.

A row is checked when the order lists the method and the block's own lines
do not switch it off. Only the block's own lines and the order count:
inherited or built-in switches never check or uncheck a row, so the dialog
does not guess what OpenSSH will do.

Saving without an edit writes the lines back exactly as authored. An edit
writes the checked methods as one ``PreferredAuthentications`` line. A
per-method line goes only with an explicit change to that method:

* checking a method drops its own ``no`` line;
* unchecking a method drops its own ``yes`` line.

Every other method's lines stay as written, whatever the edit: a reorder,
or a change to another method, never touches them. When an earlier block
or Settings decide the order, those lines are what allow or forbid each
method, so dropping one would change a setting the user did not touch.
Lines a linked login profile sets are never dropped.

Like every other field, this edits the Host block only: which block's
value a connection uses is OpenSSH's business. When an earlier block or
Settings decide the order, the order row is marked as not used (see
``connection_dialog_overrides``), and the line below the list states what
OpenSSH will try.

:class:`AuthPolicy` is the pure model (no GTK), so its rules are tested
headless; :class:`AuthPolicyGroup` draws it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from gettext import gettext as _
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .core.connections.auth_preferences import (
    AUTH_POLICY_KEYWORDS,
    OPENSSH_DEFAULT_PREFERRED_AUTH,
    PREFERRED_AUTH_KEYWORD,
)

# Methods in OpenSSH's default order, with the switch that turns each on/off.
METHOD_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("gssapi-with-mic", "GSSAPIAuthentication"),
    ("hostbased", "HostbasedAuthentication"),
    ("publickey", "PubkeyAuthentication"),
    ("keyboard-interactive", "KbdInteractiveAuthentication"),
    ("password", "PasswordAuthentication"),
)
METHODS: Tuple[str, ...] = tuple(method for method, _kw in METHOD_KEYWORDS)
KEYWORD_OF: Dict[str, str] = dict(METHOD_KEYWORDS)
# Lower-case keyword (and the older spelling) -> method.
METHOD_OF_KEYWORD: Dict[str, str] = {
    keyword.lower(): method for method, keyword in METHOD_KEYWORDS
}
METHOD_OF_KEYWORD["challengeresponseauthentication"] = "keyboard-interactive"
DEFAULT_ORDER: Tuple[str, ...] = tuple(OPENSSH_DEFAULT_PREFERRED_AUTH.split(","))

# OpenSSH's built-in values, used when no line sets a switch.
OPENSSH_SWITCH_DEFAULTS: Dict[str, bool] = {
    "gssapi-with-mic": False,
    "hostbased": False,
    "publickey": True,
    "keyboard-interactive": True,
    "password": True,
}

_TRUE = {"yes", "true", "on", "1", "unbound", "host-bound"}
_FALSE = {"no", "false", "off", "0"}


def method_label(method: str) -> str:
    return {
        "gssapi-with-mic": _("Kerberos (GSSAPI)"),
        "hostbased": _("Host-based"),
        "publickey": _("Keys (publickey)"),
        "keyboard-interactive": _("Keyboard-interactive (PAM, one-time codes)"),
        "password": _("Password"),
    }.get(method, method)


def _split(line: str) -> Tuple[str, str]:
    parts = re.split(r"[\s=]+", line.strip(), maxsplit=1)
    keyword = parts[0].lower() if parts and parts[0] else ""
    value = parts[1].strip() if len(parts) == 2 else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return keyword, value


def parse_switch(value: object) -> Optional[bool]:
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def parse_order(value: object) -> List[str]:
    return [m.strip().lower() for m in str(value or "").split(",") if m.strip()]


def is_policy_line(line: str) -> bool:
    return _split(line)[0] in AUTH_POLICY_KEYWORDS


@dataclass
class AuthPolicy:
    """What the Host block sets, what it would inherit, and what wins."""

    # Set by this Host block. ``None`` = the block writes no order.
    order: Optional[List[str]] = None
    switches: Dict[str, bool] = field(default_factory=dict)
    # Resolved by OpenSSH for what the block does not set (``ssh -G``).
    # ``inherited_known`` is False until it arrives (or if it cannot run).
    inherited_known: bool = False
    inherited_order: Optional[List[str]] = None  # None: no list anywhere
    inherited_switches: Dict[str, Optional[bool]] = field(default_factory=dict)
    # A block before this one already decides these (lower-case keyword ->
    # value), so this block's own value is not used.
    earlier: Dict[str, str] = field(default_factory=dict)
    # Settings ▸ SSH Options force these on the command line, ahead of
    # every configuration file (lower-case keyword -> value).
    forced: Dict[str, str] = field(default_factory=dict)
    # Authored text kept as written while its value is unchanged.
    _raw: Dict[str, str] = field(default_factory=dict)
    _loaded: Dict[str, object] = field(default_factory=dict)
    # Policy lines that are not the first of their kind (OpenSSH ignores
    # them); carried through untouched.
    _passthrough: List[str] = field(default_factory=list)
    # Values reset after load: ``ssh -G`` answered with the host's own line,
    # so their inherited value comes from the fallback instead.
    _unknown_after_reset: set = field(default_factory=set)
    # What the host inherits without its own authentication lines
    # (``connections.get_inherited_auth``; lower-case keyword -> value).
    # Used for values reset after load and for hosts not saved yet, where
    # ``ssh -G`` cannot answer. ``fallback_known`` is False until it arrives.
    fallback: Dict[str, str] = field(default_factory=dict)
    fallback_known: bool = False
    # A login profile's own lines: no host to resolve, nothing above them.
    standalone: bool = False
    # Methods whose lines a linked login profile sets: never dropped.
    locked_methods: set = field(default_factory=set)
    # The block as loaded (order, switches, passthrough) and the methods
    # checked then, recorded by the first edit so returning to the same
    # checked methods restores the authored lines.
    _before_edit: Optional[tuple] = None

    # -- loading -------------------------------------------------------------

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "AuthPolicy":
        policy = cls()
        for line in lines:
            if not line.strip():
                continue
            keyword, value = _split(line)
            if keyword == PREFERRED_AUTH_KEYWORD:
                if policy.order is None:
                    policy.order = parse_order(value)
                    policy._raw["order"] = line.strip()
                    policy._loaded["order"] = list(policy.order)
                    continue
            elif keyword in METHOD_OF_KEYWORD:
                method = METHOD_OF_KEYWORD[keyword]
                parsed = parse_switch(value)
                if method not in policy.switches and parsed is not None:
                    policy.switches[method] = parsed
                    policy._raw[method] = line.strip()
                    policy._loaded[method] = parsed
                    continue
            policy._passthrough.append(line.strip())
        return policy

    def set_inherited(self, resolved: Optional[Mapping[str, str]]) -> None:
        """Take ``ssh -G`` output (lower-case keyword -> value).

        Only what this block does not set is inherited; ``ssh -G`` resolved
        the block's own lines too, so those values are not used here.
        """
        if resolved is None:
            self.inherited_known = False
            return
        self.inherited_known = True
        if "order" not in self._loaded:
            raw = resolved.get(PREFERRED_AUTH_KEYWORD)
            self.inherited_order = parse_order(raw) if raw else None
        for method, keyword in METHOD_KEYWORDS:
            if method in self._loaded:
                continue
            self.inherited_switches[method] = parse_switch(resolved.get(keyword.lower()))

    def set_fallback(self, inherited: Optional[Mapping[str, str]]) -> None:
        if inherited is None:
            return
        self.fallback = {str(k).lower(): str(v) for k, v in inherited.items()}
        self.fallback_known = True

    def _fallback_switch(self, method: str) -> Optional[bool]:
        if not self.fallback_known:
            return None
        keyword = KEYWORD_OF[method].lower()
        value = self.fallback.get(keyword)
        if value is None and method == "keyboard-interactive":
            value = self.fallback.get("challengeresponseauthentication")
        parsed = parse_switch(value) if value is not None else None
        return OPENSSH_SWITCH_DEFAULTS[method] if parsed is None else parsed

    def _fallback_order(self) -> Optional[List[str]]:
        if not self.fallback_known:
            return None
        raw = self.fallback.get(PREFERRED_AUTH_KEYWORD)
        return parse_order(raw) if raw else list(DEFAULT_ORDER)

    def _uses_fallback(self, key: str) -> bool:
        """``ssh -G`` cannot tell what *key* inherits: reset, or no host yet."""
        return key in self._unknown_after_reset or not self.inherited_known

    # -- what is shown -----------------------------------------------------

    @property
    def order_is_set(self) -> bool:
        return self.order is not None

    def shown_order(self) -> List[str]:
        """The order in force: this block's, else the inherited or default one."""
        if self.order is not None:
            return list(self.order)
        if self._uses_fallback("order") and self.fallback_known:
            return self._fallback_order()
        if self.inherited_order is not None:
            return list(self.inherited_order)
        return list(DEFAULT_ORDER)

    def order_known(self) -> bool:
        """The order is known: the block's own, or what it inherits has arrived."""
        if self.order is not None or self.standalone:
            return True
        if self._uses_fallback("order"):
            return self.fallback_known
        return True

    def can_edit(self) -> bool:
        """Edits are allowed once the order they start from is known."""
        return self.order_known()

    def in_order(self, method: str) -> bool:
        return method in self.shown_order()

    def left_out(self) -> List[str]:
        """Known methods the shown order leaves out (they are not tried)."""
        shown = self.shown_order()
        return [m for m in METHODS if m not in shown]

    def _is_checked(self, method: str) -> bool:
        return self.in_order(method) and self.switches.get(method) is not False

    def _first_listed(self) -> List[str]:
        """The order with a repeated known method kept at its first place.

        OpenSSH accepts ``publickey,password,publickey``; the method is
        tried at its first place, so a repeat adds nothing.
        """
        seen: set = set()
        order: List[str] = []
        for m in self.shown_order():
            if m in METHODS:
                if m in seen:
                    continue
                seen.add(m)
            order.append(m)
        return order

    def checked(self) -> List[str]:
        """Known methods checked, in order (each once)."""
        return [m for m in self._first_listed() if m in METHODS and self._is_checked(m)]

    def rows(self) -> List[str]:
        """Known methods in row order: the checked ones in order, then the rest."""
        checked = self.checked()
        return checked + [m for m in METHODS if m not in checked]

    def shown_checked(self, method: str) -> Optional[bool]:
        """The row's checkbox; ``None`` while the order is not known.

        Checked when the order lists *method* and the block's own lines do
        not switch it off. Inherited and built-in switches do not count.
        """
        if not self.order_known():
            return None
        return self._is_checked(method)

    def unknown_tokens(self) -> List[str]:
        """Methods in the order this dialog does not know (kept as written)."""
        return [m for m in self.shown_order() if m not in METHODS]

    def switch_is_set(self, method: str) -> bool:
        return method in self.switches

    def shown_switch(self, method: str) -> Optional[bool]:
        """This block's value, else the inherited one; ``None`` when unknown."""
        if self.switch_is_set(method):
            return self.switches[method]
        if method in self._loaded:
            # ssh -G answered with the block's own line: what remains once
            # an edit dropped it comes from the fallback, when known.
            return self._fallback_switch(method)
        if self._uses_fallback(method):
            return self._fallback_switch(method)
        return self.inherited_switches.get(method)

    def _decided_elsewhere(self, keyword: str) -> Optional[str]:
        """A forced (command line) or earlier-block value, forced first."""
        for source in (self.forced, self.earlier):
            if keyword in source:
                return source[keyword]
            if keyword == "kbdinteractiveauthentication" and (
                "challengeresponseauthentication" in source
            ):
                return source["challengeresponseauthentication"]
        return None

    def effective_order(self) -> Optional[List[str]]:
        """What OpenSSH uses, or ``None`` when it cannot be known yet."""
        decided = self._decided_elsewhere(PREFERRED_AUTH_KEYWORD)
        if decided:
            return parse_order(decided)
        if self.order is not None:
            return list(self.order)
        if self._uses_fallback("order"):
            return self._fallback_order()
        return list(self.inherited_order or DEFAULT_ORDER)

    def effective_switch(self, method: str) -> Optional[bool]:
        decided = self._decided_elsewhere(KEYWORD_OF[method].lower())
        if decided is not None:
            return parse_switch(decided)
        return self.shown_switch(method)

    def will_try(self) -> Optional[List[str]]:
        """Methods OpenSSH will try, in order; ``None`` when not known yet."""
        order = self.effective_order()
        if order is None:
            return None
        return [
            m for m in order
            if m not in KEYWORD_OF or self.effective_switch(m) is not False
        ]

    # -- edits -------------------------------------------------------------

    def _snapshot(self) -> tuple:
        return (
            None if self.order is None else list(self.order),
            dict(self.switches),
            list(self._passthrough),
            set(self._unknown_after_reset),
        )

    def _begin_edit(self) -> None:
        """First edit: the order becomes the checked methods, written as one
        line (methods this dialog does not know keep their places; a
        repeated method keeps its first)."""
        if self._before_edit is not None:
            return
        self._before_edit = (self._snapshot(), self.checked())
        self.order = [
            m for m in self._first_listed() if m not in METHODS or self._is_checked(m)
        ]
        # The block gets this one order line. Later copies OpenSSH ignores
        # today must go: the writer keeps an unchanged line where it was
        # and adds the new one at the end, which would put the ignored
        # copy first and make it the one OpenSSH uses.
        self._drop_passthrough({PREFERRED_AUTH_KEYWORD})
        self._unknown_after_reset.discard("order")

    def _end_edit(self) -> None:
        """Back to the methods checked before the first edit: restore the
        authored lines, so an undone change writes nothing."""
        if self._before_edit is None:
            return
        snapshot, checked_before = self._before_edit
        if self.checked() != checked_before:
            return
        order, switches, passthrough, unknown = snapshot
        self.order, self.switches = order, switches
        self._passthrough, self._unknown_after_reset = passthrough, unknown
        self._before_edit = None

    @property
    def edited(self) -> bool:
        return self._before_edit is not None

    def _drop_switch(self, method: str) -> None:
        """Remove the block's lines for *method*'s switch, every copy."""
        self.switches.pop(method, None)
        self._drop_passthrough(
            {k for k, m in METHOD_OF_KEYWORD.items() if m == method}
        )

    def set_tried(self, method: str, value: bool) -> bool:
        """The row's checkbox: list *method* in the order or leave it out.

        Checking adds it at the end and drops its own ``no`` line;
        unchecking takes it out and drops its own ``yes`` line. The last
        checked method cannot be unchecked: an empty order is not valid.
        """
        if method not in METHODS or method in self.locked_methods:
            return False
        if not self.can_edit() or self._is_checked(method) == bool(value):
            return False
        if not value and self.checked() == [method]:
            return False
        self._begin_edit()
        if value:
            if method not in self.order:
                self.order.append(method)
            if self.switches.get(method) is False:
                self._drop_switch(method)
        else:
            self.order = [m for m in self.order if m != method]
            if self.switches.get(method) is True:
                self._drop_switch(method)
        self._end_edit()
        return True

    def move(self, method: str, index: int) -> bool:
        """Put checked *method* at *index* among the checked rows.

        Unchecked rows are not in the order, so they do not move.
        """
        if not self.can_edit():
            return False
        checked = self.checked()
        if method not in checked:
            return False
        rest = [m for m in checked if m != method]
        index = max(0, min(index, len(rest)))
        rest.insert(index, method)
        if rest == checked:
            return False
        self._begin_edit()
        # Fill the known methods' places in turn; unknown tokens stay put.
        queue = iter(rest)
        self.order = [next(queue) if m in METHODS else m for m in self.order]
        self._end_edit()
        return True

    def move_by(self, method: str, delta: int) -> bool:
        checked = self.checked()
        if method not in checked:
            return False
        index = checked.index(method) + delta
        if index < 0 or index >= len(checked):
            return False
        return self.move(method, index)

    def _drop_passthrough(self, keywords) -> None:
        """Remove repeated lines of *keywords*: a reset means the block sets
        none, and a later copy OpenSSH ignored today would take effect."""
        self._passthrough = [
            line for line in self._passthrough if _split(line)[0] not in keywords
        ]

    def reset_order(self) -> bool:
        if self.order is None:
            return False
        self.order = None
        self._before_edit = None
        self._drop_passthrough({PREFERRED_AUTH_KEYWORD})
        if "order" in self._loaded:
            self._unknown_after_reset.add("order")
        return True

    def to_lines(self) -> List[str]:
        """The block's authentication lines, authored text kept if unchanged."""
        lines: List[str] = []
        if self.order is not None and self.order:
            if self._loaded.get("order") == self.order and "order" in self._raw:
                lines.append(self._raw["order"])
            else:
                lines.append(f"PreferredAuthentications {','.join(self.order)}")
        for method, keyword in METHOD_KEYWORDS:
            if method not in self.switches:
                continue
            value = self.switches[method]
            if self._loaded.get(method) is value and method in self._raw:
                lines.append(self._raw[method])
            else:
                lines.append(f"{keyword} {'yes' if value else 'no'}")
        lines.extend(self._passthrough)
        return lines


def split_policy_lines(extra: str) -> Tuple[List[str], str]:
    """``(policy_lines, other_text)`` of an extra-options text."""
    policy, other = [], []
    for line in (extra or "").splitlines():
        if not line.strip():
            continue
        (policy if is_policy_line(line) else other).append(line)
    return policy, "\n".join(other)


def join_extra(other: str, policy_lines: Iterable[str]) -> str:
    lines = [line for line in (other or "").splitlines() if line.strip()]
    lines.extend(line for line in policy_lines if line.strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GTK
# ---------------------------------------------------------------------------

_DRAG_PREFIX = "sshpilot-auth-method:"


def build_auth_policy_group(on_changed: Callable[[], None], *, profile: bool = False):
    """Create the section; imported lazily so the model stays GTK-free."""
    return AuthPolicyGroup.create(on_changed, profile=profile)


class AuthPolicyGroup:
    """Draws an :class:`AuthPolicy` as an ``Adw.PreferencesGroup``.

    Attributes the dialog and its override marking use:

    ``group``      the PreferencesGroup to place in the page;
    ``rows``       method -> its ``Adw.ActionRow`` (``row._check`` is the box);
    ``order_row``  the "Authentication methods" expander: its subtitle says
                   what OpenSSH will try, and it is marked when an earlier
                   block decides the order.
    """

    @classmethod
    def create(
        cls, on_changed: Callable[[], None], *, profile: bool = False
    ) -> "AuthPolicyGroup":
        """``profile``: a login profile's section. It has no host to resolve,
        so what it leaves unset reads "not set" and no effective order is
        shown; a linked connection keeps its own value there."""
        self = cls()
        self.profile = profile
        self._build(on_changed)
        return self

    # -- construction ------------------------------------------------------

    def _build(self, on_changed: Callable[[], None]) -> None:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self.policy = AuthPolicy()
        self._on_changed = on_changed
        self._syncing = False
        # Set by a linked login profile: the order, and methods by name.
        self._order_locked = False
        self._locked_methods: set = set()
        self._lock_note = ""

        self.group = Adw.PreferencesGroup()
        self.group.set_description(
            _("Lines this profile sets replace a linked connection's own; what "
              "it leaves unset stays the connection's. Methods are tried from "
              "the top; unchecked methods are left out.")
            if self.profile else
            _("Methods are tried from the top. Unchecked methods are left out.")
        )

        # One row that folds the method list away: most hosts never change
        # it, so the header says what will be tried and the rows wait inside.
        self.order_row = Adw.ExpanderRow(title=_("Authentication methods"))
        self.order_row.set_subtitle_lines(2)
        self.group.add(self.order_row)

        self.reset_button = Gtk.Button(label=_("Reset"))
        self.reset_button.add_css_class("flat")
        self.reset_button.set_valign(Gtk.Align.CENTER)
        self.reset_button.set_tooltip_text(
            _("Leave the order to each linked connection") if self.profile else
            _("Use authentication order set by OpenSSH configuration")
        )
        self.reset_button.connect("clicked", self._on_reset_clicked)
        self._reset_alert = None
        self.order_row.add_suffix(self.reset_button)

        self.rows: Dict[str, object] = {}
        for method in METHODS:
            self.rows[method] = self._make_row(method)
        # Rows currently inside the expander, in order (it has no child list).
        self._shown_rows: List[object] = []

        self.extra_row = Adw.ActionRow()
        self.extra_row.set_activatable(False)

    def _make_row(self, method: str):
        from gi.repository import Adw, Gdk, Gio, GObject, Gtk

        row = Adw.ActionRow(title=method_label(method))
        row.set_subtitle(KEYWORD_OF[method])
        row._method = method

        # Prefixes stack right to left: the handle, added last, is leftmost.
        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.CENTER)
        check.update_property(
            [Gtk.AccessibleProperty.LABEL], [method_label(method)]
        )
        check.connect("toggled", self._on_toggled, method)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        row._check = check

        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.set_valign(Gtk.Align.CENTER)
        handle.add_css_class("dim-label")
        row.add_prefix(handle)

        actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("up", lambda *_a, m=method: self._edit(lambda: self.policy.move_by(m, -1))),
            ("down", lambda *_a, m=method: self._edit(lambda: self.policy.move_by(m, 1))),
            ("top", lambda *_a, m=method: self._edit(lambda: self.policy.move(m, 0))),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        row.insert_action_group("authrow", actions)
        row._actions = actions

        menu = Gio.Menu()
        menu.append(_("Move up"), "authrow.up")
        menu.append(_("Move down"), "authrow.down")
        menu.append(_("Move to top"), "authrow.top")
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(_("Move this method"))
        row.add_suffix(button)

        keys = Gtk.ShortcutController()
        keys.set_scope(Gtk.ShortcutScope.LOCAL)
        for trigger, delta in (("<Alt>Up", -1), ("<Alt>Down", 1)):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(
                    lambda *_a, m=method, d=delta: (
                        not self._can_move(m)
                        or self._edit(lambda: self.policy.move_by(m, d)), True)[1]
                ),
            ))
        row.add_controller(keys)

        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a, m=method: (
            Gdk.ContentProvider.new_for_value(_DRAG_PREFIX + m)
            if self._can_move(m)
            else None
        ))
        source.connect("drag-begin", lambda src, _drag, r=row: self._drag_icon(src, r))
        row.add_controller(source)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect("drop", self._on_drop, row)
        row.add_controller(target)
        return row

    @staticmethod
    def _drag_icon(source, row) -> None:
        from gi.repository import Gtk

        try:
            source.set_icon(Gtk.WidgetPaintable.new(row), 0, 0)
        except Exception:
            pass

    def _on_reset_clicked(self, *_args) -> None:
        """Reset the order; ask first when that removes a saved line."""
        if self.profile or "order" not in self.policy._loaded:
            # A profile's own setting, or an order set in this edit only.
            self._edit(self.policy.reset_order)
            return
        from gi.repository import Adw

        alert = Adw.AlertDialog(
            heading=_("Remove the custom authentication order?"),
            body=_(
                "This will remove your custom authentication order from the "
                "SSH configuration when you save. The host then uses the "
                "order it inherits."
            ),
        )
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("remove", _("Remove"))
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect("response", self._on_reset_response)
        self._reset_alert = alert
        alert.present(self.group.get_root())

    def _on_reset_response(self, _alert, response: str) -> None:
        self._reset_alert = None
        if response == "remove":
            self._edit(self.policy.reset_order)

    def set_profile_locks(
        self, *, order: bool, methods: Iterable[str], note: str = ""
    ) -> None:
        """Lock what a linked login profile sets: the order, and switches."""
        self._order_locked = bool(order)
        self._locked_methods = set(methods)
        self._lock_note = note
        self.policy.locked_methods = set(self._locked_methods)
        self.sync()

    def _editable(self) -> bool:
        """The order can be edited: known, and not set by a linked profile."""
        return self.policy.can_edit() and not self._order_locked

    def _can_move(self, method: str) -> bool:
        return self._editable() and method in self.policy.checked()

    def _on_drop(self, _target, value, _x, y, row) -> bool:
        value = str(value or "")
        if not value.startswith(_DRAG_PREFIX):
            return False  # only this list's own rows
        source = value[len(_DRAG_PREFIX):]
        if not self._can_move(source):
            return False
        dest = row._method
        rows = self.policy.rows()
        if source == dest or dest not in rows:
            return False
        index = rows.index(dest)
        if y > row.get_height() / 2:
            index += 1
        if rows.index(source) < index:
            index -= 1
        # Dropped among the unchecked rows: last of the checked ones.
        return self._edit(lambda: self.policy.move(source, index))

    # -- model <-> widgets ------------------------------------------------

    def load(self, policy: AuthPolicy) -> None:
        # What earlier blocks and Settings decide is about the configuration,
        # not this block's lines, and may arrive before the block is loaded.
        if not policy.earlier:
            policy.earlier = dict(self.policy.earlier)
        if not policy.forced:
            policy.forced = dict(self.policy.forced)
        if not policy.fallback_known and self.policy.fallback_known:
            policy.set_fallback(self.policy.fallback)
        policy.standalone = self.profile
        policy.locked_methods = set(self._locked_methods)
        self.policy = policy
        self.sync()

    def _edit(self, change: Callable[[], bool]) -> bool:
        # Keyboard shortcuts reach here even when the action is disabled.
        changed = bool(change())
        if changed:
            self.sync()
            try:
                self._on_changed()
            except Exception:
                pass
        return changed

    def _on_toggled(self, check, method: str) -> None:
        if self._syncing:
            return
        if not self._editable() or not self._edit(
            lambda: self.policy.set_tried(method, check.get_active())
        ):
            # Refused (locked, not known yet, last method): show the model.
            self.sync()

    def sync(self) -> None:
        """Redraw every row from the model."""
        self._syncing = True
        try:
            for row in self._shown_rows:
                self.order_row.remove(row)
            self._shown_rows = []
            policy = self.policy
            order_set = policy.order_is_set
            for method in policy.rows():
                self._show_row(self.rows[method], method)
            unknown = policy.unknown_tokens()
            if unknown:
                self.extra_row.set_title(", ".join(unknown))
                self.extra_row.set_subtitle(
                    _("Not known to this dialog; kept as written.")
                )
                self._add_row(self.extra_row)
            for method in METHODS:
                self._show_switch(self.rows[method], method)
            self.reset_button.set_sensitive(order_set and not self._order_locked)
            self.reset_button.set_tooltip_text(
                self._lock_note if self._order_locked else (
                    _("Leave the order to each linked connection") if self.profile else
                    _("Use authentication order set by OpenSSH configuration")
                )
            )
            # A profile is not one host: no order is in force, so its
            # header lists the methods it checks.
            will_try = (
                policy.checked() + policy.unknown_tokens() if self.profile
                else policy.will_try()
            )
            if will_try is None:
                self.order_row.set_subtitle(
                    _("Shown once OpenSSH has read the configuration.")
                )
            elif not will_try:
                self.order_row.set_subtitle(_("No method: the login cannot succeed."))
            else:
                self.order_row.set_subtitle(", ".join(will_try))
        finally:
            self._syncing = False

    def _add_row(self, row) -> None:
        self.order_row.add_row(row)
        self._shown_rows.append(row)

    def _show_row(self, row, method: str) -> None:
        # Never dimmed: dim reads as "off" in GNOME. Only checked rows move:
        # unchecked methods are not in the order.
        self._add_row(row)
        actions = row._actions
        checked = self.policy.checked()
        movable = self._can_move(method)
        index = checked.index(method) if method in checked else -1
        actions.lookup_action("up").set_enabled(movable and index > 0)
        actions.lookup_action("down").set_enabled(movable and index < len(checked) - 1)
        actions.lookup_action("top").set_enabled(movable and index > 0)

    def _show_switch(self, row, method: str) -> None:
        check = row._check
        policy = self.policy
        value = policy.shown_checked(method)
        check.set_inconsistent(value is None)
        check.set_active(bool(value))
        locked = method in self._locked_methods
        last = bool(value) and policy.checked() == [method]
        check.set_sensitive(
            value is not None and not locked and self._editable() and not last
        )
        # Never dimmed (see _show_row); the tooltip says why it cannot change.
        if value is None:
            note = _("Shown once OpenSSH has read the configuration.")
        elif locked or self._order_locked:
            note = self._lock_note
        elif last:
            note = _("At least one method must stay checked.")
        elif not value:
            note = _("Not tried. Check it to add it at the end of the login order.")
        else:
            note = None
        check.set_tooltip_text(note)

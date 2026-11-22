"""The connection dialog's authentication section.

One set of settings, no modes. The Host block's own lines are the truth:

* ``PreferredAuthentications`` -- the order of the method rows;
* ``PubkeyAuthentication``, ``PasswordAuthentication``,
  ``KbdInteractiveAuthentication``, ``GSSAPIAuthentication``,
  ``HostbasedAuthentication`` -- each row's checkbox.

A row or checkbox the block does not set shows what OpenSSH resolves,
dimmed; changing it writes the line for this host, and Reset removes it. A
line below the list always states what OpenSSH will try.

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
        """The rows' order: this block's, else the inherited or default one."""
        if self.order is not None:
            return list(self.order)
        if self._uses_fallback("order") and self.fallback_known:
            return self._fallback_order()
        if self.inherited_order is not None:
            return list(self.inherited_order)
        return list(DEFAULT_ORDER)

    def tried(self) -> List[str]:
        """Known methods in the shown order (the rows above "Not tried")."""
        return [m for m in self.shown_order() if m in METHODS]

    def not_tried(self) -> List[str]:
        """Known methods the shown order leaves out."""
        shown = self.shown_order()
        return [m for m in METHODS if m not in shown]

    def unknown_tokens(self) -> List[str]:
        """Methods in the order this dialog does not know (kept as written)."""
        return [m for m in self.shown_order() if m not in METHODS]

    def switch_is_set(self, method: str) -> bool:
        return method in self.switches

    def shown_switch(self, method: str) -> Optional[bool]:
        """This block's value, else the inherited one; ``None`` when unknown."""
        if method in self.switches:
            return self.switches[method]
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

    def _write_order(self, order: List[str]) -> None:
        self.order = order
        self._unknown_after_reset.discard("order")

    def move(self, method: str, index: int) -> bool:
        """Put *method* at *index* among the tried methods."""
        current = self.shown_order()
        if method not in current:
            return False
        tried = [m for m in current if m != method]
        index = max(0, min(index, len(tried)))
        tried.insert(index, method)
        if tried == current:
            return False
        self._write_order(tried)
        return True

    def move_by(self, method: str, delta: int) -> bool:
        current = self.shown_order()
        if method not in current:
            return False
        return self.move(method, current.index(method) + delta)

    def add_back(self, method: str) -> bool:
        """Put a method the order leaves out back at its end."""
        current = self.shown_order()
        if method in current:
            return False
        self._write_order(current + [method])
        return True

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
        self._drop_passthrough({PREFERRED_AUTH_KEYWORD})
        if "order" in self._loaded:
            self._unknown_after_reset.add("order")
        return True

    def set_switch(self, method: str, value: bool) -> bool:
        if self.switches.get(method) is value:
            return False
        self.switches[method] = bool(value)
        self._unknown_after_reset.discard(method)
        return True

    def reset_switch(self, method: str) -> bool:
        if method not in self.switches:
            return False
        del self.switches[method]
        self._drop_passthrough({k for k, m in METHOD_OF_KEYWORD.items() if m == method})
        if method in self._loaded:
            self._unknown_after_reset.add(method)
        return True

    # -- saving ------------------------------------------------------------

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
    ``order_row``  the "OpenSSH will try" row, marked when an earlier block
                   decides the order.
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

        self.group = Adw.PreferencesGroup(title=_("Authentication order"))
        self.group.set_description(
            _("Lines this profile sets replace a linked connection's own; what "
              "it leaves unset stays the connection's. Methods are tried from "
              "the top; unchecked methods are switched off.")
            if self.profile else
            _("Methods are tried from the top. Unchecked methods are switched off.")
        )
        self.reset_button = Gtk.Button(label=_("Reset"))
        self.reset_button.add_css_class("flat")
        self.reset_button.set_tooltip_text(
            _("Leave the order to each linked connection") if self.profile else
            _("Remove this host's order so the inherited one applies")
        )
        self.reset_button.connect("clicked", self._on_reset_clicked)
        self._reset_alert = None
        self.group.set_header_suffix(self.reset_button)

        self.list = Gtk.ListBox()
        self.list.add_css_class("boxed-list")
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.group.add(self.list)

        self.rows: Dict[str, object] = {}
        for method in METHODS:
            self.rows[method] = self._make_row(method)

        self.not_tried_row = Adw.ActionRow(title=_("Not tried"))
        self.not_tried_row.set_subtitle(
            _("Methods below are not in this host's order. Use the row menu to add one back.")
        )
        self.not_tried_row.set_activatable(False)
        self.not_tried_row.add_css_class("dim-label")

        self.extra_row = Adw.ActionRow()
        self.extra_row.set_activatable(False)

        # Below the list, in a list of its own: a group places its own rows
        # ahead of any other child.
        self.order_row = Adw.ActionRow(title=_("OpenSSH will try"))
        self.order_row.set_activatable(False)
        self.order_row.set_subtitle_selectable(True)
        help_list = Gtk.ListBox()
        help_list.add_css_class("boxed-list")
        help_list.set_selection_mode(Gtk.SelectionMode.NONE)
        help_list.set_margin_top(12)
        help_list.append(self.order_row)
        # A profile is not one host: there is no single order in force.
        help_list.set_visible(not self.profile)
        self.group.add(help_list)

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
            ("add", lambda *_a, m=method: self._edit(lambda: self.policy.add_back(m))),
            ("reset", lambda *_a, m=method: self._edit(lambda: self.policy.reset_switch(m))),
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
        menu.append(_("Add back to the order"), "authrow.add")
        menu.append(_("Reset switch"), "authrow.reset")
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(_("Move or reset this method"))
        row.add_suffix(button)

        keys = Gtk.ShortcutController()
        keys.set_scope(Gtk.ShortcutScope.LOCAL)
        for trigger, delta in (("<Alt>Up", -1), ("<Alt>Down", 1)):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(
                    lambda *_a, m=method, d=delta: (
                        self._order_locked
                        or self._edit(lambda: self.policy.move_by(m, d)), True)[1]
                ),
            ))
        row.add_controller(keys)

        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a, m=method: (
            Gdk.ContentProvider.new_for_value(_DRAG_PREFIX + m)
            if row.get_sensitive() and not self._order_locked
            and m in self.policy.shown_order() else None
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
        self.sync()

    def _on_drop(self, _target, value, _x, y, row) -> bool:
        if self._order_locked:
            return False
        value = str(value or "")
        if not value.startswith(_DRAG_PREFIX):
            return False  # only this list's own rows
        source = value[len(_DRAG_PREFIX):]
        dest = row._method
        order = self.policy.shown_order()
        if source == dest or source not in order or dest not in order:
            return False
        index = order.index(dest)
        if y > row.get_height() / 2:
            index += 1
        if order.index(source) < index:
            index -= 1
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
        self._edit(lambda: self.policy.set_switch(method, check.get_active()))

    def sync(self) -> None:
        """Redraw every row from the model."""
        from .connection_dialog_overrides import inherited_note_text

        self._syncing = True
        try:
            child = self.list.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.list.remove(child)
                child = following
            policy = self.policy
            order_set = policy.order_is_set
            for method in policy.tried():
                self._show_row(self.rows[method], order_set, in_order=True)
            unknown = policy.unknown_tokens()
            if unknown:
                self.extra_row.set_title(", ".join(unknown))
                self.extra_row.set_subtitle(
                    _("Not known to this dialog; kept as written.")
                )
                self.list.append(self.extra_row)
            not_tried = policy.not_tried()
            if not_tried:
                self.list.append(self.not_tried_row)
                for method in not_tried:
                    self._show_row(self.rows[method], order_set, in_order=False)
            for method in METHODS:
                self._show_switch(self.rows[method], method, inherited_note_text())
            self.reset_button.set_sensitive(order_set and not self._order_locked)
            self.reset_button.set_tooltip_text(
                self._lock_note if self._order_locked else (
                    _("Leave the order to each linked connection") if self.profile else
                    _("Remove this host's order so the inherited one applies")
                )
            )
            will_try = policy.will_try()
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

    def _show_row(self, row, order_set: bool, *, in_order: bool) -> None:
        self.list.append(row)
        if order_set:
            row.remove_css_class("dim-label")
        else:
            row.add_css_class("dim-label")
        actions = row._actions
        movable = not self._order_locked
        actions.lookup_action("up").set_enabled(in_order and movable)
        actions.lookup_action("down").set_enabled(in_order and movable)
        actions.lookup_action("top").set_enabled(in_order and movable)
        actions.lookup_action("add").set_enabled(not in_order and movable)

    def _show_switch(self, row, method: str, inherited_note: str) -> None:
        check = row._check
        policy = self.policy
        value = policy.shown_switch(method)
        check.set_inconsistent(value is None)
        check.set_active(bool(value))
        is_set = policy.switch_is_set(method)
        locked = method in self._locked_methods
        check.set_sensitive(not locked)
        row._actions.lookup_action("reset").set_enabled(is_set and not locked)
        if locked:
            check.remove_css_class("dim-label")
            check.set_tooltip_text(self._lock_note)
            return
        if is_set:
            check.remove_css_class("dim-label")
            check.set_tooltip_text(None)
        else:
            check.add_css_class("dim-label")
            if self.profile:
                note = _("Not set by this profile: each connection keeps its own value.")
            elif value is not None:
                note = inherited_note
            else:
                note = _("Not set for this host. The value it inherits is shown once saved.")
            check.set_tooltip_text(note)

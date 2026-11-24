"""The connection dialog's authentication section.

One set of settings, no modes. Both forms a Host block may use are read:

* ``PreferredAuthentications`` -- the order of the methods;
* ``PubkeyAuthentication``, ``PasswordAuthentication``,
  ``KbdInteractiveAuthentication``, ``GSSAPIAuthentication``,
  ``HostbasedAuthentication`` -- per-method switches.

OpenSSH tries a method only when the order lists it *and* its switch is
not off, so one checkbox per row stands for both: checked means tried.

* A row is checked when the order lists the method and its switch, this
  block's own line or else what the block inherits (``ssh -G``, OpenSSH's
  built-in value), is not off. A listed method that is switched off shows
  unchecked, and keeps its place in the order line.
* Checking adds the method to the order if it is missing and, when its
  switch is off, turns it on: the block's own ``no`` line is removed if what
  remains is on, else the block gets a ``yes`` line.
* Unchecking takes the method out of the order, or, when only the block's
  own ``yes`` line turns it on, removes that line. It never writes ``no``.

The section's header shows the order set here; the "OpenSSH will try"
line below the list shows what will be tried, including what an earlier
block or Settings decide.

Saving without an edit writes the lines back exactly as authored. An edit
of the order writes one ``PreferredAuthentications`` line: the checked
methods, and the listed ones that are switched off at their places.
Returning to the line the edit started from restores the authored one. A change of a method's setting replaces every line this block has for
that switch (both spellings of the keyboard-interactive one) with a single
line, or with none for Not set. Lines of other methods stay as written.
Lines a linked login profile sets cannot be changed here.

Like every other field, this edits the Host block only: which block's
value a connection uses is OpenSSH's business. When an earlier block or
Settings decide the order, the order row is marked as not used (see
``connection_dialog_overrides``), and the line below the list states what
OpenSSH will try.

:class:`AuthPolicy` is the pure model (no GTK), so its rules are tested
headless; :class:`AuthPolicyGroup` draws it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from gettext import gettext as _
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .core.connections.auth_preferences import (
    AUTH_POLICY_KEYWORDS,
    OPENSSH_DEFAULT_PREFERRED_AUTH,
    AUTH_KEYWORD_ALIASES,
    PREFERRED_AUTH_KEYWORD,
    canonical_keyword,
    split_option,
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
for _alias, _current in AUTH_KEYWORD_ALIASES.items():
    METHOD_OF_KEYWORD[_alias] = METHOD_OF_KEYWORD[_current]


def first_of_setting(source: Mapping[str, str], keyword: str) -> Optional[str]:
    """The value *source* holds for *keyword*'s setting, under any spelling.

    *source* lists values in the order OpenSSH obtains them, so when two
    spellings of one setting are present the first is the one in force.
    """
    for name, value in source.items():
        if canonical_keyword(name) == keyword:
            return value
    return None
DEFAULT_ORDER: Tuple[str, ...] = tuple(OPENSSH_DEFAULT_PREFERRED_AUTH.split(","))

# OpenSSH's built-in values, used when no line sets a switch.
OPENSSH_SWITCH_DEFAULTS: Dict[str, bool] = {
    "gssapi-with-mic": False,
    "hostbased": False,
    "publickey": True,
    "keyboard-interactive": True,
    "password": True,
}

# PubkeyAuthentication values that enable the method with a restriction.
_RESTRICTED = {"unbound", "host-bound"}
_TRUE = {"yes", "true", "on", "1", *_RESTRICTED}
_FALSE = {"no", "false", "off", "0"}


def method_label(method: str) -> str:
    return {
        "gssapi-with-mic": _("Kerberos (GSSAPI)"),
        "hostbased": _("Host-based"),
        "publickey": _("Keys (publickey)"),
        "keyboard-interactive": _("Keyboard-interactive (PAM, one-time codes)"),
        "password": _("Password"),
    }.get(method, method)


_split = split_option


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
    # Methods whose switch a linked login profile sets: not changed here.
    locked_methods: set = field(default_factory=set)
    # The order as loaded (its line, ignored copies, reset state) and the
    # line the first edit of the order started from, recorded by that edit
    # so returning to exactly that line restores the authored one.
    _before_edit: Optional[tuple] = None
    # Switches a checkbox changed, as they were before (method -> its
    # value, authored text and ignored copies), so the opposite click puts
    # the line back as written.
    _switch_before: Dict[str, tuple] = field(default_factory=dict)
    # The rows as shown, held from the first edit on: a row stays where it
    # is when it is unchecked, checked or another one moves. ``None`` until
    # then (and after a reset): the checked methods in order, then the rest.
    _rows: Optional[List[str]] = None
    # Unchecked rows the user dragged to a place: checked, the method is
    # tried there, also when the line lists it somewhere else.
    _placed: set = field(default_factory=set)
    # The order line around a check that moved a listed method to its row's
    # place (method -> line before, line after), so unchecking moves it back.
    _line_before_check: Dict[str, tuple] = field(default_factory=dict)

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
            # A switch OpenSSH does not report (a build without GSSAPI has
            # none) is at its built-in value, not unknown for ever.
            parsed = parse_switch(first_of_setting(resolved, keyword.lower()))
            self.inherited_switches[method] = (
                OPENSSH_SWITCH_DEFAULTS[method] if parsed is None else parsed
            )

    def set_fallback(self, inherited: Optional[Mapping[str, str]]) -> None:
        if inherited is None:
            return
        self.fallback = {str(k).lower(): str(v) for k, v in inherited.items()}
        self.fallback_known = True

    def _fallback_switch(self, method: str) -> Optional[bool]:
        if not self.fallback_known:
            return None
        value = first_of_setting(self.fallback, KEYWORD_OF[method].lower())
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
        """Tried: listed, and its switch is not off."""
        return self.in_order(method) and self.shown_switch(method) is not False

    def own_switch(self, method: str) -> Optional[bool]:
        """This block's own line for *method*: ``None`` when it has none."""
        return self.switches.get(method)

    def restriction(self, method: str) -> str:
        """The authored value of an enabled switch that is more than "yes".

        ``PubkeyAuthentication`` may be ``host-bound`` or ``unbound``: on,
        with a restriction. The line is kept as written, so the row names
        it. Empty for a plain value, and once the line is being rewritten.
        """
        if self.switches.get(method) is not True or self._loaded.get(method) is not True:
            return ""
        value = _split(self._raw.get(method, ""))[1].lower()
        return value if value in _RESTRICTED else ""

    def _switch_known(self, method: str) -> bool:
        """The switch in force is known: set here, inherited, or a profile
        (nothing above it: a switch it does not set is not off)."""
        return (self.standalone or self.switch_is_set(method)
                or self.shown_switch(method) is not None)

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
        """Known methods in row order.

        As loaded: the checked ones in order, then the rest. Once edited the
        rows are held where they are, so unchecking a method does not send
        its row to the bottom; the checked rows, read from the top, are
        always the order in force.
        """
        checked = self.checked()
        if self._rows is None:
            return checked + [m for m in METHODS if m not in checked]
        ticked = set(checked)
        queue = iter(checked)
        return [next(queue) if m in ticked else m for m in self._rows]

    def _hold_rows(self) -> None:
        self._rows = self.rows()

    def _reorder_checked(self, target: List[str]) -> None:
        """Write the checked methods into the line in *target* order: their
        places are filled in turn; unknown tokens and listed methods that
        are switched off stay put."""
        places = set(target)
        queue = iter(target)
        self.order = [next(queue) if m in places else m for m in self.order]

    def _follow_rows(self) -> bool:
        """Make the order the checked rows, read from the top."""
        checked = self.checked()
        ticked = set(checked)
        target = [m for m in (self._rows or ()) if m in ticked]
        if len(target) != len(checked) or target == checked:
            return False
        self._begin_edit()
        self._reorder_checked(target)
        return True

    def _seat_row(self, method: str) -> None:
        """Put *method*'s row where the order has it among the checked rows
        (a no-op when it is there already)."""
        checked = self.checked()
        ticked = set(checked)
        if method not in ticked or [m for m in self._rows if m in ticked] == checked:
            return
        rows = [m for m in self._rows if m != method]
        at = checked.index(method)
        if at + 1 < len(checked):
            rows.insert(rows.index(checked[at + 1]), method)
        elif at > 0:
            rows.insert(rows.index(checked[at - 1]) + 1, method)
        else:
            rows.append(method)
        self._rows = rows

    def place_row(self, method: str, index: int) -> bool:
        """Put *method*'s row at *index* among all rows (a drag).

        Any row can go anywhere. A checked one changes the order; an
        unchecked one only changes where it joins the order when checked.
        """
        if method not in METHODS or not self.can_edit():
            return False
        rows = self.rows()
        rest = [m for m in rows if m != method]
        rest.insert(max(0, min(index, len(rest))), method)
        if rest == rows:
            return False
        self._rows = rest
        if method in self.checked():
            self._placed.discard(method)
        else:
            self._placed.add(method)
        if self._follow_rows():
            self._end_edit()
        return True

    def shown_checked(self, method: str) -> Optional[bool]:
        """The row's checkbox: tried here. ``None`` while not known.

        Checked when the order lists *method* and its switch is not off.
        Unknown while the order is, or while a listed method's inherited
        switch has not arrived from OpenSSH.
        """
        if not self.order_known():
            return None
        if self.in_order(method) and not self._switch_known(method):
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
        return self._inherited_switch(method)

    def _decided_elsewhere(self, keyword: str) -> Optional[str]:
        """A forced (command line) or earlier-block value, forced first."""
        for source in (self.forced, self.earlier):
            value = first_of_setting(source, keyword)
            if value is not None:
                return value
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

    def _order_copies(self) -> List[str]:
        return [
            line for line in self._passthrough
            if _split(line)[0] == PREFERRED_AUTH_KEYWORD
        ]

    def _begin_edit(self) -> None:
        """First edit of the order: the block gets one order line of its own
        (methods this dialog does not know keep their places; a repeated
        method keeps its first).

        A listed method that is switched off stays listed: checking it
        again only turns it on, at its place, and a host that edits its
        order still tries it where an unedited host would once something
        above turns it on.
        """
        if self._before_edit is not None:
            return
        authored = None if self.order is None else list(self.order)
        copies = self._order_copies()
        unknown = "order" in self._unknown_after_reset
        line = self._first_listed()
        self.order = line
        self._before_edit = (authored, copies, unknown, list(line))
        # The block gets this one order line. Later copies OpenSSH ignores
        # today must go: the writer keeps an unchanged line where it was
        # and adds the new one at the end, which would put the ignored
        # copy first and make it the one OpenSSH uses.
        self._drop_passthrough({PREFERRED_AUTH_KEYWORD})
        self._unknown_after_reset.discard("order")

    def _end_edit(self) -> None:
        """Back to the line the first edit started from: restore the
        authored order line, so an undone change writes nothing. The whole
        line is compared, not the checked methods alone: a method moved
        past a switched-off one is a change. A method's own setting is a
        separate edit and stays as it is now."""
        if self._before_edit is None:
            return
        order, copies, unknown, started_from = self._before_edit
        if self.order != started_from:
            return
        self.order = order
        self._passthrough = copies + self._passthrough
        if unknown:
            self._unknown_after_reset.add("order")
        self._before_edit = None

    def _listed_at_start(self, method: str) -> bool:
        """The line the edit of the order started from lists *method* (so
        the line keeps it while it is switched off)."""
        if self._before_edit is None:
            return self.in_order(method)
        return method in self._before_edit[3]

    def _unlist(self, method: str) -> None:
        self.order = [m for m in (self.order or ()) if m != method]

    @property
    def edited(self) -> bool:
        return self._before_edit is not None

    def can_set_tried(self, method: str) -> bool:
        """Checking *method* would not need a switch a linked profile sets."""
        return method in METHODS and not (
            method in self.locked_methods and self.shown_switch(method) is False
        )

    def set_tried(self, method: str, value: bool) -> bool:
        """The row's checkbox: whether OpenSSH tries *method* here.

        Checking lists it at the end of the order if it is missing and turns
        its switch on if it is off (:meth:`_switch_on`). Unchecking undoes a
        switch checking turned on, else drops this block's own ``yes`` when
        what remains is off, else takes the method out of the order; it never
        writes a ``no`` line. A switch line a click removed or replaced comes
        back as written on the opposite click. The last checked method
        cannot be unchecked: nothing would be tried.
        """
        if method not in METHODS or not self.can_edit():
            return False
        if self.shown_checked(method) is None or self._is_checked(method) == bool(value):
            return False
        if not value:
            if self.checked() == [method]:
                return False
            # The row stays where it is, unchecked.
            self._hold_rows()
            moved = self._line_before_check.pop(method, None)
            if method in self._switch_before:
                # Checking turned it on: unchecking puts the line back.
                self._restore_switch(method)
                if not self._is_checked(method):
                    if moved is not None and self.order == moved[1]:
                        # ... and back at the place checking moved it from.
                        self.order = list(moved[0])
                    if self.edited and not self._listed_at_start(method):
                        # Checking listed it too: take that back. A method
                        # the line listed before the edit stays, switched off.
                        self._unlist(method)
                    self._end_edit()
                    return True
            if self.edited and not self._listed_at_start(method):
                # Checking listed it: take that back, and leave the block's
                # own line for its switch alone.
                self._unlist(method)
                self._end_edit()
                return True
            if (self.own_switch(method) is True
                    and self._inherited_switch(method) is False
                    and self.can_set_switch(method)):
                # Its own "yes" is all that turns it on: dropping that line
                # stops it with no order line. Checking again restores it.
                self._remember_switch(method)
                self.set_switch(method, None)
                return True
            self._begin_edit()
            self._unlist(method)
            self._end_edit()
            return True
        if not self.can_set_tried(method):
            return False
        self._hold_rows()
        if self.shown_switch(method) is False:
            if method in self._switch_before:
                # Unchecking dropped its line: checking puts it back.
                self._restore_switch(method)
            if self.shown_switch(method) is False:
                self._switch_on(method)
        if not self.in_order(method):
            # Not in the line: it joins the order at the place its row has.
            self._begin_edit()
            self.order.append(method)
            self._follow_rows()
        elif method in self._placed:
            # Listed already, but its row was dragged to a place: that is
            # where it was asked to be tried, so the line follows.
            self._begin_edit()
            before = list(self.order)
            if self._follow_rows():
                self._line_before_check[method] = (before, list(self.order))
        else:
            # Listed already: it is tried at its place in the line, and its
            # row goes there.
            self._seat_row(method)
        self._placed.discard(method)
        self._end_edit()
        return True

    def overruled(self, method: str) -> Optional[bool]:
        """The checkbox is not what OpenSSH does, because an earlier block
        or Settings decide: True when checked but not tried, False when
        unchecked but tried, None when they agree or it is not known."""
        checked = self.shown_checked(method)
        will_try = self.will_try()
        if checked is None or will_try is None or self.standalone:
            return None
        tried = method in will_try
        if checked == tried:
            return None
        return checked

    def overruled_by_order(self, method: str) -> bool:
        """An overruled row's cause: the order in force (not a switch) is
        what leaves *method* out, or brings it in."""
        if self.overruled(method) is None:
            return False
        if self.overruled(method):
            return self.effective_switch(method) is not False
        return self.shown_switch(method) is not False

    def _inherited_switch(self, method: str) -> Optional[bool]:
        """What *method*'s switch would be without this block's own line."""
        if method in self._loaded:
            # ssh -G answered with the block's own line: what remains once
            # it is dropped comes from the fallback, when known.
            return self._fallback_switch(method)
        if self._uses_fallback(method):
            return self._fallback_switch(method)
        return self.inherited_switches.get(method)

    def _switch_keywords(self, method: str) -> set:
        return {k for k, m in METHOD_OF_KEYWORD.items() if m == method}

    def _switch_on(self, method: str) -> None:
        """Turn *method* on with the fewest lines: drop this block's ``no``
        when what remains is on, else write ``yes``."""
        self._remember_switch(method)
        if self.own_switch(method) is False and self._inherited_switch(method) is True:
            self.set_switch(method, None)
        else:
            self.set_switch(method, True)

    def _remember_switch(self, method: str) -> None:
        """Keep *method*'s switch as it is now, before a checkbox changes it."""
        if method in self._switch_before:
            return
        keywords = self._switch_keywords(method)
        self._switch_before[method] = (
            self.switches.get(method),
            self._raw.get(method),
            [line for line in self._passthrough if _split(line)[0] in keywords],
        )

    def can_set_switch(self, method: str) -> bool:
        return method in METHODS and method not in self.locked_methods

    def set_switch(self, method: str, value: Optional[bool]) -> bool:
        """This block's own line for *method* (the checkbox sets it through
        :meth:`set_tried`; rows have no menu for it).

        ``True``/``False`` write ``yes``/``no``; ``None`` leaves the block
        without a line. Every line the block has for the switch
        is replaced, repeats and the older spelling included, so removing
        one cannot bring an ignored copy into force. The order is not
        touched, and no answer from the configuration is needed.
        """
        if not self.can_set_switch(method):
            return False
        keywords = self._switch_keywords(method)
        repeats = any(_split(line)[0] in keywords for line in self._passthrough)
        if self.switches.get(method) is value and not repeats:
            return False
        self._drop_passthrough(keywords)
        if value is None:
            self.switches.pop(method, None)
            # The line is gone: enabling the method again writes a plain
            # "yes", not the spelling (or restriction) the block had.
            self._raw.pop(method, None)
        else:
            self.switches[method] = bool(value)
        return True

    def _restore_switch(self, method: str) -> None:
        value, raw, copies = self._switch_before.pop(method)
        self._drop_passthrough(self._switch_keywords(method))
        self._passthrough.extend(copies)
        if value is None:
            self.switches.pop(method, None)
        else:
            self.switches[method] = value
        if raw is None:
            self._raw.pop(method, None)
        else:
            self._raw[method] = raw

    def move_to_top(self, method: str) -> bool:
        """"Move to top": first among the checked rows *and* first in the
        line, ahead of the switched-off methods listed before it, so turning
        one of them on later does not put that one ahead of it."""
        return self.move(method, 0, first_in_line=True)

    def move(self, method: str, index: int, *, first_in_line: bool = False) -> bool:
        """Put checked *method* at *index* among the checked rows.

        Unchecked rows are not in the order, so they do not move: a listed
        method that is switched off keeps its place in the line, whichever
        way the checked ones move around it (so moving a method back is an
        exact undo). Only :meth:`move_to_top` passes them.
        """
        if not self.can_edit():
            return False
        checked = self.checked()
        if method not in checked:
            return False
        rest = [m for m in checked if m != method]
        index = max(0, min(index, len(rest)))
        rest.insert(index, method)
        # Already first among the tried ones, "Move to top" may still pass
        # switched-off methods ahead of it in the line.
        if rest == checked and not (
            first_in_line and self._passes_switched_off(method, index)
        ):
            return False
        self._begin_edit()
        self._reorder_checked(rest)
        order = list(self.order)
        places = set(checked)
        if first_in_line:
            at = order.index(method)
            while at > 0 and order[at - 1] in METHODS and order[at - 1] not in places:
                order[at - 1], order[at] = order[at], order[at - 1]
                at -= 1
        self.order = order
        # Back to the line the edit started from restores the authored one.
        self._end_edit()
        return True

    def _passes_switched_off(self, method: str, index: int) -> bool:
        """Moving *method* to the top passes switched-off methods listed
        ahead of it, though it is already the first one tried."""
        if index != 0:
            return False
        order = self._first_listed()
        at = order.index(method) if method in order else 0
        return at > 0 and order[at - 1] in METHODS and not self._is_checked(order[at - 1])

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
        # Nothing recorded about the discarded line outlives it. A switch a
        # checkbox changed is not part of the order: its record stays, so
        # the opposite click still puts that line back.
        self.order = None
        self._before_edit = None
        self._rows = None
        self._placed.clear()
        self._line_before_check.clear()
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


def method_caption(
    value: str, profile: bool, overruled: Optional[bool] = None,
    by_order: bool = False,
) -> str:
    """A method row's caption; empty when the checkbox says it all.

    ``overruled`` (see :meth:`AuthPolicy.overruled`): an earlier block or
    Settings make OpenSSH do other than the checkbox says, which the row
    must state; ``by_order`` says the order in force is the cause, not the
    method's own switch. Else the authored value of a switch that is on with a
    restriction (e.g. host-bound), so it is not hidden.
    """
    if overruled is True and by_order:
        return _("Checked, but not tried: an earlier setting leaves it out of the order")
    if overruled is True:
        return _("Checked, but not tried: an earlier setting turns it off")
    if overruled is False:
        return _("Unchecked, but tried: an earlier setting decides")
    if not value:
        return ""
    # {value} is the authored value, e.g. host-bound.
    return (_("Enabled in this profile ({value})") if profile
            else _("Enabled for this host ({value})")).format(value=value)


_preview_css_installed = False


def _install_preview_css() -> None:
    """The section's own styling (the flat "OpenSSH will try" preview, a
    method row while it is dragged), installed once."""
    global _preview_css_installed
    if _preview_css_installed:
        return
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
list.auth-order-preview-list,
list.auth-order-preview-list > row.auth-order-preview,
list.auth-order-preview-list > row.auth-order-preview:hover {
  background: transparent;
  box-shadow: none;
}
row.auth-order-preview,
row.auth-order-preview > box.header {
  padding-left: 0; padding-right: 0; margin-left: 0; margin-right: 0;
}
row.auth-order-preview .subtitle { font-family: monospace; }
/* A method row being dragged: a solid copy follows the pointer, and the
   empty space in the list says where it lands. No drop outline besides. */
row.auth-method-list:drop(active),
row.auth-method-list:drop(active) list,
row.auth-method-list:drop(active) row {
  box-shadow: none;
  outline: none;
}
/* The copy is a surface of its own, with nothing behind it: it paints, in
   full and opaque, what is behind a row in the list (the window, the card,
   the nested rows' shade), square like the rows. The border is opaque
   too: an edge that needs blending has nothing to blend with. */
list.auth-method-drag-icon {
  border-radius: 0;
  box-shadow: none;
  border: 1px solid mix(@window_bg_color, @window_fg_color, 0.2);
  background-color: @window_bg_color;
  background-image:
    linear-gradient(alpha(@card_shade_color, 0.5), alpha(@card_shade_color, 0.5)),
    linear-gradient(@card_bg_color, @card_bg_color);
}
list.auth-method-drag-icon > row,
list.auth-method-drag-icon > row:hover {
  border-radius: 0;
  background: none;
}
row.auth-method-drag-solid { background-color: @card_bg_color; }
""")
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _preview_css_installed = True


class AuthPolicyGroup:
    """Draws an :class:`AuthPolicy` as an ``Adw.PreferencesGroup``.

    Attributes the dialog and its override marking use:

    ``group``      the PreferencesGroup to place in the page;
    ``rows``       method -> its ``Adw.ActionRow`` (``row._check`` is the box,
                   ``row._actions`` its menu: ``up``/``down``/``top``);
    ``order_row``  the "Authentication methods" expander: its subtitle is
                   the order set here, and it is marked when an earlier
                   block decides the order;
    ``effective_row`` the "OpenSSH will try" preview below the list.
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
        self.order_row.add_css_class("auth-method-list")
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
        # One drop target for the whole list, not one per row: the gap that
        # opens under the pointer belongs to no row.
        from gi.repository import Gdk, GObject

        self._drag_method: Optional[str] = None
        self._drag_home = 0       # the dragged row's own place
        self._drag_gap: Optional[int] = None
        self._drag_height = 0
        self._drag_grab = (0, 0)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_drop_motion)
        target.connect("leave", self._on_drop_leave)
        target.connect("drop", self._on_drop)
        self.order_row.add_controller(target)
        # Rows currently inside the expander, in order (it has no child list).
        self._shown_rows: List[object] = []

        self.extra_row = Adw.ActionRow()
        self.extra_row.set_activatable(False)

        # Below the list, in a list of its own (a group places its own rows
        # ahead of any other child): what OpenSSH will try, all blocks and
        # Settings counted. A read-only preview, not a setting: no card, the
        # small dim "property" title above it, the methods in monospace.
        _install_preview_css()
        self.effective_row = Adw.ActionRow(title=_("OpenSSH will try:"))
        self.effective_row.set_activatable(False)
        self.effective_row.set_subtitle_selectable(True)
        self.effective_row.add_css_class("property")
        self.effective_row.add_css_class("auth-order-preview")
        preview = Gtk.ListBox()
        preview.add_css_class("auth-order-preview-list")
        preview.set_selection_mode(Gtk.SelectionMode.NONE)
        preview.set_margin_top(6)
        preview.append(self.effective_row)
        # A profile is not one host: there is no single order in force.
        preview.set_visible(not self.profile)
        self.group.add(preview)

    def _make_row(self, method: str):
        from gi.repository import Adw, Gdk, Gio, Gtk

        row = Adw.ActionRow(title=method_label(method))
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
            ("top", lambda *_a, m=method: self._edit(lambda: self.policy.move_to_top(m))),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        row.insert_action_group("authrow", actions)
        row._actions = actions

        moves = Gio.Menu()
        moves.append(_("Move up"), "authrow.up")
        moves.append(_("Move down"), "authrow.down")
        moves.append(_("Move to top"), "authrow.top")
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=moves)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(_("Method options"))
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

        # Any row can be dragged anywhere, checked or not.
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", self._on_drag_prepare, row)
        source.connect("drag-begin", self._on_drag_begin, row)
        source.connect("drag-end", self._on_drag_end, row)
        source.connect("drag-cancel", self._on_drag_cancel, row)
        row.add_controller(source)
        return row

    # -- dragging a row ----------------------------------------------------

    def _on_drag_prepare(self, _source, x, y, row):
        from gi.repository import Gdk

        if not self._editable():
            return None
        # Where the row was grabbed, so the copy stays under the pointer.
        self._drag_grab = (int(x), int(y))
        return Gdk.ContentProvider.new_for_value(_DRAG_PREFIX + row._method)

    def _on_drag_begin(self, _source, drag, row) -> None:
        """A solid copy of the row follows the pointer. The row itself
        leaves the list, and an empty space of its size shows where it will
        land: first at its own place, then wherever the pointer is."""
        from gi.repository import Adw, Gtk

        from .platform_utils import is_macos

        method = row._method
        self._drag_method = method
        self._drag_height = self._row_height(row)
        self._drag_home = self.policy.rows().index(method)
        if is_macos():
            # Gtk.DragIcon has tripped AppKit when a drag begins (see
            # dnd_payload.set_internal_drag_icon): the row paints itself as
            # the icon there, so it has to stay in the list, on a solid
            # background.
            row.add_css_class("auth-method-drag-solid")
            try:
                _source.set_icon(Gtk.WidgetPaintable.new(row), *self._drag_grab)
            except Exception:
                pass
            return
        try:
            copy = Adw.ActionRow(title=row.get_title(), subtitle=row.get_subtitle())
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            check.set_active(row._check.get_active())
            check.set_inconsistent(row._check.get_inconsistent())
            copy.add_prefix(check)
            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.set_valign(Gtk.Align.CENTER)
            handle.add_css_class("dim-label")
            copy.add_prefix(handle)
            card = Gtk.ListBox()
            card.set_selection_mode(Gtk.SelectionMode.NONE)
            card.add_css_class("auth-method-drag-icon")
            card.set_size_request(row.get_width(), row.get_height())
            card.append(copy)
            Gtk.DragIcon.get_for_drag(drag).set_child(card)
            drag.set_hotspot(*self._drag_grab)
        except Exception:
            return  # no copy to show: the row stays, as its own stand-in
        row.set_visible(False)
        self._show_gap(self._drag_home)

    def _on_drag_end(self, _source, _drag, _delete, row) -> None:
        self._drag_over(row)

    def _on_drag_cancel(self, _source, _drag, _reason, row) -> bool:
        self._drag_over(row)
        return False

    def _drag_over(self, row) -> None:
        self._drag_method = None
        self._show_gap(None)
        row.remove_css_class("auth-method-drag-solid")
        row.set_visible(True)

    @staticmethod
    def _row_height(row) -> int:
        """The height the row takes in the list: its border and padding
        counted (``get_height`` is the content alone), margins not."""
        ok, bounds = row.compute_bounds(row)
        return int(round(bounds.get_height())) if ok else row.get_height()

    def _other_rows(self) -> List[object]:
        """The rows the dragged one is placed among, in order."""
        return [
            self.rows[m] for m in self.policy.rows() if m != self._drag_method
        ]

    def _show_gap(self, index: Optional[int]) -> None:
        """Open an empty space the size of the dragged row before the
        *index*-th of the other rows (after the last one when past the end);
        ``None`` closes it."""
        self._drag_gap = index
        others = self._other_rows()
        for at, row in enumerate(others):
            row.set_margin_top(self._drag_height if index == at else 0)
            row.set_margin_bottom(
                self._drag_height
                if index is not None and index >= len(others) and at == len(others) - 1
                else 0
            )

    def _gap_index_at(self, y: float) -> int:
        """Where the space goes for a pointer at *y* (the expander's own
        coordinates): the place whose space would be centred nearest to it.

        Worked out from the rows' heights alone, not from where the space
        is now, so opening it does not change the answer under the pointer.
        """
        from gi.repository import Graphene

        others = self._other_rows()
        if not others:
            return 0
        ok, top = others[0].compute_point(self.order_row, Graphene.Point())
        start = (top.y if ok else 0) - others[0].get_margin_top()
        best, best_distance = 0, None
        edge = start
        for index in range(len(others) + 1):
            distance = abs(y - (edge + self._drag_height / 2))
            if best_distance is None or distance < best_distance:
                best, best_distance = index, distance
            if index < len(others):
                edge += self._row_height(others[index])
        return best

    def _on_drop_motion(self, _target, _x, y):
        from gi.repository import Gdk

        if self._drag_method is None or not self._editable():
            return 0
        index = self._gap_index_at(y)
        if index != self._drag_gap:
            self._show_gap(index)
        return Gdk.DragAction.MOVE

    def _on_drop_leave(self, _target) -> None:
        # Outside the list a drop does nothing: the space goes back to the
        # row's own place.
        if self._drag_method is not None:
            self._show_gap(self._drag_home)

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

    def _on_drop(self, _target, value, _x, y) -> bool:
        value = str(value or "")
        if not value.startswith(_DRAG_PREFIX):
            return False  # only this list's own rows
        source = value[len(_DRAG_PREFIX):]
        if source != self._drag_method or not self._editable():
            return False
        index = self._gap_index_at(y)
        # Before the row is placed: the list is rebuilt from the model, and
        # must not be laid out around a space any more.
        self._drag_over(self.rows[source])
        # The row lands in the space, among all rows.
        return self._edit(lambda: self.policy.place_row(source, index))

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
        before = self.policy.to_lines()
        changed = bool(change())
        if changed:
            self.sync()
            # An unchecked row moved changes the list, not what is saved.
            if self.policy.to_lines() != before:
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
            # Refused (order locked, not known yet, last method): show the model.
            self.sync()

    def sync(self) -> None:
        """Redraw every row from the model."""
        self._syncing = True
        try:
            policy = self.policy
            order_set = policy.order_is_set
            unknown = policy.unknown_tokens()
            wanted = [self.rows[method] for method in policy.rows()]
            if unknown:
                self.extra_row.set_title(", ".join(unknown))
                self.extra_row.set_subtitle(
                    _("Not known to this dialog; kept as written.")
                )
                wanted.append(self.extra_row)
            if wanted != self._shown_rows:
                # The expander only appends, so a new order means taking
                # the rows out and putting them back; the row being moved
                # with the keyboard keeps the focus.
                focused = next(
                    (row for row in self._shown_rows if self._holds_focus(row)), None
                )
                for row in self._shown_rows:
                    self.order_row.remove(row)
                self._shown_rows = []
                for row in wanted:
                    self.order_row.add_row(row)
                    self._shown_rows.append(row)
                if focused is not None and focused in wanted:
                    focused.grab_focus()
            for method in policy.rows():
                self._show_row(self.rows[method], method)
            for method in METHODS:
                self._show_switch(self.rows[method], method)
            self.reset_button.set_sensitive(order_set and not self._order_locked)
            self.reset_button.set_tooltip_text(
                self._lock_note if self._order_locked else (
                    _("Leave the order to each linked connection") if self.profile else
                    _("Use authentication order set by OpenSSH configuration")
                )
            )
            # The header: the order this block (or profile) sets, as the
            # checked rows show it. The preview below: what OpenSSH will
            # try, earlier blocks and Settings counted.
            checked = set(policy.checked())
            if not policy.order_known():
                self.order_row.set_subtitle(
                    _("Shown once OpenSSH has read the configuration.")
                )
            else:
                self.order_row.set_subtitle(", ".join(
                    m for m in policy._first_listed() if m not in METHODS or m in checked
                ))
            will_try = policy.will_try()
            if will_try is None:
                self.effective_row.set_subtitle(
                    _("Shown once OpenSSH has read the configuration.")
                )
            elif not will_try:
                self.effective_row.set_subtitle(
                    _("No method: the login cannot succeed.")
                )
            else:
                self.effective_row.set_subtitle(", ".join(will_try))
        finally:
            self._syncing = False

    @staticmethod
    def _holds_focus(row) -> bool:
        try:
            return bool(row.has_focus() or row.get_focus_child() is not None)
        except Exception:
            return False

    def _show_row(self, row, method: str) -> None:
        # Never dimmed: dim reads as "off" in GNOME. Only checked rows move:
        # unchecked methods are not in the order.
        actions = row._actions
        checked = self.policy.checked()
        movable = self._can_move(method)
        index = checked.index(method) if method in checked else -1
        actions.lookup_action("up").set_enabled(movable and index > 0)
        actions.lookup_action("down").set_enabled(movable and index < len(checked) - 1)
        # Also first among the tried ones when switched-off methods are
        # listed ahead of it: "Move to top" puts it first in the line.
        actions.lookup_action("top").set_enabled(movable and (
            index > 0 or self.policy._passes_switched_off(method, 0)
        ))
        row.set_subtitle(method_caption(
            self.policy.restriction(method), bool(self.profile),
            self.policy.overruled(method),
            self.policy.overruled_by_order(method),
        ))

    def _show_switch(self, row, method: str) -> None:
        check = row._check
        policy = self.policy
        value = policy.shown_checked(method)
        check.set_inconsistent(value is None)
        check.set_active(bool(value))
        last = bool(value) and policy.checked() == [method]
        # Checking would need a switch a linked profile sets.
        locked = value is False and not policy.can_set_tried(method)
        check.set_sensitive(
            value is not None and self._editable() and not last and not locked
        )
        # Never dimmed (see _show_row); the tooltip says why it cannot change.
        if value is None:
            note = _("Shown once OpenSSH has read the configuration.")
        elif self._order_locked or locked:
            note = self._lock_note
        elif last:
            note = _("At least one method must stay checked.")
        elif not value and policy.in_order(method):
            # Listed in the order, but its switch is off.
            note = _("Not tried: it is turned off. Check it to turn it on.")
        elif not value:
            note = _("Not tried. Check it to add it to the login order at this place.")
        else:
            note = None
        check.set_tooltip_text(note)

"""Colour network-device terminal output (IPs, MACs, VLANs, interfaces, ...).

``Highlighter.feed`` rewrites a display byte stream, adding zero-width SGR
escapes around recognised tokens. It uses palette indices (``ESC[31m``..
``ESC[36m``) so colours follow the terminal theme, and restores the default
foreground with ``ESC[39m`` so no other attribute is disturbed. Visible text
is never changed: stripping SGR from the output gives back the input.

Streaming rules:

* Escape sequences split across chunks are held until complete. Control
  strings (OSC, DCS, SOS, PM, APC) are passed through by state, not buffered;
  OSC ends at BEL or ST, the others at ST only. C1 controls are recognised in
  their UTF-8 form (``C2 9B`` ...), never as raw 8-bit bytes, which are UTF-8
  continuation bytes.
* Text is never held back. A token split across chunks is coloured from the
  chunk boundary on (the already-shown part stays as it was).
* Text is left alone while the remote has set a foreground/background colour,
  reverse or conceal, and while any alternate-screen mode (47/1047/1049) is on.
* A disabled highlighter returns its input unchanged but keeps tracking
  terminal state, so enabling it mid-session needs no resync.

This module is pure Python and does not import ``gi``.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Set

PROFILES = ("generic", "cisco-ios", "junos", "huawei-vrp")

_RED, _GREEN, _YELLOW, _BLUE, _MAGENTA, _CYAN = 31, 32, 33, 34, 35, 36

_MAX_VLAN = 4094
_MAX_MPLS_LABEL = 1048575

Validator = Callable[[bytes, "re.Match[bytes]"], bool]


class Rule(NamedTuple):
    name: str
    pattern: str
    colour: int
    validate: Optional[Validator] = None


def _numbers_at_most(limit: int, low: int = 0) -> Validator:
    def check(_full: bytes, m: "re.Match[bytes]") -> bool:
        return all(low <= int(n) <= limit for n in re.findall(rb"\d+", m.group()))

    return check


def _valid_ipv6(_full: bytes, m: "re.Match[bytes]") -> bool:
    address, _, prefix = m.group().partition(b"/")
    if prefix and int(prefix) > 128:
        return False
    if not re.search(rb"[0-9A-Fa-f]", address):
        return False
    try:
        ipaddress.IPv6Address(address.decode("ascii"))
    except ValueError:
        return False
    return True


_VERSION_BEFORE = re.compile(rb"(?i)(?:version|ver|release|build)[ :=]+\Z")


def _not_a_version(full: bytes, m: "re.Match[bytes]") -> bool:
    return _VERSION_BEFORE.search(full, max(0, m.start() - 16), m.start()) is None


_OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_BEFORE = r"(?<![0-9A-Za-z])"
_WORD = r"(?<![\w/-])"
_NOT_WORD = r"(?![\w/-])"

_MAC = [
    Rule(
        "mac",
        r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{2}(?P<ms>[:-])(?:[0-9A-Fa-f]{2}(?P=ms)){4}"
        r"[0-9A-Fa-f]{2}(?![0-9A-Za-z]|:[0-9A-Fa-f])",
        _MAGENTA,
    ),
    Rule(
        "mac_dotted",
        r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{4}(?P<md>[.-])[0-9A-Fa-f]{4}(?P=md)[0-9A-Fa-f]{4}"
        r"(?![0-9A-Za-z])",
        _MAGENTA,
    ),
]
_IP = [
    Rule(
        "ipv6",
        r"(?<![0-9A-Za-z:.])(?=[0-9A-Fa-f]{0,4}:)[0-9A-Fa-f:]{2,39}(?:\.\d{1,3}){0,3}"
        r"(?:/\d{1,3})?(?![0-9A-Za-z:])",
        _CYAN,
        _valid_ipv6,
    ),
    Rule(
        "ipv4",
        rf"(?<![0-9A-Za-z.])(?:{_OCT}\.){{3}}{_OCT}(?:/(?:3[0-2]|[12]?\d))?"
        r"(?![0-9A-Za-z]|\.[0-9])",
        _CYAN,
        _not_a_version,
    ),
]
_VLAN = Rule(
    "vlan",
    rf"{_BEFORE}(?:[Vv][Ll][Aa][Nn](?:-id)?[ \t]+\d{{1,4}}(?:[,-]\d{{1,4}})*|VLAN\d{{4}})"
    r"(?![0-9A-Za-z])",
    _BLUE,
    _numbers_at_most(_MAX_VLAN, low=1),
)
_MPLS = Rule(
    "mpls",
    rf"{_BEFORE}[Ll]abel:?[ \t]+\d{{1,7}}(?![0-9.])|{_WORD}(?:implicit|explicit)-null{_NOT_WORD}",
    _BLUE,
    _numbers_at_most(_MAX_MPLS_LABEL),
)

# Interfaces: long forms every vendor spells out; profiles add abbreviations.
_IFACE = Rule(
    "iface",
    rf"{_BEFORE}(?:(?:Gigabit|TenGigabit|TwentyFiveGig|FortyGigabit|HundredGig|Fast|XGigabit)"
    r"Ethernet|Ethernet|Port-channel|Loopback|LoopBack|Tunnel|Serial|Vlanif|Vlan|Eth-Trunk"
    r"|Management|MEth|TwentyFiveGigE|HundredGigE|TenGigE)\d+(?:/\d+){0,4}(?:\.\d+)?"
    r"(?![0-9A-Za-z])"
    rf"|{_BEFORE}(?:(?:ge|xe|et|fe)-\d+/\d+/\d+(?::\d+)?(?:\.\d+)?|ae\d+(?:\.\d+)?)"
    r"(?![0-9A-Za-z])",
    _YELLOW,
)
_EXTRA_IFACE: Dict[str, str] = {
    "cisco-ios": r"(?:Gi|Te|Fa|Eth|Et|Po|Lo|Tu|Hu|Fo|Twe|Vl)\d+(?:/\d+){0,4}(?:\.\d+)?",
    "junos": (
        r"(?:(?:so|vcp|lt|pp|ip|gr|st)-\d+/\d+/\d+(?::\d+)?(?:\.\d+)?"
        r"|(?:lo|em|fxp|me|reth)\d+(?:\.\d+)?|(?:irb|vlan)\.\d+)"
    ),
    "huawei-vrp": r"(?:100GE|40GE|25GE|10GE|XGE|GE|NULL)\d+(?:/\d+){0,3}(?:\.\d+)?",
}

# Link state: generic rules only fire in an obvious status context ("is up",
# "up/down"); vendors add the bare column words their tables use.
_STATE_CONTEXT = r"(?:(?<=\bis )|(?<=\bare )|(?<=: )|(?<=\bstate )|(?<=\bstatus ))"
_STATE = [
    Rule("state_ok", rf"{_STATE_CONTEXT}[Uu]p{_NOT_WORD}(?! to\b)|{_WORD}up/up{_NOT_WORD}", _GREEN),
    Rule(
        "state_bad",
        rf"{_STATE_CONTEXT}(?:administratively )?[Dd]own{_NOT_WORD}"
        rf"|{_WORD}(?:up/down|down/up|down/down){_NOT_WORD}"
        rf"|{_WORD}(?:err-disabled|notconnect){_NOT_WORD}",
        _RED,
    ),
]
# profile -> (good words, bad words, prefix on bad words, suffix on both)
_EXTRA_STATE = {
    "cisco-ios": (r"up|connected|Established", r"down|Idle", "", ""),
    "junos": (r"up|Up|Established", r"down|Down|Idle", "", ""),
    "huawei-vrp": (r"up|UP", r"down|DOWN", r"[*^]?", r"(?:\([a-z]+\))?"),
}
_ERROR = Rule(
    "error",
    r"(?<![A-Za-z0-9_])(?:%[ ]?(?:Invalid|Incomplete|Ambiguous|Unknown|Bad)|syntax error"
    r"|unknown command|missing argument|[Ee]rror:)[^\r\n]*",
    _RED,
)


def _rules(profile: str) -> List[Rule]:
    rules = [*_MAC, *_IP, _IFACE]
    if profile in _EXTRA_IFACE:
        rules.append(Rule("iface_x", rf"{_BEFORE}{_EXTRA_IFACE[profile]}(?![0-9A-Za-z])", _YELLOW))
    rules += [_VLAN, _MPLS]
    if profile == "junos":
        rules.append(Rule("mpls_x", rf"{_BEFORE}(?:push|swap)[ \t]+\d{{2,7}}(?![0-9.])", _BLUE,
                          _numbers_at_most(_MAX_MPLS_LABEL)))
    if profile in _EXTRA_STATE:
        good, bad, prefix, suffix = _EXTRA_STATE[profile]
        rules.append(Rule("state_ok_x", rf"{_WORD}(?:{good}){suffix}{_NOT_WORD}", _GREEN))
        rules.append(Rule("state_bad_x", rf"{_WORD}{prefix}(?:{bad}){suffix}{_NOT_WORD}", _RED))
    rules += _STATE
    rules.append(_ERROR)
    return rules


class _Compiled(NamedTuple):
    regex: "re.Pattern[bytes]"
    colours: Dict[str, bytes]
    validators: Dict[str, Validator]


def _compile(profile: str) -> _Compiled:
    rules = _rules(profile)
    pattern = "|".join(f"(?P<{r.name}>{r.pattern})" for r in rules)
    return _Compiled(
        re.compile(pattern.encode("ascii")),
        {r.name: b"\x1b[%dm" % r.colour for r in rules},
        {r.name: r.validate for r in rules if r.validate},
    )


_COMPILED: Dict[str, _Compiled] = {}

_NEXT_RE = re.compile(rb"\x1b|\xc2(?:[\x80-\x9f]|\Z)")
_CSI_RE = re.compile(rb"(?:\x1b\[|\xc2\x9b)([0-?]*)([ -/]*)([@-~])")
_STRING_INTRO_RE = re.compile(rb"\x1b([\]PX^_])|\xc2([\x90\x98\x9d\x9e\x9f])")
# Short escapes: anything but the CSI/string introducers [ ] P X ^ _ (ESC) or
# their C1 forms.
_ESC_RE = re.compile(rb"\x1b[ -/]*[0-?@-OQ-WYZ\\`-~]|\xc2[\x80-\x8f\x91-\x97\x99\x9a\x9c]")
_PARTIAL_ESC_RE = re.compile(rb"(?:\x1b(?:\[[0-?]*[ -/]*|[ -/]*)|\xc2(?:\x9b[0-?]*[ -/]*)?)\Z")
# A string ends at ST (and BEL for OSC), is cancelled by CAN/SUB, and is cut
# short by a new ESC (matched without consuming it by the last alternative).
_OSC_END_RE = re.compile(rb"\x07|\x1b\\|\xc2\x9c|[\x18\x1a]|\x1b(?!\\|\Z)")
_ST_END_RE = re.compile(rb"\x1b\\|\xc2\x9c|[\x18\x1a]|\x1b(?!\\|\Z)")
_TAIL_RE = re.compile(rb"[^\x00-\x20\x7f]*\Z")  # trailing run of non-break bytes
_ALT_SCREEN_MODES = {b"47", b"1047", b"1049"}
_CARRY_MAX = 64
_CTX_MAX = 16  # bytes of look-behind kept for _not_a_version across chunks


def normalize_profile(value: object) -> str:
    return value if isinstance(value, str) and value in PROFILES else "generic"


class Highlighter:
    """Stateful stream rewriter; one instance per terminal."""

    def __init__(self, profile: str = "generic", enabled: bool = True) -> None:
        self.enabled = enabled
        self.set_profile(profile)
        self.reset()

    def set_profile(self, profile: str) -> None:
        self.profile = normalize_profile(profile)
        if self.profile not in _COMPILED:
            _COMPILED[self.profile] = _compile(self.profile)
        self._compiled = _COMPILED[self.profile]

    def configure(self, enabled: bool, profile: str) -> bytes:
        """Apply settings without losing stream state.

        Returns bytes that were held back and must now be displayed unchanged
        (non-empty only when switching off mid-escape).
        """
        self.set_profile(profile)
        flushed = b""
        if self.enabled != enabled:
            self._carry = self._ctx = b""
            # The held bytes are on screen after this call either way; keep them
            # for parsing (the rest of the sequence may follow) but never re-emit.
            flushed = self._hold[self._shown:] if self.enabled else b""
            self._shown = len(self._hold)
        self.enabled = enabled
        return flushed

    def reset(self) -> None:
        """Forget stream state (new session, reattach, lost continuity)."""
        self._hold = b""
        self._in_string: Optional[bytes] = None  # terminator kind: b"osc" or b"st"
        self._alt_modes: Set[bytes] = set()
        self._carry = b""
        self._ctx = b""  # plain text before the carry: lookbehind for validators
        self._shown = 0  # leading bytes of _hold already displayed
        self._reset_sgr()

    def _reset_sgr(self) -> None:
        self._fg = self._bg = self._reverse = self._conceal = False

    def feed(self, data: bytes) -> bytes:
        if (
            not self.enabled
            and not self._hold
            and not self._in_string
            and b"\x1b" not in data
            and b"\xc2" not in data  # UTF-8 C1 controls
        ):
            self._carry = self._ctx = b""
            return data  # nothing that changes tracked state: skip the scan
        shown, self._shown = self._shown, 0
        buf = self._hold + bytes(data)
        self._hold = b""
        out: List[bytes] = []
        i, n = 0, len(buf)
        while i < n:
            if self._in_string:
                end = (_OSC_END_RE if self._in_string == b"osc" else _ST_END_RE).search(buf, i)
                if end is None:
                    if buf.endswith((b"\x1b", b"\xc2")):
                        self._hold = buf[-1:]
                        n -= 1
                    out.append(buf[i:n])
                    break
                stop = end.start() if end.group() == b"\x1b" else end.end()
                out.append(buf[i:stop])
                i = stop
                self._in_string = None
                continue
            nxt = _NEXT_RE.search(buf, i)
            stop = n if nxt is None else nxt.start()
            if stop > i:
                out.append(self._text(buf[i:stop], last=stop == n))
                i = stop
            if nxt is None:
                break
            self._carry = self._ctx = b""
            i = self._escape(buf, i, out)
        if not self.enabled:
            self._shown = len(self._hold)  # disabled output shows everything
            return bytes(data)
        result = b"".join(out)
        if shown:
            if len(self._hold) == len(buf):  # still the same unfinished sequence
                self._shown = shown
            else:
                result = result[shown:]  # the old hold was displayed already
        return result

    def _escape(self, buf: bytes, i: int, out: List[bytes]) -> int:
        """Pass one escape sequence through, tracking state; return the new index."""
        m = _CSI_RE.match(buf, i)
        if m is not None:
            self._csi(m.group(1), m.group(2), m.group(3))
        else:
            m = _STRING_INTRO_RE.match(buf, i)
            if m is not None:
                self._in_string = b"osc" if m.group(1) == b"]" or m.group(2) == b"\x9d" else b"st"
            else:
                m = _ESC_RE.match(buf, i)
                if m is not None:
                    if m.group() == b"\x1bc":
                        self.reset()
                elif _PARTIAL_ESC_RE.match(buf, i):
                    self._hold = buf[i:]
                    return len(buf)
                else:
                    out.append(buf[i:i + 1])
                    return i + 1
        out.append(m.group())
        return m.end()

    def _csi(self, params: bytes, inter: bytes, final: bytes) -> None:
        if final == b"m" and not inter:
            self._sgr(params)
        elif final == b"p" and inter == b"!":  # DECSTR resets SGR, not the screen
            self._reset_sgr()
        elif final in (b"h", b"l") and params.startswith(b"?"):
            for mode in set(params[1:].split(b";")) & _ALT_SCREEN_MODES:
                (self._alt_modes.add if final == b"h" else self._alt_modes.discard)(mode)

    def _sgr(self, params: bytes) -> None:
        it = iter(params.split(b";") if params else [b"0"])
        for raw in it:
            # Colon sub-parameters (38:2::R:G:B, 4:3) belong to their own
            # code: take the code, consume nothing else.
            head, colon, _ = raw.partition(b":")
            code = int(head) if head.isdigit() else 0
            if code == 0:
                self._reset_sgr()
            elif code == 7:
                self._reverse = True
            elif code == 27:
                self._reverse = False
            elif code == 8:
                self._conceal = True
            elif code == 28:
                self._conceal = False
            elif code == 39:
                self._fg = False
            elif code == 49:
                self._bg = False
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self._fg = True
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self._bg = True
            elif code in (38, 48, 58):
                if code == 38:
                    self._fg = True
                elif code == 48:
                    self._bg = True
                if not colon:  # semicolon form: 38;5;n / 38;2;r;g;b
                    kind = next(it, b"")
                    for _ in range(3 if kind == b"2" else 1 if kind == b"5" else 0):
                        next(it, None)

    def _text(self, text: bytes, last: bool) -> bytes:
        ctx, carry, self._ctx, self._carry = self._ctx, self._carry, b"", b""
        if (
            not self.enabled
            or self._alt_modes
            or self._fg
            or self._bg
            or self._reverse
            or self._conceal
        ):
            return text
        regex, colours, validators = self._compiled
        full = ctx + carry + text
        skip = len(ctx) + len(carry)
        parts: List[bytes] = []
        pos = 0
        for m in regex.finditer(full):
            if m.end() <= skip:
                continue
            check = validators.get(m.lastgroup)
            if check is not None and not check(full, m):
                continue
            start = max(m.start(), skip)
            parts.append(full[max(pos, skip):start])
            parts.append(colours[m.lastgroup])
            parts.append(full[start:m.end()])
            parts.append(b"\x1b[39m")
            pos = m.end()
        if last:
            tail = _TAIL_RE.search(full, max(0, len(full) - _CARRY_MAX - 1)).group()
            if len(tail) <= _CARRY_MAX:
                self._carry = tail
                keep = len(full) - len(tail)
                self._ctx = full[max(0, keep - _CTX_MAX):keep]
        if not parts:
            return text
        parts.append(full[max(pos, skip):])
        return b"".join(parts)


def apply_config(highlighter: Highlighter, config: object) -> bytes:
    """Sync ``highlighter`` with the terminal highlight settings.

    Returns bytes to display unchanged (see ``Highlighter.configure``).
    """
    get = config.get_setting  # type: ignore[attr-defined]
    return highlighter.configure(
        get("terminal.highlight_enabled", False) is True,
        normalize_profile(get("terminal.highlight_profile", "generic")),
    )

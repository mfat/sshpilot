"""Colour network-device terminal output (IPs, MACs, VLANs, interfaces, ...).

``Highlighter.feed`` rewrites a display byte stream, adding zero-width SGR
escapes around recognised tokens. Authentication success also adds bold.
Colours follow the terminal theme, adjusted for background contrast when the
backend supplies its effective colours. Without a theme, ANSI palette indices
are used. The highlighter restores the default
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

Speed (#1426): this runs on the GUI thread for every output byte, so the two
common cases avoid per-token Python work. A disabled highlighter applies only
the SGR sequences after a chunk's last reset when the chunk holds nothing
else. An enabled one runs only the rules whose ``Rule.needs`` fragments occur
in the text: the full alternation costs about 3 us per byte whatever the text.

This module is pure Python and does not import ``gi``.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Sequence

from .custom_terminal_themes import normalize_color

PROFILES = ("generic", "cisco-ios", "junos", "huawei-vrp")

_RED, _GREEN, _YELLOW, _BLUE, _MAGENTA, _CYAN = 31, 32, 33, 34, 35, 36

RGB = tuple[float, float, float]


def _luminance(rgb: RGB) -> float:
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _readable_colour(rgb: RGB, background: RGB) -> tuple[int, ...]:
    """Keep the theme hue, mixing toward black/white only to reach 4.5:1.

    Check the quantized RGB actually sent to the emulator, not rounded ratios.
    At least one of black/white meets this target for any opaque sRGB background.
    """
    bg = _luminance(background)

    def quantize(ratio: float, target: float) -> tuple[int, ...]:
        return tuple(round(255 * (c * (1 - ratio) + target * ratio)) for c in rgb)

    def readable(colour: tuple[int, ...]) -> bool:
        fg = _luminance(tuple(c / 255 for c in colour))
        return (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05) >= 4.5

    original = quantize(0, 0)
    if readable(original):
        return original
    candidates = []
    for target in (0.0, 1.0):
        if not readable(quantize(1, target)):
            continue
        low, high = 0.0, 1.0
        for _ in range(20):
            mid = (low + high) / 2
            if readable(quantize(mid, target)):
                high = mid
            else:
                low = mid
        candidates.append((high, quantize(high, target)))
    return min(candidates)[1]

_MAX_VLAN = 4094
_MAX_MPLS_LABEL = 1048575

Validator = Callable[[bytes, "re.Match[bytes]"], bool]


class Rule(NamedTuple):
    name: str
    pattern: str
    colour: int
    validate: Optional[Validator] = None
    # Lowercase fragments, one of which every match of ``pattern`` contains
    # (compared against the lowercased text). Text with none of them cannot
    # match, so the rule is left out of the scan. Empty: always scanned.
    needs: tuple[bytes, ...] = ()


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
        needs=(b":", b"-"),
    ),
    Rule(
        "mac_dotted",
        r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{4}(?P<md>[.-])[0-9A-Fa-f]{4}(?P=md)[0-9A-Fa-f]{4}"
        r"(?![0-9A-Za-z])",
        _MAGENTA,
        needs=(b".", b"-"),
    ),
]
_IP = [
    Rule(
        "ipv6",
        r"(?<![0-9A-Za-z:.])(?=[0-9A-Fa-f]{0,4}:)[0-9A-Fa-f:]{2,39}(?:\.\d{1,3}){0,3}"
        r"(?:/\d{1,3})?(?![0-9A-Za-z:])",
        _CYAN,
        _valid_ipv6,
        needs=(b":",),
    ),
    Rule(
        "ipv4",
        rf"(?<![0-9A-Za-z.])(?:{_OCT}\.){{3}}{_OCT}(?:/(?:3[0-2]|[12]?\d))?"
        r"(?![0-9A-Za-z]|\.[0-9])",
        _CYAN,
        _not_a_version,
        needs=(b".",),
    ),
]
_VLAN = Rule(
    "vlan",
    rf"{_BEFORE}(?:[Vv][Ll][Aa][Nn](?:-id)?[ \t]+\d{{1,4}}(?:[,-]\d{{1,4}})*|VLAN\d{{4}})"
    r"(?![0-9A-Za-z])",
    _BLUE,
    _numbers_at_most(_MAX_VLAN, low=1),
    needs=(b"vlan",),
)
_MPLS = Rule(
    "mpls",
    rf"{_BEFORE}[Ll]abel:?[ \t]+\d{{1,7}}(?![0-9.])|{_WORD}(?:implicit|explicit)-null{_NOT_WORD}",
    _BLUE,
    _numbers_at_most(_MAX_MPLS_LABEL),
    needs=(b"label", b"-null"),
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
    needs=(
        b"ethernet", b"port-channel", b"loopback", b"tunnel", b"serial", b"vlan",
        b"eth-trunk", b"management", b"meth", b"gige",
        b"ge-", b"xe-", b"et-", b"fe-", b"ae",
    ),
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
_EXTRA_IFACE_NEEDS: Dict[str, tuple[bytes, ...]] = {
    "cisco-ios": (b"gi", b"te", b"fa", b"et", b"po", b"lo", b"tu", b"hu", b"fo", b"twe", b"vl"),
    "junos": (
        b"so-", b"vcp-", b"lt-", b"pp-", b"ip-", b"gr-", b"st-",
        b"lo", b"em", b"fxp", b"me", b"reth", b"irb.", b"vlan.",
    ),
    "huawei-vrp": (b"ge", b"null"),
}

_STATE_CONTEXT = r"(?:(?<=\bis )|(?<=\bare )|(?<=: )|(?<=\bstate )|(?<=\bstatus ))"
_STATE = [
    Rule("state_ok", rf"{_STATE_CONTEXT}[Uu]p{_NOT_WORD}(?! to\b)|{_WORD}up/up{_NOT_WORD}", _GREEN,
         needs=(b"up",)),
    Rule(
        "state_bad",
        rf"{_STATE_CONTEXT}(?:administratively )?[Dd]own{_NOT_WORD}"
        rf"|{_WORD}(?:up/down|down/up|down/down){_NOT_WORD}"
        rf"|{_WORD}(?:err-disabled|notconnect){_NOT_WORD}",
        _RED,
        needs=(b"down", b"err-disabled", b"notconnect"),
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
    needs=(b"%", b"syntax error", b"unknown command", b"missing argument", b"error:"),
)

# Uppercase labels are conventional log fields. Lowercase words need an
# explicit bracket or level= context to avoid colouring ordinary prose.
_LOG_LEVELS = [
    ("log_error", "EMERGENCY|EMERG|ALERT|CRITICAL|CRIT|FATAL|ERROR|ERR", _RED,
     (b"emerg", b"alert", b"crit", b"fatal", b"err")),
    ("log_warning", "WARNING|WARN", _YELLOW, (b"warn",)),
    ("log_info", "NOTICE|INFO", _CYAN, (b"notice", b"info")),
    ("log_debug", "DEBUG|TRACE", _MAGENTA, (b"debug", b"trace")),
]


def _log_rules() -> List[Rule]:
    return [
        Rule(
            name,
            rf"(?<![\w./-])(?:{levels})(?![\w./-])"
            rf"|(?<=\[)(?i:{levels})(?=\])"
            rf"|(?<=\blevel=)(?i:{levels})(?![\w./-])",
            colour,
            needs=needs,
        )
        for name, levels, colour, needs in _LOG_LEVELS
    ]


def _rules(profile: str) -> List[Rule]:
    rules = [
        Rule(
            "auth_success",
            r"(?m:^(?:debug[123]:[ \t]*)?(?i:Authenticated to|Authentication succeeded"
            r"|Authentication successful)\b[^\r\n]*)",
            _GREEN,
            needs=(b"authenticat",),
        ),
        Rule(
            "auth_key",
            r"(?m:^(?:debug[123]:[ \t]*)?(?:Server accepts key:|Using private key:)"
            r"[^\r\n]*)",
            _CYAN,
            needs=(b"key:",),
        ),
        *_MAC, *_IP, _IFACE,
    ]
    if profile in _EXTRA_IFACE:
        rules.append(Rule("iface_x", rf"{_BEFORE}{_EXTRA_IFACE[profile]}(?![0-9A-Za-z])", _YELLOW,
                          needs=_EXTRA_IFACE_NEEDS[profile]))
    rules += [_VLAN, _MPLS]
    if profile == "junos":
        rules.append(Rule("mpls_x", rf"{_BEFORE}(?:push|swap)[ \t]+\d{{2,7}}(?![0-9.])", _BLUE,
                          _numbers_at_most(_MAX_MPLS_LABEL), needs=(b"push", b"swap")))
    if profile in _EXTRA_STATE:
        good, bad, prefix, suffix = _EXTRA_STATE[profile]
        rules.append(Rule("state_ok_x", rf"{_WORD}(?:{good}){suffix}{_NOT_WORD}", _GREEN,
                          needs=(b"up", b"connected", b"established")))
        rules.append(Rule("state_bad_x", rf"{_WORD}{prefix}(?:{bad}){suffix}{_NOT_WORD}", _RED,
                          needs=(b"down", b"idle")))
    rules += _STATE
    rules += _log_rules()
    rules.append(_ERROR)
    return rules


def _alternation(rules: Sequence[Rule]) -> "re.Pattern[bytes]":
    pattern = "|".join(f"(?P<{r.name}>{r.pattern})" for r in rules)
    return re.compile(pattern.encode("ascii"))


_SUBSET_CACHE_MAX = 128
_SHORT_TEXT_MAX = 48
_SHORT_CACHE_MAX = 1024


class _Compiled:
    """One profile's rules, with the scan narrowed to what a text can match."""

    def __init__(self, profile: str) -> None:
        self.rules = _rules(profile)
        self.regex = _alternation(self.rules)  # every rule; the reference scan
        self.colours = {r.name: b"\x1b[%dm" % r.colour for r in self.rules}
        self.validators = {r.name: r.validate for r in self.rules if r.validate}
        self._always = sum(1 << bit for bit, r in enumerate(self.rules) if not r.needs)
        # fragment -> the rules it lets in; one flat pass over distinct fragments.
        masks: Dict[bytes, int] = {}
        for bit, rule in enumerate(self.rules):
            for fragment in rule.needs:
                masks[fragment] = masks.get(fragment, 0) | 1 << bit
        self._fragments = tuple(masks.items())
        # Text without any byte of any fragment (digits, blanks) needs no pass.
        used = bytes(sorted({b for f in masks for b in f + f.upper()} - {0x20}))
        self._fragment_byte = (
            None if self._always else re.compile(b"[" + re.escape(used) + b"]")
        )
        self._subsets: Dict[int, "re.Pattern[bytes]"] = {}
        # Coloured output arrives as many short runs that repeat (" ", ": ").
        self._short: Dict[bytes, Optional["re.Pattern[bytes]"]] = {}

    def regex_for(self, text: bytes) -> Optional["re.Pattern[bytes]"]:
        """The rules that can match in *text*, in priority order; None if none.

        Dropping a rule that cannot match leaves every match unchanged: the
        alternation is leftmost-first and that branch would have failed.
        """
        if self._fragment_byte is not None and not self._fragment_byte.search(text):
            return None
        if len(text) <= _SHORT_TEXT_MAX:
            try:
                return self._short[text]
            except KeyError:
                if len(self._short) >= _SHORT_CACHE_MAX:
                    self._short.clear()
                regex = self._short[text] = self._select(text)
                return regex
        return self._select(text)

    def _select(self, text: bytes) -> Optional["re.Pattern[bytes]"]:
        low = text.lower()
        key = self._always
        for fragment, mask in self._fragments:
            if fragment in low:
                key |= mask
        if not key:
            return None
        regex = self._subsets.get(key)
        if regex is None:
            if len(self._subsets) >= _SUBSET_CACHE_MAX:
                self._subsets.clear()
            regex = self._subsets[key] = _alternation(
                [r for bit, r in enumerate(self.rules) if key >> bit & 1]
            )
        return regex


def _compile(profile: str) -> _Compiled:
    return _Compiled(profile)


_COMPILED: Dict[str, _Compiled] = {}

_NEXT_RE = re.compile(rb"\x1b|\xc2(?:[\x80-\x9f]|\Z)")
# A chunk whose every ESC starts one of these holds nothing but text and SGR.
_PLAIN_SGR_RE = re.compile(rb"\x1b\[([0-9;:]*)m")
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
_CTX_MAX = 64  # context for validators and authentication prefixes across chunks


def normalize_profile(value: object) -> str:
    return value if isinstance(value, str) and value in PROFILES else "generic"


class Highlighter:
    """Stateful stream rewriter; one instance per terminal."""

    def __init__(self, profile: str = "generic", enabled: bool = True) -> None:
        self.enabled = enabled
        self._theme_colours: Dict[bytes, bytes] = {}
        self.set_profile(profile)
        self.reset()

    def set_theme(self, background: RGB, palette: Sequence[RGB]) -> None:
        """Use the emulator's effective background, including group overrides."""
        self._theme_colours = {}
        for index, fallback in enumerate((
            (0.8, 0.2, 0.2), (0.2, 0.65, 0.3), (0.8, 0.6, 0.1),
            (0.25, 0.4, 0.8), (0.65, 0.3, 0.7), (0.1, 0.6, 0.65),
        ), 1):
            rgb = palette[index] if index < len(palette) else fallback
            colour = _readable_colour(rgb, background)
            self._theme_colours[b"\x1b[%dm" % (30 + index)] = b"\x1b[38;2;%d;%d;%dm" % colour

    def set_theme_hex(self, background: str, palette: Sequence[str]) -> None:
        """Adapter for backends whose theme is represented as CSS hex colours."""
        def rgb(value: str, fallback: str) -> RGB:
            value = normalize_color(value) or fallback
            return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))

        self.set_theme(rgb(background, "#000000"), [rgb(c, "#000000") for c in palette])

    def _colour(self, rule: str) -> bytes:
        colour = self._compiled.colours[rule]
        return self._theme_colours.get(colour, colour)

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
            self._line_rule = None
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
        # Cursor save slots belong to the normal/alternate screen buffers.
        self._saved_sgr: Dict[bool, tuple[bool, ...]] = {}
        self._line_rule: Optional[str] = None
        self._carry = b""
        self._ctx = b""  # plain text before the carry: lookbehind for validators
        self._shown = 0  # leading bytes of _hold already displayed
        self._reset_sgr()

    def _reset_sgr(self) -> None:
        self._fg = self._bg = self._reverse = self._conceal = False
        self._bold = self._faint = False

    def _save_sgr(self) -> None:
        self._saved_sgr[bool(self._alt_modes)] = (
            self._fg, self._bg, self._reverse, self._conceal, self._bold, self._faint,
        )

    def _restore_sgr(self) -> None:
        (self._fg, self._bg, self._reverse, self._conceal,
         self._bold, self._faint) = self._saved_sgr.get(
            bool(self._alt_modes), (False,) * 6,
        )

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
        if (
            not self.enabled
            and not self._hold
            and not self._in_string
            and b"\xc2" not in data
        ):
            sgr = _PLAIN_SGR_RE.findall(data)
            if len(sgr) == data.count(b"\x1b"):
                # Coloured output with highlighting off (#1426): the state
                # after this chunk depends only on the SGRs from its last
                # full reset on, so the rest need no work at all.
                first = len(sgr) - 1
                while first > 0:
                    params = sgr[first]
                    if not params or params == b"0" or params.startswith(b"0;"):
                        break
                    first -= 1
                for params in sgr[first:]:
                    self._sgr(params)
                self._carry = self._ctx = b""
                self._line_rule = None
                return data
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
            sgr = _PLAIN_SGR_RE.match(buf, i)
            if sgr is not None:  # by far the most common escape
                self._sgr(sgr.group(1))
                out.append(sgr.group())
                i = sgr.end()
            else:
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
                    elif m.group() == b"\x1b7":
                        self._save_sgr()
                    elif m.group() == b"\x1b8":
                        self._restore_sgr()
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
        elif not params and not inter and final in (b"s", b"u"):
            (self._save_sgr if final == b"s" else self._restore_sgr)()
        elif final in (b"h", b"l") and params.startswith(b"?"):
            for mode in params[1:].split(b";"):
                if mode == b"1048":
                    (self._save_sgr if final == b"h" else self._restore_sgr)()
                elif mode in _ALT_SCREEN_MODES:
                    if mode == b"1049" and final == b"h":
                        self._save_sgr()
                    (self._alt_modes.add if final == b"h" else self._alt_modes.discard)(mode)
                    if mode == b"1049" and final == b"l":
                        self._restore_sgr()

    def _sgr(self, params: bytes) -> None:
        if not params or params == b"0":
            self._reset_sgr()
            return
        it = iter(params.split(b";") if params else [b"0"])
        for raw in it:
            # Colon sub-parameters (38:2::R:G:B, 4:3) belong to their own
            # code: take the code, consume nothing else.
            head, colon, _ = raw.partition(b":")
            # Remote parameters are untrusted and can exceed Python's integer
            # conversion limit. No supported SGR code needs more than 3 digits.
            number = head.lstrip(b"0") or b"0"
            if not number.isdigit() or len(number) > 3:
                continue
            code = int(number)
            if code == 0:
                self._reset_sgr()
            elif code == 1:
                self._bold = True
            elif code == 2:
                self._faint = True
            elif code == 22:
                self._bold = self._faint = False
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
        if (
            not self.enabled
            or self._alt_modes
            or self._fg
            or self._bg
            or self._reverse
            or self._conceal
        ):
            self._carry = self._ctx = b""
            self._line_rule = None
            return text
        # Only the rules that can match somewhere in this run are scanned; the
        # context and carry are rescanned with its first line, so they count.
        regex = self._compiled.regex_for(self._ctx + self._carry + text)
        if regex is None and not self._line_rule:
            # Nothing to colour: keep only the state the loop below would.
            cut = max(text.rfind(b"\r"), text.rfind(b"\n")) + 1
            if cut:
                self._carry = self._ctx = b""
            if cut < len(text):
                self._tokens(text[cut:], last, None)
            return text
        parts = []
        # Keep full-message emphasis through output chunks, but never let it
        # leak onto the next line. Do not buffer output awaiting a newline.
        for part in re.split(rb"([\r\n]+)", text):
            if not part:
                continue
            if part.startswith((b"\r", b"\n")):
                parts.append(part)
                self._carry = self._ctx = b""
                self._line_rule = None
            elif self._line_rule:
                parts.append(self._emphasize_line(part, self._line_rule))
            else:
                parts.append(self._tokens(part, last, regex))
        return b"".join(parts)

    def _emphasize_line(self, text: bytes, rule: str) -> bytes:
        colour = self._colour(rule)
        if rule != "auth_success":
            return colour + text + b"\x1b[39m"
        # SGR 22 clears both bold and faint. Restore remote faint explicitly,
        # and leave pre-existing bold intact; never use a blanket SGR reset.
        restore = b"" if self._bold else b"\x1b[22m" + (b"\x1b[2m" if self._faint else b"")
        return b"\x1b[1m" + colour + text + b"\x1b[39m" + restore

    def _tokens(
        self, text: bytes, last: bool, regex: Optional["re.Pattern[bytes]"]
    ) -> bytes:
        ctx, carry, self._ctx, self._carry = self._ctx, self._carry, b"", b""
        validators = self._compiled.validators
        full = ctx + carry + text
        skip = len(ctx) + len(carry)
        parts: List[bytes] = []
        pos = 0
        for m in regex.finditer(full) if regex is not None else ():
            if m.end() <= skip:
                continue
            check = validators.get(m.lastgroup)
            if check is not None and not check(full, m):
                continue
            start = max(m.start(), skip)
            parts.append(full[max(pos, skip):start])
            if m.lastgroup in ("auth_success", "auth_key"):
                self._line_rule = m.lastgroup
                parts.append(self._emphasize_line(full[start:m.end()], m.lastgroup))
            else:
                parts.append(self._colour(m.lastgroup))
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

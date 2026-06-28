"""Round-trippable parser/serialiser for OpenSSH ``authorized_keys`` files.

The parser preserves the original line text for any untouched entry so that an
unmodified parse → serialise cycle is byte-identical, including unknown
options (security-critical: a silently dropped option is a regression).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union


# Known SSH public key type prefixes. Anything not in this set is treated as an
# option token rather than a keytype during line parsing.
KEYTYPE_PREFIXES = (
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ssh-ed448",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
)

# Cert variants: same prefixes with -cert-v01@openssh.com suffix.
CERT_SUFFIX = "-cert-v01@openssh.com"


def _is_keytype(token: str) -> bool:
    if token in KEYTYPE_PREFIXES:
        return True
    if token.endswith(CERT_SUFFIX):
        base = token[: -len(CERT_SUFFIX)]
        return base in KEYTYPE_PREFIXES or base in (
            "ssh-rsa",
            "ssh-dss",
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
        )
    return False


# Known option names. Flag options have no '=' value. Value options take a
# quoted string. Repeatable options may appear more than once on a line.
FLAG_OPTIONS = {
    "cert-authority",
    "no-agent-forwarding",
    "no-port-forwarding",
    "no-pty",
    "no-user-rc",
    "no-X11-forwarding",
    "no-x11-forwarding",
    "restrict",
    "agent-forwarding",
    "port-forwarding",
    "pty",
    "user-rc",
    "X11-forwarding",
    "x11-forwarding",
    "verify-required",
    "no-touch-required",
    "touch-required",
}

VALUE_OPTIONS = {
    "command",
    "environment",
    "expiry-time",
    "from",
    "permitlisten",
    "permitopen",
    "principals",
    "tunnel",
}

REPEATABLE_OPTIONS = {"environment", "permitopen", "permitlisten"}

KNOWN_OPTIONS = FLAG_OPTIONS | VALUE_OPTIONS


OptionValue = Union[str, bool]
OptionTuple = Tuple[str, OptionValue]


@dataclass
class AuthorizedKeyEntry:
    """One parsed entry from an authorized_keys file.

    ``raw_line`` is the original input line (without trailing newline). It is
    used to emit byte-identical output when the entry has not been mutated.
    """

    raw_line: str
    disabled: bool
    options: List[OptionTuple]
    unknown_options: List[str]
    keytype: str
    key_b64: str
    comment: str
    fingerprint_sha256: str = ""
    dirty: bool = False

    def mark_dirty(self) -> None:
        self.dirty = True

    def get_option(self, name: str) -> Optional[OptionValue]:
        for n, v in self.options:
            if n == name:
                return v
        return None

    def get_options(self, name: str) -> List[OptionValue]:
        return [v for n, v in self.options if n == name]

    def set_flag(self, name: str, on: bool) -> None:
        self.options = [(n, v) for n, v in self.options if n != name]
        if on:
            self.options.append((name, True))
        self.mark_dirty()

    def set_value(self, name: str, value: Optional[str]) -> None:
        self.options = [(n, v) for n, v in self.options if n != name]
        if value is not None and value != "":
            self.options.append((name, value))
        self.mark_dirty()

    def set_repeatable(self, name: str, values: List[str]) -> None:
        self.options = [(n, v) for n, v in self.options if n != name]
        for v in values:
            if v:
                self.options.append((name, v))
        self.mark_dirty()


PassthroughLine = str
Item = Union[AuthorizedKeyEntry, PassthroughLine]


def _tokenize_options(s: str) -> List[OptionTuple]:
    """Split a comma-separated option list, respecting quoted values."""
    out: List[OptionTuple] = []
    i = 0
    n = len(s)
    while i < n:
        # name
        j = i
        while j < n and s[j] not in (",", "="):
            j += 1
        name = s[i:j]
        if j < n and s[j] == "=":
            # quoted value
            j += 1
            if j < n and s[j] == '"':
                j += 1
                buf = []
                while j < n:
                    c = s[j]
                    if c == "\\" and j + 1 < n:
                        buf.append(s[j + 1])
                        j += 2
                        continue
                    if c == '"':
                        break
                    buf.append(c)
                    j += 1
                value = "".join(buf)
                # skip closing quote
                if j < n and s[j] == '"':
                    j += 1
            else:
                start = j
                while j < n and s[j] != ",":
                    j += 1
                value = s[start:j]
            out.append((name, value))
        else:
            out.append((name, True))
        # consume optional comma + whitespace
        while j < n and s[j] in (",", " ", "\t"):
            j += 1
        i = j
    return out


def _split_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Return (options_str, keytype, key_b64, comment) or None if not a key line."""
    # Skip leading whitespace.
    stripped = line.lstrip()
    if not stripped:
        return None
    n = len(stripped)
    # First, try: does the line start with a keytype? Then options_str = "".
    first_space = stripped.find(" ")
    if first_space == -1:
        return None
    first_token = stripped[:first_space]
    if _is_keytype(first_token):
        keytype = first_token
        rest = stripped[first_space + 1 :].lstrip()
        parts = rest.split(" ", 1)
        if not parts or not parts[0]:
            return None
        key_b64 = parts[0]
        comment = parts[1] if len(parts) > 1 else ""
        return "", keytype, key_b64, comment

    # Otherwise: scan forward to find the keytype, respecting quotes inside
    # option values.
    in_quote = False
    j = 0
    while j < n:
        c = stripped[j]
        if c == "\\" and in_quote and j + 1 < n:
            j += 2
            continue
        if c == '"':
            in_quote = not in_quote
            j += 1
            continue
        if c == " " and not in_quote:
            # Candidate boundary: token from current word-start to j may be
            # the keytype. Look at the next non-space token.
            k = j
            while k < n and stripped[k] == " ":
                k += 1
            # find end of next token
            m = k
            while m < n and stripped[m] != " ":
                m += 1
            tok = stripped[k:m]
            if _is_keytype(tok):
                options_str = stripped[:j]
                rest = stripped[k:]
                parts = rest.split(" ", 2)
                keytype = parts[0]
                if len(parts) < 2:
                    return None
                key_b64 = parts[1]
                comment = parts[2] if len(parts) > 2 else ""
                return options_str, keytype, key_b64, comment
            j = m
            continue
        j += 1
    return None


def compute_fingerprint(keytype: str, key_b64: str) -> str:
    """Return the OpenSSH SHA256 fingerprint of a public key.

    Output format matches ``ssh-keygen -lf``: ``SHA256:<base64-no-padding>``.
    Returns an empty string if the key data is not decodable.
    """
    try:
        raw = base64.b64decode(key_b64, validate=False)
    except Exception:
        return ""
    digest = hashlib.sha256(raw).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


def _classify_options(
    tokens: List[OptionTuple],
) -> Tuple[List[OptionTuple], List[str]]:
    """Split tokens into (known, unknown_raw).

    Unknown options are preserved verbatim as their original token text so
    they can be re-emitted unchanged.
    """
    known: List[OptionTuple] = []
    unknown: List[str] = []
    for name, value in tokens:
        if name in KNOWN_OPTIONS:
            known.append((name, value))
        else:
            tok = _serialize_option(name, value)
            if tok:
                unknown.append(tok)
    return known, unknown


def _serialize_option(name: str, value: OptionValue) -> str:
    if value is True:
        return name
    if value is False:
        # A False flag means the option is *not* set; emitting the bare name
        # would silently re-enable it. Return empty and let the caller filter.
        return ""
    # Escape backslashes and double quotes.
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"'


def parse_file(text: str) -> List[Item]:
    """Parse an authorized_keys file. Returns a list of entries / passthrough lines."""
    items: List[Item] = []
    for raw in text.splitlines():
        line = raw
        stripped = line.strip()
        if not stripped:
            items.append(line)
            continue

        disabled = False
        body = line
        if stripped.startswith("#"):
            # Could be a disabled key entry or just a comment line.
            # Heuristic: try to parse the line with the leading '#' (and any
            # whitespace after it) removed; if we recover a keytype, it's a
            # disabled entry. Otherwise treat as passthrough.
            after_hash = stripped[1:].lstrip()
            candidate = after_hash
            split = _split_line(candidate)
            if split is None:
                items.append(line)
                continue
            disabled = True
            body = candidate

        split = _split_line(body)
        if split is None:
            items.append(line)
            continue

        options_str, keytype, key_b64, comment = split
        if options_str:
            tokens = _tokenize_options(options_str)
            known, unknown = _classify_options(tokens)
        else:
            known, unknown = [], []

        entry = AuthorizedKeyEntry(
            raw_line=line,
            disabled=disabled,
            options=known,
            unknown_options=unknown,
            keytype=keytype,
            key_b64=key_b64,
            comment=comment,
            fingerprint_sha256=compute_fingerprint(keytype, key_b64),
            dirty=False,
        )
        items.append(entry)
    # Preserve trailing newline behavior in serialize via marker.
    if text.endswith("\n"):
        items.append("")  # trailing blank to produce trailing newline
    return items


def _serialize_entry(entry: AuthorizedKeyEntry) -> str:
    if not entry.dirty:
        return entry.raw_line
    # Options: known first (in stored order), then unknown verbatim.
    opt_pieces: List[str] = []
    for name, value in entry.options:
        tok = _serialize_option(name, value)
        if tok:
            opt_pieces.append(tok)
    opt_pieces.extend(entry.unknown_options)
    line_parts: List[str] = []
    if opt_pieces:
        line_parts.append(",".join(opt_pieces))
    line_parts.append(entry.keytype)
    line_parts.append(entry.key_b64)
    if entry.comment:
        line_parts.append(entry.comment)
    body = " ".join(line_parts)
    if entry.disabled:
        body = "# " + body
    return body


def serialize(items: List[Item]) -> str:
    """Serialize parsed items back to text. Round-trips clean parses byte-identically."""
    lines: List[str] = []
    for item in items:
        if isinstance(item, AuthorizedKeyEntry):
            lines.append(_serialize_entry(item))
        else:
            lines.append(item)
    if not lines:
        return ""
    # If the last item is a passthrough empty string, treat it as trailing newline marker.
    if lines and lines[-1] == "":
        return "\n".join(lines[:-1]) + "\n"
    return "\n".join(lines)

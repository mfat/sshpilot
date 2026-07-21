"""SSH command converter.

Converts a complete ``ssh ...`` command line — or a bare ``user@host`` — into a
sshPilot *connection-data* dict. The resulting dict has the same shape that the
connection dialog and :class:`~sshpilot.connection_manager.Connection` consume,
so it can be used to construct a connection or to pre-fill the new-connection
form from a command a user pasted.

This logic was extracted from the former *Quick Connect* feature. Unlike Quick
Connect, the converter does **not** preserve the raw command for verbatim
execution: it fully decomposes the command into structured fields (host, port,
username, identity file, forwardings, proxy jump, X11, agent forwarding, and any
remaining ``-o`` options as ``extra_ssh_config``). Those fields are written to
``~/.ssh/config`` through the normal connection path, keeping the SSH config as
the single source of truth (see ``docs/architecture.md``).

It is currently not wired to any UI. See ``docs/command-converter.md``
for the public API and notes on adding a UI entry point.

Public API
----------
``parse_ssh_command(command_text) -> dict | None``
    Returns a connection-data dict on success, a ``{"error": message}`` dict for
    inputs that are recognised but rejected (e.g. a non-``ssh`` command), or
    ``None`` when the input cannot be parsed at all.
"""

from __future__ import annotations

import re
import shlex
from typing import Optional

from gettext import gettext as _


# SSH single-letter options that consume the following token as their argument.
# Used to skip over option values we do not specifically handle so they are not
# mistaken for the host token.
SSH_OPTIONS_EXPECTING_ARGUMENT = {
    "-b",
    "-B",
    "-c",
    "-D",
    "-E",
    "-e",
    "-F",
    "-I",
    "-J",
    "-L",
    "-l",
    "-m",
    "-O",
    "-o",
    "-p",
    "-Q",
    "-R",
    "-S",
    "-W",
    "-w",
}


# An address token is either an IPv6 literal in brackets [::1] or any
# sequence of characters that contains no unbracketed colon (hostname/IPv4).
_ADDR = r'(?:\[[^\]]*\]|[^\[\]:]+)'
_PORT = r'\d+'

# Matches [bind_addr:]port:host:hostport with full bracket-awareness.
_FORWARD_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')'           # listen_port
    r':(' + _ADDR + r')'          # remote host
    r':(' + _PORT + r')$'         # remote port
)

# Matches [bind_addr:]port for -D (dynamic SOCKS proxy).
_DYNAMIC_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')$'          # port
)

_TRUE_VALUES = frozenset(('yes', 'true', '1', 'on'))
_FALSE_VALUES = frozenset(('no', 'false', '0', 'off'))


def _is_ssh_true(value: str) -> bool:
    return value.lower() in _TRUE_VALUES


def _is_ssh_false(value: str) -> bool:
    return value.lower() in _FALSE_VALUES


def _parse_forward_spec(spec: str, fwd_type: str):
    """Parse -L/-R spec [bind_addr:]port:host:hostport into a forwarding_rules dict.

    Handles IPv6 literals in brackets (e.g. [::1]:8080:localhost:80).
    Returns None if the spec cannot be parsed; callers should preserve the raw
    spec in unparsed_args so the rule is not silently lost.
    """
    m = _FORWARD_RE.match(spec)
    if not m:
        return None
    bind_addr, listen_port, remote_host, remote_port = m.groups()
    rule = {'type': fwd_type, 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(listen_port)}
    if fwd_type == 'local':
        rule['remote_host'] = remote_host
        rule['remote_port'] = int(remote_port)
    else:
        rule['local_host'] = remote_host
        rule['local_port'] = int(remote_port)
    return rule


def _parse_dynamic_spec(spec: str):
    """Parse -D spec [bind_addr:]port into a forwarding_rules dict.

    Handles IPv6 bind addresses in brackets (e.g. [::1]:1080).
    Returns None if the spec cannot be parsed.
    """
    m = _DYNAMIC_RE.match(spec)
    if not m:
        return None
    bind_addr, port = m.groups()
    return {'type': 'dynamic', 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(port)}


def _append_extra_config(data: dict, line: str) -> None:
    """Append a SSH-config-syntax 'Key value' line to extra_ssh_config."""
    existing = data.get("extra_ssh_config", "")
    data["extra_ssh_config"] = (existing + "\n" + line).lstrip("\n")


def _bare_user_host_data(username: str, host: str) -> dict:
    return {
        "nickname": host,
        "host": host,
        "hostname": host,
        "username": username,
        "port": 22,
        "auth_method": 0,
        "key_select_mode": 0,
        "unparsed_args": [],
    }


def _empty_connection_data() -> dict:
    return {
        "nickname": "",
        "host": "",
        "hostname": "",
        "username": "",
        "port": 22,
        "auth_method": 0,
        "key_select_mode": 0,
        "keyfile": "",
        "certificate": "",
        "x11_forwarding": False,
        "forwarding_rules": [],
        "proxy_jump": [],
        "forward_agent": False,
        "extra_ssh_config": "",
        "unparsed_args": [],
    }


def _apply_o_option(data: dict, key: str, value: str) -> None:
    """Apply a single ``-o Key=value`` (or ``Key value``) option to *data*."""
    key_lower = key.lower()
    value = value.strip()
    if key_lower == 'user':
        data["username"] = value
    elif key_lower == 'port':
        try:
            data["port"] = int(value)
        except ValueError:
            pass
    elif key_lower == 'identityfile':
        data["keyfile"] = value
        data["key_select_mode"] = 2
    elif key_lower == 'identitiesonly':
        if _is_ssh_true(value):
            data["key_select_mode"] = 1
        elif _is_ssh_false(value) and data.get("keyfile"):
            data["key_select_mode"] = 2
    elif key_lower == 'forwardagent':
        data["forward_agent"] = _is_ssh_true(value)
    else:
        _append_extra_config(data, f"{key} {value}")


def _set_host_token(data: dict, token: str) -> None:
    """Fill host/username fields from a destination token (``user@host`` or host)."""
    if '@' in token:
        username, host = token.split('@', 1)
        data["username"] = username
        data["host"] = host
        data["hostname"] = host
        data["nickname"] = host
    else:
        data["host"] = token
        data["hostname"] = token
        data["nickname"] = token


def _consume_unknown_option(data: dict, args: list, i: int) -> int:
    """Record an unrecognized flag (and its argument, if any) in unparsed_args.

    Returns the next index to process.
    """
    arg = args[i]
    option_key = arg
    attached_value = ""
    if option_key.startswith('--'):
        option_key, _sep, attached_value = option_key.partition('=')
    elif option_key.startswith('-') and len(option_key) > 2:
        option_key, attached_value = option_key[:2], option_key[2:]

    expects_argument = option_key in SSH_OPTIONS_EXPECTING_ARGUMENT
    if attached_value:
        data["unparsed_args"].append(arg)
        return i + 1

    if expects_argument and i + 1 < len(args) and not args[i + 1].startswith('-'):
        data["unparsed_args"].extend([arg, args[i + 1]])
        return i + 2

    data["unparsed_args"].append(arg)
    return i + 1


def _try_parse_int_option(data: dict, field: str, raw: str) -> bool:
    """Set *field* from *raw* if it parses as an int. Returns True on success."""
    try:
        data[field] = int(raw)
        return True
    except ValueError:
        return False


def _tokenize(raw_command: str) -> list:
    try:
        return shlex.split(raw_command)
    except ValueError:
        return raw_command.split()


def parse_ssh_command(command_text: str) -> Optional[dict]:
    """Parse an SSH command string into connection parameters.

    Returns a connection data dict, a dict with an "error" key for user-visible
    errors, or None when the input cannot be parsed at all.
    Only accepts bare user@host or commands starting with 'ssh'.
    """
    try:
        raw_command = command_text.strip()

        # Allow bare user@host without the 'ssh' prefix
        if '@' in raw_command and ' ' not in raw_command and not raw_command.startswith('ssh'):
            username, host = raw_command.split('@', 1)
            return _bare_user_host_data(username, host) if host else None

        tokens = _tokenize(raw_command)
        if not tokens:
            return None
        if tokens[0] != "ssh":
            return {"error": _("Only SSH commands are allowed. Example: ssh user@host")}

        data = _empty_connection_data()
        args = tokens[1:]
        i = 0
        while i < len(args):
            arg = args[i]
            has_next = i + 1 < len(args)

            if arg == '-p' and has_next and _try_parse_int_option(data, "port", args[i + 1]):
                i += 2
            elif arg == '-i' and has_next:
                data["keyfile"] = args[i + 1]
                data["key_select_mode"] = 2
                i += 2
            elif arg == '-o' and has_next:
                option = args[i + 1]
                if '=' in option:
                    key, value = option.split('=', 1)
                    _apply_o_option(data, key, value)
                i += 2
            elif arg.startswith('-o') and '=' in arg[2:]:
                key, value = arg[2:].split('=', 1)
                _apply_o_option(data, key, value)
                i += 1
            elif arg == '-X':
                data["x11_forwarding"] = True
                i += 1
            elif arg == '-A':
                data["forward_agent"] = True
                i += 1
            elif arg == '-C':
                _append_extra_config(data, "Compression yes")
                i += 1
            elif arg == '-4':
                _append_extra_config(data, "AddressFamily inet")
                i += 1
            elif arg == '-6':
                _append_extra_config(data, "AddressFamily inet6")
                i += 1
            elif arg == '-J' and has_next:
                data["proxy_jump"] = [
                    h.strip() for h in args[i + 1].split(',') if h.strip()
                ]
                i += 2
            elif arg == '-L' and has_next:
                rule = _parse_forward_spec(args[i + 1], 'local')
                if rule:
                    data["forwarding_rules"].append(rule)
                i += 2
            elif arg == '-R' and has_next:
                rule = _parse_forward_spec(args[i + 1], 'remote')
                if rule:
                    data["forwarding_rules"].append(rule)
                i += 2
            elif arg == '-D' and has_next:
                rule = _parse_dynamic_spec(args[i + 1])
                if rule:
                    data["forwarding_rules"].append(rule)
                i += 2
            elif arg.startswith('-p') and _try_parse_int_option(data, "port", arg[2:]):
                i += 1
            elif arg.startswith('-i') and len(arg) > 2:
                data["keyfile"] = arg[2:]
                data["key_select_mode"] = 2
                i += 1
            elif not arg.startswith('-'):
                if not data["host"]:
                    _set_host_token(data, arg)
                else:
                    data["unparsed_args"].append(arg)
                i += 1
            else:
                i = _consume_unknown_option(data, args, i)

        if not data["host"]:
            return None

        if data.get("keyfile") and data.get("key_select_mode", 0) == 0:
            data["key_select_mode"] = 2

        return data

    except Exception:
        # Last-resort fallback for bare user@host that somehow raised
        if '@' in command_text and ' ' not in command_text:
            try:
                username, host = command_text.split('@', 1)
                if host:
                    return _bare_user_host_data(username, host)
            except Exception:
                pass
        return None

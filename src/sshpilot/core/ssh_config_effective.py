"""Canonical GTK-free OpenSSH configuration resolution for the daemon core.

This module owns Include discovery, ``ssh -G`` execution, repeated-option
parsing, and authored/effective comparison.  The legacy top-level
``ssh_config_utils`` module is only a compatibility facade for these read
helpers plus its unrelated atomic editor/validation helpers.
"""

from __future__ import annotations

import glob
import getpass
import logging
import os
import re
import socket
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Union

from sshpilot.platform.process import hidden_console_kwargs

from ..platform.paths import include_base_dir, is_openssh_user_config, openssh_user_dir
from ..ssh_config_document import (
    SSHConfigDocument,
    _split_keyword,
    include_patterns,
    split_config_lines,
    split_host_tokens,
)

logger = logging.getLogger(__name__)

_PATH_DIRECTIVES = frozenset(
    {
        "identityfile",
        "certificatefile",
        "identityagent",
        "pkcs11provider",
        "securitykeyprovider",
        "controlpath",
        "userknownhostsfile",
        "globalknownhostsfile",
        "xauthlocation",
        "revokedhostkeys",
        "include",
    }
)
_TOKEN_RE = re.compile(r"%(.)")


def expand_ssh_tokens(value: str) -> str:
    """Expand host-independent OpenSSH tokens used by Include paths."""
    if not value or "%" not in value:
        return value
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    mapping = {
        "%": "%",
        "d": os.path.expanduser("~"),
        "u": user,
        "i": str(os.getuid()) if hasattr(os, "getuid") else "",
        "l": hostname,
        "L": hostname.split(".", 1)[0],
    }
    return _TOKEN_RE.sub(lambda match: mapping.get(match.group(1), match.group(0)), value)


@dataclass(frozen=True)
class SSHConfigPathDiscovery:
    """Resolved SSH inputs and filesystem paths needed to rediscover them."""

    files: tuple[str, ...]
    watch_paths: FrozenSet[str]
    unreadable_paths: FrozenSet[str]


def _watch_parent_for_pattern(pattern: str) -> str:
    candidate = pattern
    while glob.has_magic(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    if not candidate or glob.has_magic(candidate):
        candidate = os.path.dirname(pattern) or "."
    return os.path.abspath(candidate)


def discover_ssh_config_paths(
    main_path: str, *, max_depth: int = 32, as_openssh: bool = False
) -> SSHConfigPathDiscovery:
    """Resolve the Include graph and retain parents for future glob matches.

    A relative ``Include`` is resolved where the app looks for it (see
    :func:`sshpilot.platform.paths.include_base_dir`). With *as_openssh* it is
    resolved where OpenSSH looks, ``~/.ssh``, wherever the root file lives.
    """
    resolved: List[str] = []
    visited: Set[str] = set()
    watch_paths: Set[str] = set()
    unreadable_paths: Set[str] = set()
    root_absolute = os.path.abspath(os.path.expanduser(os.path.expandvars(main_path)))

    def resolve(path: str, depth: int, stack: List[str]) -> None:
        absolute = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        watch_paths.add(absolute)
        if absolute in stack:
            logger.warning("Include cycle detected: %s -> %s", " -> ".join(stack), absolute)
            return
        if depth > max_depth:
            logger.warning("Maximum include depth (%d) exceeded at %s", max_depth, absolute)
            return
        if absolute in visited:
            return
        try:
            with open(absolute, encoding="utf-8", newline="") as handle:
                lines = split_config_lines(handle.read())
        except (OSError, UnicodeError) as exc:
            logger.warning("Cannot read SSH config include %s: %s", absolute, exc)
            if os.path.exists(absolute):
                unreadable_paths.add(absolute)
            return
        visited.add(absolute)
        resolved.append(absolute)
        if as_openssh:
            base_dir = str(openssh_user_dir())
        else:
            base_dir = include_base_dir(root_absolute, absolute)
        stack.append(absolute)
        for raw_line in lines:
            try:
                patterns = include_patterns(raw_line)
            except ValueError:
                continue
            if patterns is None:
                continue
            for pattern in patterns:
                expanded = os.path.expanduser(
                    os.path.expandvars(expand_ssh_tokens(pattern))
                )
                if not os.path.isabs(expanded):
                    expanded = os.path.abspath(os.path.join(base_dir, expanded))
                else:
                    expanded = os.path.abspath(expanded)
                if glob.has_magic(expanded):
                    watch_paths.add(_watch_parent_for_pattern(expanded))
                else:
                    watch_paths.add(expanded)
                    watch_paths.add(os.path.dirname(expanded) or ".")
                for matched in sorted(glob.glob(expanded)):
                    if os.path.isdir(matched):
                        watch_paths.add(os.path.abspath(matched))
                        for child in sorted(glob.glob(os.path.join(matched, "*"))):
                            resolve(child, depth + 1, stack)
                    else:
                        resolve(matched, depth + 1, stack)
        stack.pop()

    resolve(main_path, 1, [])
    return SSHConfigPathDiscovery(
        files=tuple(resolved),
        watch_paths=frozenset(watch_paths),
        unreadable_paths=frozenset(unreadable_paths),
    )


def openssh_reachable_files(root_path: str) -> FrozenSet[str]:
    """The files ``ssh -F <root_path>`` reads: the root and what it includes.

    Differs from what the app loads only for a root outside ``~/.ssh`` that
    uses a relative ``Include``: the app finds the file next to the root,
    OpenSSH looks in ``~/.ssh`` and does not. A host declared in such a file
    cannot be resolved through the root.
    """
    try:
        return frozenset(
            discover_ssh_config_paths(str(root_path), as_openssh=True).files
        )
    except Exception:
        logger.debug("Could not resolve what OpenSSH reads", exc_info=True)
        return frozenset()


def launch_config_file(root_path, source_path=None) -> Optional[str]:
    """The ``-F`` argument for resolving a saved host, or ``None`` for no ``-F``.

    ``None`` when the root is the user's own ``~/.ssh/config``: plain
    ``ssh host`` reads that file and then ``/etc/ssh/ssh_config``, and ``-F``
    would drop the system file. Any other root is passed whole, so its
    ``Host *`` and ``Match`` blocks apply to hosts declared in included
    files too.

    *source_path* is the file that declared the host. It is passed instead
    of the root only when OpenSSH cannot reach it from the root (see
    :func:`openssh_reachable_files`); the root's own blocks then do not
    apply to that host, which is logged.
    """
    root = os.path.abspath(os.path.expanduser(str(root_path))) if root_path else ""
    source = os.path.abspath(os.path.expanduser(str(source_path))) if source_path else ""
    if root and is_openssh_user_config(root):
        return None
    if root and os.path.isfile(root):
        if not source or source == root or source in openssh_reachable_files(root):
            return root
        logger.warning(
            "SSH config %s is not reachable by OpenSSH from %s (a relative "
            "Include is resolved in ~/.ssh); resolving its hosts from that "
            "file alone",
            source,
            root,
        )
    if source and os.path.isfile(source):
        return source
    return None


def resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    return list(discover_ssh_config_paths(main_path, max_depth=max_depth).files)


def _resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    """Private compatibility name used by older core tests."""
    return resolve_ssh_config_files(main_path, max_depth=max_depth)


def _safe_host_identifier(host: str) -> str:
    value = (host or "").strip()
    if not value or "\x00" in value or value.startswith("-"):
        return ""
    return value


def collect_host_block_lines(
    host_identifier: str, ssh_config_path: Optional[str] = None
) -> List[str]:
    host_identifier = _safe_host_identifier(host_identifier)
    if not host_identifier:
        return []
    config_path = ssh_config_path or os.path.expanduser("~/.ssh/config")
    try:
        files = resolve_ssh_config_files(config_path)
    except Exception:
        files = [config_path]
    combined: List[str] = []
    for path in files:
        try:
            if not path or not os.path.exists(path):
                continue
            document = SSHConfigDocument.parse_file(path)
        except (OSError, UnicodeDecodeError):
            continue
        for block in document.host_blocks(host_identifier):
            combined.extend(line.rstrip("\r\n") for line in block.lines)
    return combined


def get_effective_ssh_config(
    host: str,
    config_file: Optional[str] = None,
    *,
    user: Optional[str] = None,
    port: Optional[int] = None,
    proxy_jump: Optional[str] = None,
    overrides: Optional[Sequence[str]] = None,
) -> Dict[str, Union[str, List[str]]]:
    """Resolve *host* through OpenSSH without allowing option confusion.

    *overrides* is the composed Settings ▸ SSH Options fragment
    (``ssh.ssh_overrides``). A launch puts it ahead of the connection's own
    options, so a query that wants what the launch will really use (which
    account, which port, which jump host) has to pass it too. Only its
    ``-o Keyword=value`` pairs are used.
    """
    host = _safe_host_identifier(host)
    if not host:
        return {}
    command = ["ssh"]
    if config_file:
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(config_file)))
        if not os.path.isfile(expanded):
            logger.warning("Requested SSH config override %s does not exist", expanded)
            return {}
        command.extend(["-F", expanded])
    if overrides:
        from .settings.ssh_overrides import select_override_args

        command.extend(select_override_args(list(overrides), flags=False))
    if user:
        command.extend(["-o", f"User={user}"])
    if port is not None:
        command.extend(["-p", str(port)])
    if proxy_jump:
        command.extend(["-o", f"ProxyJump={proxy_jump}"])
    command.extend(["-G", host])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            **hidden_console_kwargs(),
        )
    except Exception:
        return {}
    config: Dict[str, Union[str, List[str]]] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key = key.lower()
        value = value.strip()
        previous = config.get(key)
        if previous is None:
            config[key] = value
        elif isinstance(previous, list):
            previous.append(value)
        else:
            config[key] = [previous, value]
    return config


def _effective_config_lines(config: Dict[str, Union[str, List[str]]]) -> List[str]:
    lines: List[str] = []
    for key in sorted(config):
        values = config[key]
        if isinstance(values, list):
            lines.extend(f"{key} {value}" for value in values)
        else:
            lines.append(f"{key} {values}")
    return lines


def _authored_directives(block_text: str) -> Set[str]:
    names: Set[str] = set()
    for raw_line in split_config_lines(block_text):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        token = re.split(r"[\s=]", line, maxsplit=1)[0].lower()
        if token not in {"host", "match"}:
            names.add(token)
    return names


def diff_effective_config(
    host: str,
    config_file: Optional[str],
    own_block_text: str,
    *,
    include_system_defaults_in_display: Optional[bool] = None,
    overrides: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, object]]:
    """Compare an authored block against the daemon-selected OpenSSH config.

    ``config_file`` anchors both sides of the *diff* (own vs. full) to a
    specific root, e.g. the daemon-selected SSH root, so accumulated globals
    from an unrelated file never leak in as spurious changes. Whether the
    *displayed* full side additionally reflects system-wide config
    (``/etc/ssh/ssh_config``) is controlled separately by
    ``include_system_defaults_in_display`` (defaults to ``config_file is
    None`` for callers that don't care) — real ``ssh -G`` invocations made
    without ``-F`` pick up the system-wide config, so the display should
    match that whenever the app itself launches sessions without ``-F``.

    *overrides* is what a launch sends ahead of the connection's own options
    (Settings ▸ SSH Options). It is applied to the effective sides only, never
    to the authored block, so an option forced there shows up as a difference
    from what the Host block says.
    """
    if not _safe_host_identifier(host):
        return None
    if include_system_defaults_in_display is None:
        include_system_defaults_in_display = config_file is None
    fd, temporary_path = tempfile.mkstemp(prefix=".sshpilot-own-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(own_block_text)
        own = get_effective_ssh_config(host, config_file=temporary_path)
        if config_file is None:
            config_file = os.path.expanduser("~/.ssh/config")
        compare_file = config_file if os.path.isfile(config_file) else temporary_path
        full = get_effective_ssh_config(
            host, config_file=compare_file, overrides=overrides
        )
        display = (
            get_effective_ssh_config(host, overrides=overrides)
            if include_system_defaults_in_display
            else full
        )
    finally:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
    if not own or not full:
        return None
    display = display or full

    def expand_paths(config):
        result = {}
        for key, value in config.items():
            if key not in _PATH_DIRECTIVES:
                result[key] = value
            elif isinstance(value, list):
                result[key] = [os.path.expanduser(item) for item in value]
            elif isinstance(value, str):
                result[key] = os.path.expanduser(value)
            else:
                result[key] = value
        return result

    full, own, display = map(expand_paths, (full, own, display))

    def as_list(value) -> List[str]:
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    authored = _authored_directives(own_block_text)
    changes: List[Dict[str, object]] = []
    for key in sorted(set(full) | set(own)):
        own_values = as_list(own.get(key))
        full_values = as_list(full.get(key))
        if own_values == full_values:
            continue
        if key not in authored:
            changes.append(
                {
                    "key": key,
                    "own": [],
                    "effective": full_values,
                    "added": full_values,
                    "removed": [],
                    "kind": "added",
                }
            )
            continue
        own_counts = Counter(own_values)
        full_counts = Counter(full_values)
        added = list((full_counts - own_counts).elements())
        removed = list((own_counts - full_counts).elements())
        kind = (
            "overridden"
            if added and removed
            else "added"
            if added
            else "removed"
            if removed
            else "overridden"
        )
        changes.append(
            {
                "key": key,
                "own": own_values,
                "effective": full_values,
                "added": added,
                "removed": removed,
                "kind": kind,
            }
        )
    return {
        "has_diff": bool(changes),
        "changes": changes,
        "own": _effective_config_lines(own),
        "full": _effective_config_lines(display),
    }


# ---------------------------------------------------------------------------
# What OpenSSH reads before a host's own block
# ---------------------------------------------------------------------------

#: Options OpenSSH collects instead of keeping the first one: an earlier block
#: adds to these and takes nothing away from the host's own lines.
ACCUMULATING_DIRECTIVES = frozenset(
    {
        "identityfile",
        "certificatefile",
        "localforward",
        "remoteforward",
        "dynamicforward",
        "sendenv",
        "setenv",
        "permitremoteopen",
    }
)


def _host_pattern_matches(pattern: str, host: str) -> bool:
    """OpenSSH ``match_pattern``: only ``*`` and ``?`` are special."""
    expression = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(expression, host, flags=re.IGNORECASE) is not None


def _host_header_matches(tokens: Sequence[str], host: str) -> bool:
    """Whether a ``Host`` line applies to *host* (a negated match vetoes)."""
    matched = False
    for token in tokens:
        if token.startswith("!"):
            if _host_pattern_matches(token[1:], host):
                return False
        elif _host_pattern_matches(token, host):
            matched = True
    return matched


def _match_header_state(remainder: str, host: str) -> Optional[bool]:
    """``True``/``False`` when a ``Match`` line can be decided here, else ``None``.

    Only ``all`` and ``originalhost`` depend on nothing but the name given to
    ``ssh``. Every other criterion (``host``, ``user``, ``exec``, ``final``,
    ...) needs what OpenSSH resolves or runs, so the block stays undecided.
    """
    try:
        tokens = split_host_tokens(remainder)
    except Exception:
        return None
    lowered = [token.lower() for token in tokens]
    if lowered == ["all"]:
        return True
    if len(tokens) == 2 and lowered[0] == "originalhost":
        return _host_header_matches(tokens[1].split(","), host)
    return None


def _unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def earlier_directives(
    host: str,
    root_path: str,
    effective: Optional[Dict[str, Union[str, List[str]]]] = None,
    *,
    max_depth: int = 32,
) -> List[Dict[str, object]]:
    """Options OpenSSH obtains for *host* before it reaches the host's own block.

    OpenSSH keeps the first value it obtains for an option. A block that
    applies to *host* and comes before the host's own ``Host`` block therefore
    decides every option it sets, whatever the own block says. This walks the
    configuration in the order OpenSSH reads it (an ``Include`` is read where
    it stands) and returns, for each such option, the first line that sets
    it::

        {"key": "user", "value": "deploy", "block": "Host *",
         "file": "/home/me/.ssh/config", "line": 3}

    ``block`` is empty for a line above every ``Host``/``Match`` line. The
    walk stops at the first ``Host`` line that names *host* literally; when
    there is none, nothing is "earlier" and the result is empty.

    A ``Match`` block whose criteria cannot be judged here is reported only
    when *effective* (``ssh -G`` for the host) shows the value its line sets,
    which is the evidence that it applied. Options OpenSSH accumulates are
    never reported.
    """
    host = _safe_host_identifier(host)
    if not host or not root_path:
        return []
    root = os.path.abspath(os.path.expanduser(str(root_path)))
    found: Dict[str, Dict[str, object]] = {}
    seen_keys: Set[str] = set()
    reached_own = False

    def effective_value(key: str) -> str:
        value = (effective or {}).get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    def walk(path: str, active: Optional[bool], block: str, depth: int, stack) -> None:
        nonlocal reached_own
        absolute = os.path.abspath(path)
        if reached_own or depth > max_depth or absolute in stack:
            return
        try:
            with open(absolute, encoding="utf-8", newline="") as handle:
                lines = split_config_lines(handle.read())
        except (OSError, UnicodeError):
            return
        base_dir = include_base_dir(root, absolute)
        stack = stack + [absolute]
        for number, raw_line in enumerate(lines, start=1):
            if reached_own:
                return
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            keyword, remainder = _split_keyword(line)
            if keyword == "host":
                try:
                    tokens = split_host_tokens(remainder)
                except Exception:
                    tokens = remainder.split()
                if any(token.lower() == host.lower() for token in tokens):
                    reached_own = True
                    return
                active = _host_header_matches(tokens, host)
                block = "Host " + remainder.strip()
                continue
            if keyword == "match":
                active = _match_header_state(remainder, host)
                block = "Match " + remainder.strip()
                continue
            if active is False:
                continue
            if keyword == "include":
                try:
                    patterns = include_patterns(line) or []
                except ValueError:
                    continue
                for pattern in patterns:
                    expanded = os.path.expanduser(
                        os.path.expandvars(expand_ssh_tokens(pattern))
                    )
                    if not os.path.isabs(expanded):
                        expanded = os.path.join(base_dir, expanded)
                    for matched in sorted(glob.glob(os.path.abspath(expanded))):
                        children = (
                            sorted(glob.glob(os.path.join(matched, "*")))
                            if os.path.isdir(matched)
                            else [matched]
                        )
                        for child in children:
                            walk(child, active, block, depth + 1, stack)
                            if reached_own:
                                return
                continue
            if keyword in ACCUMULATING_DIRECTIVES or keyword in seen_keys:
                continue
            value = _unquoted(remainder)
            if not value:
                continue
            if active is None:
                # Undecided Match block: believed only on OpenSSH's evidence.
                if effective_value(keyword).lower() != value.lower():
                    continue
            seen_keys.add(keyword)
            found[keyword] = {
                "key": keyword,
                "value": value,
                "block": block,
                "file": absolute,
                "line": number,
            }

    walk(root, True, "", 1, [])
    if not reached_own:
        return []
    return [found[key] for key in sorted(found)]

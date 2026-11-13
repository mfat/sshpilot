"""Canonical GTK-free OpenSSH configuration resolution for the daemon core.

This module owns Include discovery, ``ssh -G`` execution, repeated-option
parsing, and authored/effective comparison.  The legacy top-level
``ssh_config_utils`` module is only a compatibility facade for these read
helpers plus its unrelated atomic editor/validation helpers.
"""

from __future__ import annotations

import functools
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


class ConfigScanCache:
    """Each file of one configuration, read and parsed once.

    A scan over every saved host (``list_config_conflicts``) asks the same
    questions of the same files once per host; without this it re-reads and
    re-parses the whole include tree each time, which grows with the square
    of the number of hosts. Valid for one scan only: it never notices a file
    changing.
    """

    def __init__(self) -> None:
        self._files: Dict[str, List[str]] = {}
        self._documents: Dict[str, Optional[SSHConfigDocument]] = {}
        self._lines: Dict[str, Optional[list]] = {}
        self._includes: Dict[tuple, List[str]] = {}
        self._blocks: Dict[str, Dict[str, list]] = {}
        self._base_dirs: Dict[tuple, str] = {}

    def include_base(self, root: str, path: str) -> str:
        key = (root, path)
        if key not in self._base_dirs:
            self._base_dirs[key] = include_base_dir(root, path)
        return self._base_dirs[key]

    def config_files(self, root: str) -> List[str]:
        if root not in self._files:
            try:
                self._files[root] = resolve_ssh_config_files(root)
            except Exception:
                self._files[root] = [root]
        return self._files[root]

    def document(self, path: str) -> Optional[SSHConfigDocument]:
        if path not in self._documents:
            try:
                self._documents[path] = (
                    SSHConfigDocument.parse_file(path) if os.path.exists(path) else None
                )
            except (OSError, UnicodeDecodeError):
                self._documents[path] = None
        return self._documents[path]

    def host_blocks(self, path: str, token: str) -> list:
        """``document(path).host_blocks(token)``, from an index built once."""
        if path not in self._blocks:
            index: Dict[str, list] = {}
            document = self.document(path)
            for block in (document.host_blocks() if document is not None else ()):
                for name in dict.fromkeys(block.tokens):
                    index.setdefault(name, []).append(block)
            self._blocks[path] = index
        return self._blocks[path].get(token, [])

    def parsed_lines(self, path: str) -> Optional[list]:
        if path not in self._lines:
            self._lines[path] = _parse_walk_lines(path)
        return self._lines[path]

    def include_children(self, base_dir: str, line: str) -> List[str]:
        key = (base_dir, line)
        if key not in self._includes:
            self._includes[key] = _include_children(base_dir, line)
        return self._includes[key]


def collect_host_block_lines(
    host_identifier: str,
    ssh_config_path: Optional[str] = None,
    *,
    cache: Optional["ConfigScanCache"] = None,
) -> List[str]:
    host_identifier = _safe_host_identifier(host_identifier)
    if not host_identifier:
        return []
    config_path = ssh_config_path or os.path.expanduser("~/.ssh/config")
    if cache is not None:
        files = cache.config_files(config_path)
    else:
        try:
            files = resolve_ssh_config_files(config_path)
        except Exception:
            files = [config_path]
    combined: List[str] = []
    for path in files:
        if cache is not None:
            if not path:
                continue
            for block in cache.host_blocks(path, host_identifier):
                combined.extend(line.rstrip("\r\n") for line in block.lines)
            continue
        else:
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


@functools.lru_cache(maxsize=4096)
def _host_pattern_regex(pattern: str) -> "re.Pattern[str]":
    expression = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    )
    return re.compile(expression, flags=re.IGNORECASE)


def _host_pattern_matches(pattern: str, host: str) -> bool:
    """OpenSSH ``match_pattern``: only ``*`` and ``?`` are special."""
    return _host_pattern_regex(pattern).fullmatch(host) is not None


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


def _parse_walk_lines(path: str) -> Optional[list]:
    """One tuple per line: ``(number, keyword, remainder, tokens, line,
    names, block_end)``.

    For a ``Host`` line, *tokens* are its patterns and *names* their
    lower-case set when every one is a literal name (no ``*``, ``?``, ``!``),
    else None; *block_end* is the index of the next ``Host``/``Match`` line.
    A literal-only block applies to the hosts it names and to no other, so
    a walk for another host can jump straight to *block_end*. Blank lines
    and comments are dropped. None when the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            lines = split_config_lines(handle.read())
    except (OSError, UnicodeError):
        return None
    parsed = []
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, remainder = _split_keyword(line)
        tokens = None
        names = None
        if keyword == "host":
            try:
                tokens = tuple(split_host_tokens(remainder))
            except Exception:
                tokens = tuple(remainder.split())
            if not any(char in token for token in tokens for char in "*?!"):
                names = frozenset(token.lower() for token in tokens)
        parsed.append([number, keyword, remainder, tokens, line, names, 0])
    block_end = len(parsed)
    for index in range(len(parsed) - 1, -1, -1):
        parsed[index][6] = block_end
        if parsed[index][1] in ("host", "match"):
            block_end = index
    return [tuple(item) for item in parsed]


def _include_children(base_dir: str, line: str) -> List[str]:
    """The files an ``Include`` line reads, in the order OpenSSH reads them."""
    try:
        patterns = include_patterns(line) or []
    except ValueError:
        return []
    children: List[str] = []
    for pattern in patterns:
        expanded = os.path.expanduser(os.path.expandvars(expand_ssh_tokens(pattern)))
        if not os.path.isabs(expanded):
            expanded = os.path.join(base_dir, expanded)
        for matched in sorted(glob.glob(os.path.abspath(expanded))):
            if os.path.isdir(matched):
                children.extend(sorted(glob.glob(os.path.join(matched, "*"))))
            else:
                children.append(matched)
    return children


def earlier_directives(
    host: str,
    root_path: str,
    effective: Optional[Dict[str, Union[str, List[str]]]] = None,
    *,
    max_depth: int = 32,
    new_host: bool = False,
    _whole: bool = False,
    _undecided: Optional[Set[str]] = None,
    cache: Optional[ConfigScanCache] = None,
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
    host's own blocks are those whose ``Host`` line names *host* literally;
    when there is none, nothing is "earlier" and the result is empty.

    A host may have several own blocks. Every applicable line before the
    first one is reported. A line after it, in a block that is not the
    host's (``Host *``), is reported only when a later own block sets the
    same option: OpenSSH has then already taken that line's value.

    *new_host* asks about a host that has no block yet. The app appends a
    new block at the end of the root file, so every applicable line of the
    whole configuration comes before it.

    A ``Match`` block whose criteria cannot be judged here is reported only
    when *effective* (``ssh -G`` for the host) shows the value its line sets,
    which is the evidence that it applied. Options OpenSSH accumulates are
    never reported.
    """
    host = _safe_host_identifier(host)
    if not host or not root_path:
        return []
    root = os.path.abspath(os.path.expanduser(str(root_path)))
    host_lower = host.lower()
    found: Dict[str, Dict[str, object]] = {}
    seen_keys: Set[str] = set()
    reached_own = False
    # After the first own block: the first non-own line per option, kept in
    # case a later own block sets that option too.
    pending: Dict[str, Dict[str, object]] = {}
    own_keys: Set[str] = set()

    def effective_value(key: str) -> str:
        value = (effective or {}).get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    def walk(
        path: str, active: Optional[bool], block: str, own: bool, depth: int, stack
    ) -> None:
        nonlocal reached_own
        absolute = os.path.abspath(path)
        if depth > max_depth or absolute in stack:
            return
        lines = (
            cache.parsed_lines(absolute) if cache is not None
            else _parse_walk_lines(absolute)
        )
        if lines is None:
            return
        base_dir = (
            cache.include_base(root, absolute) if cache is not None
            else include_base_dir(root, absolute)
        )
        stack = stack + [absolute]
        index = 0
        while index < len(lines):
            number, keyword, remainder, tokens, line, names, block_end = lines[index]
            index += 1
            if keyword == "host":
                if names is not None and host_lower not in names:
                    # Names other hosts only: nothing in it applies.
                    # (No label: the next header replaces it before any use.)
                    own = False
                    active = False
                    index = block_end
                    continue
                own = not _whole and any(
                    token.lower() == host_lower for token in tokens
                )
                if own:
                    reached_own = True
                active = True if own else _host_header_matches(tokens, host)
                block = "Host " + remainder.strip()
                continue
            if keyword == "match":
                own = False
                active = _match_header_state(remainder, host)
                block = "Match " + remainder.strip()
                continue
            if active is False:
                continue
            if keyword == "include":
                children = (
                    cache.include_children(base_dir, line) if cache is not None
                    else _include_children(base_dir, line)
                )
                for child in children:
                    walk(child, active, block, own, depth + 1, stack)
                continue
            if keyword in ACCUMULATING_DIRECTIVES:
                continue
            value = _unquoted(remainder)
            if not value:
                continue
            if own:
                if keyword in seen_keys:
                    # Something before this line set it first. When that was
                    # a line after the first own block, it beats this one.
                    if keyword in pending and keyword not in own_keys:
                        found[keyword] = pending.pop(keyword)
                    continue
                seen_keys.add(keyword)
                own_keys.add(keyword)
                continue
            if keyword in seen_keys:
                continue
            if active is None:
                # Undecided Match block: believed only on OpenSSH's evidence.
                if effective_value(keyword).lower() != value.lower():
                    continue
                if _undecided is not None:
                    _undecided.add(keyword)
            seen_keys.add(keyword)
            entry = {
                "key": keyword,
                "value": value,
                "block": block,
                "file": absolute,
                "line": number,
            }
            if reached_own:
                pending[keyword] = entry
            else:
                found[keyword] = entry

    walk(root, True, "", False, 1, [])
    if not reached_own and not new_host and not _whole:
        return []
    return [found[key] for key in sorted(found)]


def configured_directives(
    host: str,
    root_path: str,
    effective: Optional[Dict[str, Union[str, List[str]]]] = None,
    *,
    _undecided: Optional[Set[str]] = None,
) -> Dict[str, Dict[str, object]]:
    """The first line of the configuration that sets each option for *host*.

    Like :func:`earlier_directives`, over the whole file: the host's own
    block and everything after it count too. This is the only way to tell an
    option the configuration sets to its default from one it does not set at
    all -- ``ssh -G`` prints both the same (``serveraliveinterval 0``).
    Options OpenSSH accumulates are not reported.

    A line in a ``Match`` block this walk cannot judge (``exec``, ``host``,
    ``user``, ...) is believed when ``ssh -G`` shows its value, and its key is
    added to *_undecided*: for a default value that evidence proves nothing.
    :func:`launch_configured_directives` settles those keys with OpenSSH.
    """
    return {
        str(item["key"]): item
        for item in earlier_directives(
            host, root_path, effective, _whole=True, _undecided=_undecided
        )
    }


_YES_NO_LIKE = frozenset({"yes", "no", "ask", "confirm", "none"})


def normalized_directive_value(keyword: str, value: str) -> str:
    """*value* in the form two spellings of one setting share.

    Host names and yes/no-like words are case-insensitive to OpenSSH, and a
    jump list may be separated by commas or blanks. Anything else is compared
    as written.
    """
    value = str(value).strip()
    if keyword.lower() == "proxyjump":
        return ",".join(part for part in re.split(r"[\s,]+", value) if part)
    if keyword.lower() == "hostname" or value.lower() in _YES_NO_LIKE:
        return value.lower()
    return value


def own_block_values(own_lines: Sequence[str]) -> Dict[str, str]:
    """The first value each option gets in a host's own block lines.

    *own_lines* is what :func:`collect_host_block_lines` returns: the
    ``Host`` header lines are skipped, and so are the options OpenSSH
    collects, since an earlier block cannot take those away.
    """
    values: Dict[str, str] = {}
    for raw_line in own_lines:
        line = str(raw_line).strip()
        if not line or line.startswith("#"):
            continue
        keyword, remainder = _split_keyword(line)
        if keyword in {"host", "match"} or keyword in ACCUMULATING_DIRECTIVES:
            continue
        value = _unquoted(remainder)
        if keyword and value and keyword not in values:
            values[keyword] = value
    return values


def losing_directives(
    host: str,
    root_path: str,
    own_lines: Sequence[str],
    *,
    cache: Optional[ConfigScanCache] = None,
) -> List[Dict[str, object]]:
    """Options the host's own block sets but an earlier block decides.

    One entry per option, as :func:`earlier_directives` reports it plus
    ``own``, the value the host's block gives that is never used. Equal
    values are not a loss. No ``ssh -G`` is run: a ``Match`` block whose
    criteria cannot be judged from the file is left out, so every entry is
    certain.
    """
    own = own_block_values(own_lines)
    if not own:
        return []
    found = []
    for item in earlier_directives(host, root_path, cache=cache):
        key = str(item["key"])
        mine = own.get(key)
        if mine is None:
            continue
        if normalized_directive_value(key, mine) == normalized_directive_value(
            key, str(item["value"])
        ):
            continue
        found.append(dict(item, own=mine))
    return found


#: A value no one configures, per option :func:`launch_configured_directives`
#: can ask OpenSSH about. Only the keepalive pair needs asking: it is what the
#: app fills in when the configuration sets nothing.
_UNSET_PROBE_SENTINELS = {
    "serveraliveinterval": "86399",
    "serveralivecountmax": "86399",
}


def launch_configured_directives(
    host: str,
    launch_file: Optional[str],
    effective: Optional[Dict[str, Union[str, List[str]]]] = None,
) -> Dict[str, Dict[str, object]]:
    """What the files a launch makes OpenSSH read set for *host*.

    *launch_file* is the ``-F`` file of the launch (see
    :func:`launch_config_file`); ``None`` means no ``-F``, so OpenSSH reads
    ``~/.ssh/config`` and then the system-wide file.

    A key only an undecided ``Match`` block sets, with the value ``ssh -G``
    shows anyway, is put to OpenSSH: the host is resolved through a wrapper
    that includes those files and then sets a sentinel. OpenSSH keeps the
    first value it obtains, so the sentinel comes out exactly when no line of
    the configuration applied -- whatever ``Match exec`` or ``Match host``
    decided. This costs one more ``ssh -G`` (which runs ``Match exec``
    commands again), so it runs only for such a key.
    """
    from ..platform.paths import openssh_user_dir

    files = [launch_file] if launch_file else [
        str(openssh_user_dir() / "config"), openssh_system_config(),
    ]
    files = [os.path.abspath(os.path.expanduser(str(f))) for f in files if f]
    found: Dict[str, Dict[str, object]] = {}
    undecided: Set[str] = set()
    for path in files:
        for key, item in configured_directives(
            host, path, effective, _undecided=undecided
        ).items():
            found.setdefault(key, item)
    ask = sorted(key for key in undecided if key in found and key in _UNSET_PROBE_SENTINELS)
    if ask:
        for key in _keys_openssh_leaves_unset(host, files, ask):
            found.pop(key, None)
    return found


def _keys_openssh_leaves_unset(
    host: str, files: Sequence[str], keys: Sequence[str]
) -> List[str]:
    """Which of *keys* no line of *files* sets for *host*, as OpenSSH decides.

    The wrapper is read with ``-F``, so OpenSSH resolves a relative
    ``Include`` in it under ``~/.ssh`` -- as it does for the user's own
    configuration. On any failure nothing is reported unset, which keeps the
    walk's answer.
    """
    lines = [f'Include "{path}"' for path in files if os.path.isfile(path)]
    if not lines:
        return []
    lines.append("Match all")
    lines += [f"    {key} {_UNSET_PROBE_SENTINELS[key]}" for key in keys]
    try:
        with tempfile.TemporaryDirectory(prefix="sshpilot-probe-") as folder:
            wrapper = os.path.join(folder, "config")
            with open(wrapper, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            resolved = get_effective_ssh_config(host, config_file=wrapper)
    except Exception:
        logger.debug("Could not ask OpenSSH which options are unset", exc_info=True)
        return []
    if not resolved:
        return []
    unset = []
    for key in keys:
        value = resolved.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if str(value or "").strip() == _UNSET_PROBE_SENTINELS[key]:
            unset.append(key)
    return unset


def openssh_system_config() -> Optional[str]:
    """The system-wide file ``ssh`` reads after the user's, if it exists.

    ``-F`` makes OpenSSH skip it, so a launch that has to pass ``-F`` (a
    rewritten copy of the user's configuration) appends it by hand. The file
    sits in the ``etc/ssh`` of the prefix the ``ssh`` binary was built for:
    ``/etc/ssh`` for a system ``/usr/bin/ssh``, ``/opt/homebrew/etc/ssh``
    for Homebrew, ``/app/etc/ssh`` inside the Flatpak, and
    ``%PROGRAMDATA%\\ssh`` on Windows.
    """
    import shutil

    candidates: List[str] = []
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            candidates.append(os.path.join(program_data, "ssh", "ssh_config"))
    else:
        binary = shutil.which("ssh")
        if binary:
            prefix = os.path.dirname(os.path.dirname(os.path.realpath(binary)))
            if prefix not in ("/", "/usr"):
                candidates.append(os.path.join(prefix, "etc", "ssh", "ssh_config"))
        candidates.append("/etc/ssh/ssh_config")
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None

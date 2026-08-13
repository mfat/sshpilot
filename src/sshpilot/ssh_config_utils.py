import os
import glob
import shlex
import getpass
import socket
import logging
import re
import stat
import shutil
import tempfile
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Union


logger = logging.getLogger(__name__)

# ssh -G keys (lowercased) whose values are filesystem paths and so may carry a
# leading ``~`` that should be normalised before comparison.
_PATH_DIRECTIVES = frozenset({
    'identityfile', 'certificatefile', 'identityagent', 'pkcs11provider',
    'securitykeyprovider', 'controlpath', 'userknownhostsfile',
    'globalknownhostsfile', 'xauthlocation', 'revokedhostkeys', 'include',
})


def atomic_write_text(path: str, text: str, *, mode: Optional[int] = None,
                      backup: bool = False) -> None:
    """Atomically write *text* to *path* (temp file in the same dir, fsync,
    then ``os.replace`` — readers only ever see the old or the complete new
    file, never a truncated one).

    - ``backup=True`` copies the prior file to ``<path>.bak`` first.
    - ``mode`` sets the final permission bits (e.g. ``0o600`` for ~/.ssh/config).
      When ``mode`` is None and the file already exists, its current permission
      bits are preserved (a fresh temp file would otherwise be 0600 from
      mkstemp); when None and the file is new, the OS umask applies.

    Shared by the SSH config editors so manual edits get the same crash-safety
    as the structured connection writer.
    """
    directory = os.path.dirname(path) or '.'
    exists = os.path.exists(path)

    final_mode = mode
    if final_mode is None and exists:
        try:
            final_mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            final_mode = None

    if backup and exists:
        try:
            shutil.copy2(path, f"{path}.bak")
            if final_mode is not None:
                os.chmod(f"{path}.bak", final_mode)
        except Exception as exc:  # noqa: BLE001 — backup is best-effort
            logger.warning("Could not back up %s before writing: %s", path, exc)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.sshpilot-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if final_mode is not None:
        try:
            os.chmod(path, final_mode)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to set permissions on %s: %s", path, exc)


def validate_ssh_config_text(text: str) -> Optional[str]:
    """Dry-run validate SSH config *text* with ``ssh -G`` against a throwaway
    host. Returns None if it parses, else a short error string (the parser's
    own message). Returns None if ssh is unavailable / times out (don't block a
    save just because we couldn't check).
    """
    fd, tmp_path = tempfile.mkstemp(prefix='.sshpilot-validate-', suffix='.conf')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        result = subprocess.run(
            ['ssh', '-G', '-F', tmp_path, 'sshpilot-config-check'],
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None  # no ssh binary — can't validate, don't block
    except subprocess.TimeoutExpired:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ssh -G validation could not run: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if result.returncode == 0:
        return None
    err = (result.stderr or '').strip()
    # ssh prints lines like "/path: line N: Bad configuration option: foo";
    # surface the most relevant line.
    for line in err.splitlines():
        if 'line ' in line or 'Bad ' in line or 'error' in line.lower():
            return line.strip()
    return err.splitlines()[-1].strip() if err else f"ssh -G exited {result.returncode}"


_TOKEN_RE = re.compile(r'%(.)')


def expand_ssh_tokens(value: str) -> str:
    """Expand the ssh_config(5) percent tokens that are resolvable without a
    connection context (used for Include paths, which only meaningfully support
    %d and %u, plus the other host-independent tokens).

    %% -> literal %, %d -> local home dir, %u -> local username,
    %i -> local uid, %l/%L -> local hostname. Unknown tokens are left intact.
    """
    if not value or '%' not in value:
        return value

    home = os.path.expanduser('~')
    try:
        user = getpass.getuser()
    except Exception:
        user = ''
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ''
    mapping = {
        '%': '%',
        'd': home,
        'u': user,
        'i': str(os.getuid()) if hasattr(os, 'getuid') else '',
        'l': hostname,
        'L': hostname.split('.')[0],
    }

    def _repl(match: 're.Match') -> str:
        token = match.group(1)
        return mapping.get(token, match.group(0))

    return _TOKEN_RE.sub(_repl, value)


@dataclass(frozen=True)
class SSHConfigPathDiscovery:
    """Resolved SSH inputs and filesystem paths needed to rediscover them."""

    files: tuple[str, ...]
    watch_paths: FrozenSet[str]
    unreadable_paths: FrozenSet[str]


def _watch_parent_for_pattern(pattern: str) -> str:
    """Return the nearest non-glob parent used to discover future matches."""

    candidate = pattern
    while glob.has_magic(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    if not candidate or glob.has_magic(candidate):
        candidate = os.path.dirname(pattern) or '.'
    return os.path.abspath(candidate)


def discover_ssh_config_paths(
    main_path: str,
    *,
    max_depth: int = 32,
) -> SSHConfigPathDiscovery:
    """Resolve SSH inputs and the minimal watch set for their Include graph.

    Paths are expanded and resolved relative to their parent file. Duplicate files
    are ignored. Missing exact includes and non-glob parents are retained in the
    watch set so later creation and wildcard matches are detected.
    """
    resolved: List[str] = []
    visited: Set[str] = set()
    watch_paths: Set[str] = set()
    unreadable_paths: Set[str] = set()

    def _resolve(path: str, depth: int, stack: List[str]):
        abs_path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        watch_paths.add(abs_path)
        if abs_path in stack:
            logger.warning("Include cycle detected: %s -> %s", " -> ".join(stack), abs_path)
            return
        if depth > max_depth:
            logger.warning("Maximum include depth (%d) exceeded at %s", max_depth, abs_path)
            return
        if abs_path in visited:
            return
        try:
            with open(abs_path) as f:
                lines = f.readlines()
        except (OSError, UnicodeError) as exc:
            logger.warning("Cannot read include file %s: %s", abs_path, exc)
            if os.path.exists(abs_path):
                unreadable_paths.add(abs_path)
            return
        visited.add(abs_path)
        resolved.append(abs_path)
        base_dir = os.path.dirname(abs_path)
        stack.append(abs_path)
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            lowered = line.lower()
            if lowered.startswith('include '):
                patterns = shlex.split(line[len('include '):])
                for pattern in patterns:
                    expanded = os.path.expanduser(os.path.expandvars(expand_ssh_tokens(pattern)))
                    if not os.path.isabs(expanded):
                        expanded = os.path.join(base_dir, expanded)
                    expanded = os.path.abspath(expanded)
                    if glob.has_magic(expanded):
                        watch_paths.add(_watch_parent_for_pattern(expanded))
                    else:
                        watch_paths.add(expanded)
                        watch_paths.add(os.path.dirname(expanded) or '.')
                    matches = glob.glob(expanded)
                    if not matches:
                        logger.warning("Include pattern %s does not match any files", pattern)
                    for matched in sorted(matches):
                        if os.path.isdir(matched):
                            watch_paths.add(os.path.abspath(matched))
                            dir_matches = sorted(glob.glob(os.path.join(matched, '*')))
                            if not dir_matches:
                                logger.warning("Include directory %s is empty", matched)
                            for fname in dir_matches:
                                _resolve(fname, depth + 1, stack)
                        else:
                            _resolve(matched, depth + 1, stack)
        stack.pop()

    _resolve(main_path, 1, [])
    return SSHConfigPathDiscovery(
        files=tuple(resolved),
        watch_paths=frozenset(watch_paths),
        unreadable_paths=frozenset(unreadable_paths),
    )


def resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    """Return SSH config files, including recursively resolved Include files."""

    return list(discover_ssh_config_paths(main_path, max_depth=max_depth).files)


def collect_host_block_lines(host_identifier: str, ssh_config_path: Optional[str] = None) -> List[str]:
    """Combined lines of every *concrete* Host stanza matching *host_identifier*."""
    host_identifier = (host_identifier or '').strip()
    if not host_identifier:
        return []
    
    if not ssh_config_path:
        ssh_config_path = os.path.expanduser('~/.ssh/config')

    try:
        files = resolve_ssh_config_files(ssh_config_path)
    except Exception:
        files = [ssh_config_path]

    from .ssh_config_document import SSHConfigDocument

    combined: List[str] = []
    for path in files:
        try:
            if not path or not os.path.exists(path):
                continue
            doc = SSHConfigDocument.parse_file(path)
        except (OSError, UnicodeDecodeError):
            continue
        for block in doc.host_blocks(host_identifier):
            combined.extend(line.rstrip('\r\n') for line in block.lines)
    return combined


def get_effective_ssh_config(
    host: str, config_file: Optional[str] = None

) -> Dict[str, Union[str, List[str]]]:
    """Return effective SSH options for *host* using ``ssh -G``.

    The output is parsed into a dictionary with lowercased keys. Options that
    appear multiple times (e.g. ``IdentityFile``) are stored as lists.
    """
    cmd = ['ssh']
    if config_file:
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(config_file)))
        if os.path.isfile(expanded):
            cmd.extend(['-F', expanded])
        else:
            # An explicit override that no longer exists must NOT silently fall
            # back to ~/.ssh/config — that resolves the wrong configuration.
            logger.warning("Requested SSH config override %s does not exist", expanded)
            return {}
    cmd.extend(['-G', host])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=True, timeout=10)

    except Exception:
        # ssh missing, non-zero exit, or a hang (timeout) — callers treat an
        # empty dict as "no effective options".
        return {}

    config: Dict[str, Union[str, List[str]]] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ' ' in line:
            key, value = line.split(None, 1)
        else:
            key, value = line, ''
        key = key.lower()
        value = value.strip()
        if key in config:
            existing = config[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                config[key] = [existing, value]
        else:
            config[key] = value
    return config


def _effective_config_lines(cfg: Dict[str, Union[str, List[str]]]) -> List[str]:
    """Flatten an effective-config dict to sorted ``key value`` lines.

    Multi-value keys (e.g. ``identityfile``) become one line per value, in the
    order ssh reported them, so accumulation from other blocks is visible.
    """
    lines: List[str] = []
    for key in sorted(cfg):
        value = cfg[key]
        if isinstance(value, list):
            lines.extend(f"{key} {v}" for v in value)
        else:
            lines.append(f"{key} {value}")
    return lines


def _authored_directives(block_text: str) -> Set[str]:
    """Directive names explicitly written in a Host block (lowercased).

    ssh_config accepts both ``Keyword value`` and ``Keyword=value`` forms;
    ``Host``/``Match`` stanza headers and comments are not directives.
    """
    names: Set[str] = set()
    for raw_line in block_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        token = re.split(r'[\s=]', line, maxsplit=1)[0].lower()
        if token in ('host', 'match'):
            continue
        names.add(token)
    return names


def diff_effective_config(
    host: str,
    config_file: Optional[str],
    own_block_text: str,
) -> Optional[Dict[str, object]]:
    """Compare what a host's OWN block resolves to vs. the full effective config.

    Both sides go through ``ssh -G`` with an explicit ``-F``, so ssh's compiled-in
    defaults appear on both sides and cancel out — the remaining delta is exactly
    what global/wildcard blocks (e.g. ``Host *``) and includes add or override
    for *host*. The system-wide ``/etc/ssh/ssh_config`` is excluded from BOTH
    sides on purpose: an explicit ``-F`` makes ssh ignore it, and resolving the
    own block with ``-F`` while the full side used plain ``ssh -G`` (which reads
    it) leaked distro defaults such as ``GSSAPIAuthentication``,
    ``HashKnownHosts`` and ``SendEnv`` into the diff as phantom "added" entries.

    - *config_file*: the real config ssh will use for this connection (the app's
      isolated ``ssh_config`` or ``None`` for the default ``~/.ssh/config``).
    - *own_block_text*: the connection's own generated ``Host`` block.

    Returns ``None`` when the comparison can't run (no ssh / timeout — never
    block on a best-effort check), otherwise a dict with ``has_diff`` (bool),
    ``changes`` (classified rows), and ``own`` / ``full`` line lists for
    display. The displayed ``full`` side is always what a REAL ssh invocation
    resolves — in default mode (config_file=None) the app launches ssh without
    ``-F``, so plain ``ssh -G`` (including the system-wide config) is the
    truthful display even though change detection excludes it.
    """
    if not host:
        return None

    fd, tmp_path = tempfile.mkstemp(prefix='.sshpilot-own-', suffix='.conf')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(own_block_text)
        own = get_effective_ssh_config(host, config_file=tmp_path)

        if config_file is None:
            # Default mode. Change detection compares against the per-user
            # config via -F, so /etc/ssh/ssh_config stays out of BOTH sides
            # (see docstring). When no user config exists nothing can override
            # the block — resolve it against itself (a clean "no diff").
            user_config = os.path.expanduser('~/.ssh/config')
            compare_file = user_config if os.path.isfile(user_config) else tmp_path
            full = get_effective_ssh_config(host, config_file=compare_file)
            # The display side is resolved the way the app actually connects:
            # plain ssh, no -F, system-wide config included.
            display = get_effective_ssh_config(host)
        else:
            full = get_effective_ssh_config(host, config_file=config_file)
            display = full
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not full:
        return None  # couldn't resolve the real effective config — say nothing
    if not own:
        return None
    if not display:
        display = full

    # Normalise leading ``~`` ONLY for path-valued directives, so a stored-expanded
    # path (identity_files keeps the absolute form) doesn't read as a difference
    # from a config that wrote ``~``. Expanding every value would wrongly collapse
    # non-path values such as ``RemoteCommand ~/script``.
    def _expand(cfg):
        out = {}
        for key, value in cfg.items():
            if key not in _PATH_DIRECTIVES:
                out[key] = value
                continue
            if isinstance(value, list):
                out[key] = [os.path.expanduser(v) if isinstance(v, str) else v for v in value]
            elif isinstance(value, str):
                out[key] = os.path.expanduser(value)
            else:
                out[key] = value
        return out

    full = _expand(full)
    own = _expand(own)
    display = _expand(display)

    def _as_list(value) -> List[str]:
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    authored = _authored_directives(own_block_text)
    changes: List[Dict[str, object]] = []
    for key in sorted(set(full) | set(own)):
        own_vals = _as_list(own.get(key))
        full_vals = _as_list(full.get(key))
        if own_vals == full_vals:
            continue
        if key not in authored:
            # The block never set this key, so the own side merely shows ssh's
            # compiled default; a difference here is a global adding (or
            # changing) a setting the connection never authored. Report it as
            # a pure addition rather than a misleading "overridden: default →
            # value".
            changes.append({
                'key': key,
                'own': [],
                'effective': full_vals,
                'added': full_vals,
                'removed': [],
                'kind': 'added',
            })
            continue
        # Multiset difference: preserves duplicate counts, and a pure reorder
        # (same values, different order) yields empty added/removed but is still
        # a real change, classified as 'overridden'.
        own_counts = Counter(own_vals)
        full_counts = Counter(full_vals)
        added = list((full_counts - own_counts).elements())
        removed = list((own_counts - full_counts).elements())
        if added and removed:
            kind = 'overridden'   # a value was replaced
        elif added:
            kind = 'added'        # global adds a new value (or accumulates)
        elif removed:
            kind = 'removed'      # global drops a value the block set
        else:
            kind = 'overridden'   # same values, different effective order
        changes.append({
            'key': key,
            'own': own_vals,
            'effective': full_vals,
            'added': added,
            'removed': removed,
            'kind': kind,
        })

    return {
        'has_diff': bool(changes),
        'changes': changes,
        'own': _effective_config_lines(own),
        'full': _effective_config_lines(display),
    }

"""
Backup and Restore Manager for sshPilot
Handles import/export of SSH and application configuration
"""

import base64
import json
import logging
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from .platform_utils import get_config_dir, get_ssh_dir, is_flatpak
from .config import Config, CONFIG_VERSION

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1
BACKUP_OPTION_KEYS = (
    'app_settings',
    'ssh_config',
    'known_hosts',
    'private_keys',
    'secrets',
)
DEFAULT_BACKUP_OPTIONS = {
    'app_settings': True,
    'ssh_config': True,
    'known_hosts': True,
    'secrets': True,
    'private_keys': False,
}
DEFAULT_RESTORE_OPTIONS = dict(DEFAULT_BACKUP_OPTIONS)


def _current_home() -> str:
    return os.path.abspath(os.path.expanduser('~'))


def _rebase_home_path(path: str, source_home: Optional[str], target_home: str) -> str:
    """Map an absolute path that lived under the backup's *source* home onto the *current*
    home, so a key backed up as ``/home/alice/.ssh/id`` restores to ``/home/bob/.ssh/id``.

    Paths outside the source home (e.g. ``/etc/ssh/…``), or backups that predate the recorded
    ``source_home``, are returned unchanged — no guessing."""
    if not path or not source_home:
        return path
    ap = os.path.abspath(os.path.expanduser(str(path)))
    sh = os.path.abspath(str(source_home))
    if ap == sh:
        return target_home
    prefix = sh + os.sep
    if ap.startswith(prefix):
        return os.path.join(target_home, ap[len(prefix):])
    return path


# SSH directives whose argument is a path on the LOCAL filesystem — the only lines where a
# home-prefix rewrite is meaningful. Anything else (RemoteCommand/RemoteForward target the
# REMOTE host; comments and hostnames are free text) must be left byte-for-byte alone.
_LOCAL_PATH_DIRECTIVES = frozenset({
    'identityfile', 'certificatefile', 'identityagent', 'controlpath',
    'userknownhostsfile', 'globalknownhostsfile', 'include',
    'pkcs11provider', 'securitykeyprovider', 'xauthlocation', 'revokedhostkeys',
})


def _rebase_home_in_text(text: str, source_home: Optional[str], target_home: str) -> str:
    """Rewrite home-prefixed absolute paths onto the current home, but ONLY on lines whose
    directive names a local-filesystem path (``IdentityFile``, ``ControlPath``, ``Include`` …).

    A blind text replace would also corrupt ``RemoteCommand``/``RemoteForward`` arguments (which
    live on the remote host) and comments that merely mention the old home — so we scope the
    rewrite per directive."""
    if not text or not source_home:
        return text
    sh = os.path.abspath(str(source_home))
    th = os.path.abspath(str(target_home))
    if sh == th:
        return text
    needle = sh + os.sep
    repl = th + os.sep
    out = []
    for line in text.splitlines(keepends=True):
        if needle in line and _ssh_keyword(line) in _LOCAL_PATH_DIRECTIVES:
            out.append(line.replace(needle, repl))
        else:
            out.append(line)
    return ''.join(out)


# --- SSH config as an Include-aware file tree --------------------------------
#
# The app READS ~/.ssh/config as a tree (Include-resolved) and WRITES it surgically per host,
# so backups must treat it as a tree too — not one opaque blob. These helpers gather/split it
# using the same header rules ssh uses, and they never invent a second semantic parser: the
# splitter only needs to find stanza boundaries (Host/Match headers), exactly the boundary the
# loader's own edit/delete scanners use.

# A single sshPilot-owned file that merge-imported hosts are written to, referenced by one
# Include. Deliberately not inside a common ``*.d`` glob dir, so we can add an explicit Include
# without risking double-inclusion.
_IMPORT_FRAGMENT_NAME = "sshpilot-imported.conf"
# Marker comment written immediately above our managed Include. Must stay at the *top* of the
# main config (before any Host/Match): OpenSSH treats directives after a Host/Match as part of
# that block, so an Include appended at EOF nests inside e.g. ``Host *`` / ``User root`` and
# first-match-wins then overrides per-host ``User`` from the fragment (Oracle → root).
_IMPORT_INCLUDE_MARKER = "# Added by sshPilot import"


def _ssh_config_root(main_path: str) -> str:
    """Directory that owns the SSH config (``~/.ssh`` default, the app config dir isolated)."""
    return os.path.dirname(os.path.abspath(os.path.expanduser(main_path))) or os.path.expanduser('~')


def _is_explicit_import_include_line(line: str, fragment_abs: str, config_root: str) -> bool:
    """True when *line* is an ``Include`` that names our import fragment (not a glob)."""
    s = line.strip()
    if not s or s.startswith('#'):
        return False
    lowered = s.lower()
    if not lowered.startswith('include'):
        return False
    # ``Include`` + separator (space or =)
    rest = s[7:]
    if rest.startswith('='):
        rest = rest[1:]
    elif rest[:1] and rest[:1].isspace():
        rest = rest.lstrip()
    else:
        return False
    try:
        patterns = shlex.split(rest)
    except ValueError:
        return False
    if len(patterns) != 1:
        return False
    pattern = patterns[0]
    if any(ch in pattern for ch in '*?['):
        return False
    from .ssh_config_utils import expand_ssh_tokens
    expanded = os.path.expanduser(os.path.expandvars(expand_ssh_tokens(pattern)))
    if not os.path.isabs(expanded):
        expanded = os.path.join(config_root, expanded)
    return os.path.abspath(expanded) == os.path.abspath(fragment_abs)


def _strip_managed_import_includes(text: str, fragment_abs: str, config_root: str) -> str:
    """Remove sshPilot-managed ``Include`` lines (and their marker comments) for *fragment_abs*."""
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == _IMPORT_INCLUDE_MARKER:
            i += 1
            if i < n and _is_explicit_import_include_line(lines[i], fragment_abs, config_root):
                i += 1
            # Drop one blank line that commonly followed our appended block.
            if i < n and lines[i].strip() == '':
                i += 1
            continue
        if _is_explicit_import_include_line(lines[i], fragment_abs, config_root):
            i += 1
            if i < n and lines[i].strip() == '':
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return ''.join(out)


def _import_include_follows_host_or_match(text: str, fragment_abs: str, config_root: str) -> bool:
    """True if our managed Include appears after a Host/Match (i.e. nested inside a block)."""
    seen_block = False
    for raw in text.splitlines():
        kw = _ssh_keyword(raw)
        if kw in ('host', 'match'):
            seen_block = True
        if not seen_block:
            continue
        if raw.strip() == _IMPORT_INCLUDE_MARKER:
            return True
        if _is_explicit_import_include_line(raw, fragment_abs, config_root):
            return True
    return False


def _import_include_arg(frag_abs: str, config_root: str) -> str:
    """Return the Include path OpenSSH will resolve with ``-F`` for this config.

    OpenSSH resolves *relative* user-config Includes under ``~/.ssh``, not under the
    directory of the ``-F`` file. Relative names are therefore only safe when the
    main config itself lives in ``~/.ssh``; isolated mode (and tests) need an
    absolute path so ``ssh -F <isolated>`` still finds the fragment.
    """
    ssh_dir = os.path.abspath(os.path.expanduser(os.path.join("~", ".ssh")))
    if os.path.abspath(config_root) == ssh_dir:
        return os.path.relpath(frag_abs, config_root)
    return os.path.abspath(frag_abs)


def ensure_import_include_at_top(main_path: str, fragment_path: Optional[str] = None) -> bool:
    """Ensure the merge-import ``Include`` is a top-level directive at the start of *main_path*.

    OpenSSH nests any directive that appears after a ``Host``/``Match`` inside that block.
    Appending ``Include sshpilot-imported.conf`` at EOF therefore lands inside a trailing
    ``Host *`` (common for defaults / IdentityAgent), so ``User root`` from ``Host *``
    wins over per-host ``User`` values in the fragment.

    Returns True when the main config file was rewritten.
    """
    main_path = os.path.abspath(os.path.expanduser(main_path))
    root = _ssh_config_root(main_path)
    frag_abs = os.path.abspath(
        fragment_path or os.path.join(root, _IMPORT_FRAGMENT_NAME))
    if not os.path.isfile(frag_abs):
        return False

    existing = ''
    if os.path.exists(main_path):
        try:
            with open(main_path, encoding='utf-8') as f:
                existing = f.read()
        except OSError:
            return False

    stripped = _strip_managed_import_includes(existing, frag_abs, root)

    # Is the fragment still pulled in without our explicit Include (e.g. ``Include *.conf``)?
    still_covered = False
    try:
        import tempfile
        from .ssh_config_utils import resolve_ssh_config_files
        fd, tmp = tempfile.mkstemp(prefix='sshpilot-inc-', suffix='.conf', dir=root)
        try:
            os.close(fd)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(stripped)
            # Point includes relative to the real config root: rewrite is same dir as main.
            # resolve uses the temp file's directory for relative Includes — same root.
            resolved = {os.path.abspath(p) for p in resolve_ssh_config_files(tmp)}
            still_covered = frag_abs in resolved
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        still_covered = False

    if still_covered:
        new_text = stripped
    else:
        include_arg = _import_include_arg(frag_abs, root)
        block = f"{_IMPORT_INCLUDE_MARKER}\nInclude {include_arg}\n\n"
        body = stripped[1:] if stripped.startswith('\n') else stripped
        new_text = block + body

    if new_text == existing:
        return False

    # Atomic replace (same pattern as BackupManager._atomic_write_text).
    tmp_path = main_path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, main_path)
        try:
            os.chmod(main_path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Placed sshPilot import Include at top of %s", main_path)
    return True


def repair_misplaced_import_include(main_path: str) -> bool:
    """Rewrite *main_path* when a prior merge left ``Include sshpilot-imported.conf`` nested
    under a Host/Match block. No-op when the fragment is absent or already top-level."""
    main_path = os.path.abspath(os.path.expanduser(main_path))
    root = _ssh_config_root(main_path)
    frag_abs = os.path.join(root, _IMPORT_FRAGMENT_NAME)
    if not os.path.isfile(frag_abs) or not os.path.isfile(main_path):
        return False
    try:
        with open(main_path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return False
    if not _import_include_follows_host_or_match(text, frag_abs, root):
        # Still ensure a top-level Include exists when the fragment is orphaned.
        from .ssh_config_utils import resolve_ssh_config_files
        try:
            resolved = {os.path.abspath(p) for p in resolve_ssh_config_files(main_path)}
        except Exception:
            resolved = set()
        if frag_abs in resolved:
            return False
    return ensure_import_include_at_top(main_path, frag_abs)


def _is_under(path: str, root: str) -> bool:
    ap = os.path.abspath(path)
    rp = os.path.abspath(root)
    return ap == rp or ap.startswith(rp + os.sep)


def _ssh_keyword(line: str) -> str:
    """The lowercase SSH directive keyword of a line, honoring ``Key value``, ``Key=value`` and
    ``Key = value``. Blank/comment lines return ``''``."""
    s = line.strip()
    if not s or s.startswith('#'):
        return ''
    for i, ch in enumerate(s):
        if ch.isspace() or ch == '=':
            return s[:i].lower()
    return s.lower()


def _host_patterns(line: str) -> List[str]:
    """Patterns from a ``Host a b c`` header line."""
    s = line.strip()
    rest = s[4:].lstrip(' =\t') if len(s) >= 4 else ''
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def _iter_config_stanzas(text: str):
    """Yield ``(kind, patterns, block_lines)`` for each top-level stanza. ``kind`` is
    ``'host'``/``'match'``/``'global'`` (the preamble before the first Host/Match). A stanza runs
    from its header to the next Host/Match header — boundary detection only, never option
    interpretation, so unusual-but-valid layouts (unindented options, tabs, ``Host=x``) are kept
    whole instead of being truncated."""
    cur_kind = 'global'
    cur_patterns: Optional[List[str]] = None
    buf: List[str] = []
    for line in text.splitlines():
        kw = _ssh_keyword(line)
        if kw in ('host', 'match'):
            if buf:
                yield (cur_kind, cur_patterns, buf)
            buf = [line]
            cur_kind = kw
            cur_patterns = _host_patterns(line) if kw == 'host' else None
        else:
            buf.append(line)
    if buf:
        yield (cur_kind, cur_patterns, buf)


def _is_wildcard_pattern(pattern: str) -> bool:
    """A Host pattern that isn't a single concrete name (``*``/``?`` wildcards, ``!`` negation)."""
    return any(ch in pattern for ch in '*?!')


def _owned_and_readable(path: str) -> bool:
    """True if we own ``path`` and can read it. Ownership (not writability) is the right test for
    "a file the user controls" — a defensively read-only (chmod 400) config is still theirs and
    must be backed up, while a root-owned system file must not be."""
    try:
        if not os.access(path, os.R_OK):
            return False
        getuid = getattr(os, 'getuid', None)
        if getuid is None:            # non-POSIX: no ownership concept, fall back to readability
            return True
        return os.stat(path).st_uid == getuid()
    except OSError:
        return False


def _gather_ssh_config_tree(main_path: str) -> Tuple[Dict[str, str], List[str]]:
    """Return ``({relpath_from_root: content}, [skipped_abspaths])`` for every file reachable from
    ``main_path`` via Include that is **under the config root and owned by the user**.

    Files outside the root or not owned by us (``/etc/ssh/…``, team-shared, root-owned) are
    reported as skipped and never bundled — a backup must not ship content the user doesn't own.
    Owned-but-read-only files ARE bundled (readability, not writability, is the test)."""
    from .ssh_config_utils import resolve_ssh_config_files
    root = _ssh_config_root(main_path)
    tree: Dict[str, str] = {}
    skipped: List[str] = []
    try:
        files = resolve_ssh_config_files(main_path)
    except Exception:
        files = [main_path] if os.path.exists(main_path) else []
    for f in files:
        af = os.path.abspath(f)
        if not _is_under(af, root) or not _owned_and_readable(af):
            if af not in skipped:
                skipped.append(af)
            continue
        try:
            with open(af, encoding='utf-8') as fh:
                tree[os.path.relpath(af, root)] = fh.read()
        except OSError:
            if af not in skipped:
                skipped.append(af)
    return tree, skipped


def _existing_host_names(main_path: str) -> set:
    """All Host patterns already present across the live config tree (Include-aware), so merge
    dedup sees hosts that live in fragments, not just the main file."""
    from .ssh_config_utils import resolve_ssh_config_files
    names: set = set()
    try:
        files = resolve_ssh_config_files(main_path)
    except Exception:
        files = [main_path]
    for f in files:
        try:
            with open(f, encoding='utf-8') as fh:
                txt = fh.read()
        except OSError:
            continue
        for kind, patterns, _blk in _iter_config_stanzas(txt):
            if kind == 'host' and patterns:
                names.update(patterns)
    return names


def _select_new_host_blocks(imported_texts: List[str], existing_names: set
                            ) -> Tuple[str, int, List[List[str]]]:
    """From imported config text(s), pick the concrete Host stanzas that are genuinely new.

    Returns ``(new_block_text, dropped_nonhost, partial_collisions)``:
    - wildcard/negated ``Host`` stanzas, ``Match`` blocks and top-level globals are dropped
      (counted) — merge must never inject machine-wide SSH behavior from a backup;
    - a stanza whose names are all already present is skipped silently;
    - a stanza where only SOME names collide is skipped and reported (an inherent ambiguity we
      surface rather than guess)."""
    kept: List[str] = []
    dropped = 0
    collisions: List[List[str]] = []
    for txt in imported_texts:
        if not txt:
            continue
        for kind, patterns, blk in _iter_config_stanzas(txt):
            has_content = any(l.strip() and not l.strip().startswith('#') for l in blk)
            if kind != 'host' or not patterns:
                if has_content:
                    dropped += 1
                continue
            # Keep a stanza as long as it names at least one concrete host; only PURE
            # wildcard/negation stanzas (Host *, Host prod-*, Host * !x) are global-ish and
            # dropped. `Host prod !prod-db` keeps `prod`.
            concrete = [p for p in patterns if not _is_wildcard_pattern(p)]
            if not concrete:
                dropped += 1
                continue
            present = [p for p in concrete if p in existing_names]
            new_names = [p for p in concrete if p not in existing_names]
            if not present:
                kept.append('\n'.join(blk).rstrip())
            elif new_names:
                # Partial collision: import only the non-colliding names (rebuild the Host
                # header, keep the body) instead of dropping the whole stanza — and still report
                # the collision so the user knows some names were left to the existing hosts.
                indent = blk[0][:len(blk[0]) - len(blk[0].lstrip())]
                split_block = [f"{indent}Host {' '.join(new_names)}"] + list(blk[1:])
                kept.append('\n'.join(split_block).rstrip())
                collisions.append(patterns)
            # else: every concrete name already exists -> skip silently (a full duplicate,
            # e.g. re-importing your own backup, is not a reportable collision)
    return ('\n\n'.join(kept), dropped, collisions)


def _rewrite_include_line(line: str, source_root: Optional[str]) -> str:
    """Make one ``Include`` line portable for restore onto another machine/mode.

    - relative includes are kept (they resolve under the target config root, where the bundled
      fragments now live);
    - absolute includes UNDER the backup's source root become relative (fixes cross-mode restore,
      where the target root is not the home dir);
    - absolute includes OUTSIDE the source root were never bundled, so the line is commented out
      with a note instead of silently re-pointing at the target machine's system files."""
    if _ssh_keyword(line) != 'include':
        return line
    rest = line.strip()[len('include'):].lstrip(' =\t')
    try:
        patterns = shlex.split(rest)
    except ValueError:
        patterns = rest.split()
    if not patterns:
        return line
    new_patterns: List[str] = []
    foreign = False
    for p in patterns:
        pe = os.path.expanduser(os.path.expandvars(p))
        if not os.path.isabs(pe):
            new_patterns.append(p)
        elif source_root and _is_under(pe, source_root):
            new_patterns.append(os.path.relpath(pe, source_root))
        else:
            foreign = True
    indent = line[:len(line) - len(line.lstrip())]
    tail = '\n' if line.endswith('\n') else ''
    if foreign:
        return f"{indent}# sshPilot: Include not included in backup: {line.strip()}{tail}"
    return f"{indent}Include {' '.join(new_patterns)}{tail}"


def _rewrite_includes(text: str, source_root: Optional[str]) -> str:
    """Apply :func:`_rewrite_include_line` to every line of an imported config file."""
    return ''.join(_rewrite_include_line(line, source_root)
                   for line in text.splitlines(keepends=True))


class BackupManager:
    """Manages configuration backup and restore operations"""

    def __init__(self, config: Config, connection_manager=None):
        self.config = config
        self.connection_manager = connection_manager
        self.backup_dir = Path(get_config_dir()) / 'backups'
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.last_export_counts = {'credentials': 0, 'private_keys': 0}
        self.last_import_skipped_keys = 0   # existing keys left untouched on the last import
        self.last_import_skipped_credentials = 0  # secrets already present, left untouched
        # False when the last restore targeted a backend that does not persist secrets
        # (the "agent"/don't-store backend), so the UI can say so instead of claiming success.
        self.last_import_secrets_persisted = True
        # SSH-config files skipped on the last export (outside the config root / not writable).
        self.last_export_skipped_config_files: List[str] = []
        # Referenced private-key files that didn't exist at export time (omitted from the backup).
        self.last_export_missing_key_files: List[str] = []
        # Credentials present in a legacy JSON import that this path does not restore (.spbk only).
        self.last_import_ignored_secrets = 0
        # Merge diagnostics: wildcard/Match/global stanzas dropped, and partial-name collisions.
        self.last_merge_dropped_globals = 0
        self.last_merge_collisions: List[List[str]] = []
        # {'mirrored': n, 'failed': n} after an export that also mirrored secrets, else None.
        self.last_mirror_counts: Optional[Dict[str, int]] = None

    def get_ssh_config_path(self) -> str:
        """Get the current SSH config path based on mode"""
        if self.connection_manager:
            return getattr(self.connection_manager, 'ssh_config_path', '')
        
        # Fallback: determine from config
        use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        if use_isolated:
            return str(Path(get_config_dir()) / 'ssh_config')
        else:
            return str(Path(get_ssh_dir()) / 'config')

    def get_known_hosts_path(self) -> Optional[str]:
        """known_hosts path — **isolated mode only** (sshPilot's own file).

        In default mode this returns ``None`` so the user's GLOBAL ``~/.ssh/known_hosts`` is
        never backed up, replaced, merged, or otherwise touched: it is shared TOFU state used
        by all of the user's SSH tooling, not something sshPilot owns."""
        if self.connection_manager:
            if getattr(self.connection_manager, 'isolated_mode', False):
                return getattr(self.connection_manager, 'known_hosts_path', None)
            return None

        use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        if use_isolated:
            return str(Path(get_config_dir()) / 'known_hosts')
        return None

    def _current_isolated_mode(self) -> bool:
        """Return the mode that restore targets should keep using after import."""
        if self.connection_manager:
            return bool(getattr(self.connection_manager, 'isolated_mode', False))
        return bool(self.config.get_setting('ssh.use_isolated_config', False))

    def _app_config_for_restore(self, app_config: Dict[str, Any]) -> Dict[str, Any]:
        """Copy imported app settings while preserving machine-specific local settings: the SSH
        operation mode and the secret-storage backend selection.

        The ``secrets`` subtree (backend choice + absolute vault paths like
        ``keepassxc.database`` / ``bitwarden.profile``) is specific to *this* machine. Importing
        the source machine's values would point the selected backend at a file that doesn't exist
        here, silently disabling all secret save/autofill — so we always keep the local values."""
        restored = dict(app_config)
        ssh_settings = restored.get('ssh')
        if isinstance(ssh_settings, dict):
            ssh_settings = dict(ssh_settings)
        else:
            ssh_settings = {}
        ssh_settings['use_isolated_config'] = self._current_isolated_mode()
        restored['ssh'] = ssh_settings

        # Keep the local secret-storage selection; never import the source machine's.
        local_secrets = None
        try:
            local_secrets = self.config.config_data.get('secrets')
        except Exception:
            local_secrets = None
        if isinstance(local_secrets, dict):
            restored['secrets'] = dict(local_secrets)
        else:
            restored.pop('secrets', None)   # revert to defaults rather than the source's paths
        return restored

    @staticmethod
    def normalize_backup_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
        """Return a complete, boolean option map for export/restore categories."""
        merged = dict(DEFAULT_BACKUP_OPTIONS)
        if options:
            for key in BACKUP_OPTION_KEYS:
                if key in options:
                    merged[key] = bool(options[key])
        return merged

    def _build_export_data(self, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The config payload (ssh_config + known_hosts + app_config + metadata) shared by the
        legacy JSON export and the ``.spbk`` backup."""
        options = self.normalize_backup_options(options)
        export_data = {
            'version': BACKUP_VERSION,
            'export_date': datetime.now().isoformat(),
            'platform': 'flatpak' if is_flatpak() else os.name,
            'config_version': CONFIG_VERSION,
            'backup_options': options,
            # Home dir at export time, so restore can rebase key/config paths onto this machine.
            'source_home': _current_home(),
        }

        # Export SSH configuration mode
        use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        export_data['isolated_mode'] = bool(use_isolated)

        # Export SSH config file
        ssh_config_path = self.get_ssh_config_path()
        if not options['ssh_config']:
            export_data['ssh_config'] = ''
        elif ssh_config_path and os.path.exists(ssh_config_path):
            try:
                with open(ssh_config_path, encoding='utf-8') as f:
                    export_data['ssh_config'] = f.read()
                logger.info(f"Exported SSH config from {ssh_config_path}")
            except Exception as e:
                logger.warning(f"Could not read SSH config: {e}")
                export_data['ssh_config'] = ''
        else:
            export_data['ssh_config'] = ''
            logger.warning(f"SSH config not found at {ssh_config_path}")

        # Capture the whole Include-resolved tree (main + user-owned fragments), so hosts that
        # live in Include'd files are backed up too — not silently dropped. Keyed relative to the
        # config root, so restore can rebase onto a different machine. ``ssh_config`` (main text)
        # stays for readers that predate this field.
        export_data['ssh_config_files'] = {}
        export_data['ssh_config_main_rel'] = ''
        export_data['ssh_config_root'] = ''
        self.last_export_skipped_config_files = []
        if options['ssh_config'] and ssh_config_path and os.path.exists(ssh_config_path):
            tree, skipped = _gather_ssh_config_tree(ssh_config_path)
            export_data['ssh_config_files'] = tree
            export_data['ssh_config_main_rel'] = os.path.relpath(
                os.path.abspath(ssh_config_path), _ssh_config_root(ssh_config_path))
            # Absolute source root, so restore can make absolute Include lines relative.
            export_data['ssh_config_root'] = _ssh_config_root(ssh_config_path)
            self.last_export_skipped_config_files = skipped
            if skipped:
                logger.warning("Excluded %d SSH config file(s) outside your control from the "
                               "backup: %s", len(skipped), ", ".join(skipped))

        # Export known_hosts — isolated mode only. get_known_hosts_path() returns None in
        # default mode, so the user's global ~/.ssh/known_hosts is never put into a backup.
        known_hosts_path = self.get_known_hosts_path()
        if not options['known_hosts']:
            export_data['known_hosts'] = None
        elif known_hosts_path and os.path.exists(known_hosts_path):
            try:
                with open(known_hosts_path, encoding='utf-8') as f:
                    export_data['known_hosts'] = f.read()
                logger.info(f"Exported known_hosts from {known_hosts_path}")
            except Exception as e:
                logger.warning(f"Could not read known_hosts: {e}")
                export_data['known_hosts'] = None
        else:
            export_data['known_hosts'] = None

        # Export app configuration
        config_file = Path(get_config_dir()) / 'config.json'
        if not options['app_settings']:
            export_data['app_config'] = {}
        elif config_file.exists():
            try:
                with open(config_file, encoding='utf-8') as f:
                    export_data['app_config'] = json.load(f)
                logger.info(f"Exported app config from {config_file}")
            except Exception as e:
                logger.warning(f"Could not read app config: {e}")
                export_data['app_config'] = self.config.get_default_config()
        else:
            export_data['app_config'] = self.config.get_default_config()
            logger.warning("App config not found, using defaults")

        return export_data

    def export_configuration(self, export_path: str) -> Tuple[bool, Optional[str]]:
        """Export all configuration to a plain JSON file (legacy format; no secrets)."""
        try:
            export_data = self._build_export_data()
            export_path = os.path.expanduser(export_path)
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2)
            logger.info(f"Configuration exported successfully to {export_path}")
            return True, None
        except Exception as e:
            error_msg = f"Failed to export configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _gather_credentials(self, connections) -> List[Dict[str, Any]]:
        """Serialized credentials (password/sudo/key passphrase) for the given connections —
        only their secrets, no enumerated orphans."""
        if not connections:
            return []
        try:
            from .credential_manager import CredentialManager
            creds = CredentialManager(list(connections)).list_credentials(include_orphans=False)
        except Exception:
            logger.warning("Gathering credentials for backup failed", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for c in creds:
            if c.secret is None:
                continue
            out.append({'id': c.id, 'type': c.type, 'host': c.host,
                        'username': c.username, 'secret': c.secret, 'metadata': c.metadata})
        return out

    def _connection_key_paths(self, connections) -> List[str]:
        paths: List[str] = []
        for conn in connections or []:
            for attr in ('keyfile', 'identity_files', 'resolved_identity_files'):
                value = getattr(conn, attr, None)
                if isinstance(value, (list, tuple)):
                    candidates = value
                else:
                    candidates = [value]
                for candidate in candidates:
                    if not candidate:
                        continue
                    path = os.path.expanduser(str(candidate))
                    if path and path not in paths:
                        paths.append(path)
        return paths

    def _gather_private_keys(self, connections) -> List[Dict[str, Any]]:
        """Serialize selected connections' private key files and matching .pub files."""
        out: List[Dict[str, Any]] = []
        missing: List[str] = []
        for key_path in self._connection_key_paths(connections):
            try:
                if not os.path.isfile(key_path):
                    # A referenced key file is gone — record it so export can warn rather than
                    # silently ship a backup with fewer keys than the user expects.
                    missing.append(key_path)
                    continue
                with open(key_path, 'rb') as f:
                    private_raw = f.read()
                stat = os.stat(key_path)
                item: Dict[str, Any] = {
                    'path': key_path,
                    'mode': stat.st_mode & 0o777,
                    'content_b64': base64.b64encode(private_raw).decode('ascii'),
                }
                public_path = f"{key_path}.pub"
                if os.path.isfile(public_path):
                    with open(public_path, 'rb') as f:
                        public_raw = f.read()
                    item['public_path'] = public_path
                    item['public_content_b64'] = base64.b64encode(public_raw).decode('ascii')
                    item['public_mode'] = os.stat(public_path).st_mode & 0o777
                out.append(item)
            except Exception:
                logger.warning("Failed to include private key in backup: %s", key_path,
                               exc_info=True)
        self.last_export_missing_key_files = missing
        if missing:
            logger.warning("Export: %d referenced key file(s) not found and omitted: %s",
                           len(missing), ", ".join(missing))
        return out

    def _build_manifest(self, connections, options: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
        """The full backup manifest (config + selected credentials + private keys) — the transport-
        independent payload shared by the ``.spbk`` file and the Bitwarden-note destinations. Also
        updates ``last_export_counts``."""
        options = self.normalize_backup_options(options)
        manifest = self._build_export_data(options)
        manifest['format'] = 'spbk'
        manifest['credentials'] = (
            self._gather_credentials(connections) if options['secrets'] else [])
        manifest['private_keys'] = (
            self._gather_private_keys(connections) if options['private_keys'] else [])
        self.last_export_counts = {
            'credentials': len(manifest['credentials']),
            'private_keys': len(manifest['private_keys']),
        }
        return manifest

    def export_backup(self, export_path: str, *, connections=None,
                      passphrase: Optional[str] = None,
                      options: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
        """Export a ``.spbk`` backup with user-selected config and secret categories."""
        try:
            from .backup_archive import write_spbk
            manifest = self._build_manifest(connections, options)
            write_spbk(os.path.expanduser(export_path), manifest, passphrase or None)
            logger.info("Backup exported to %s (%d credential(s), %d private key(s), encrypted=%s)",
                        export_path, len(manifest['credentials']),
                        len(manifest['private_keys']), bool(passphrase))
            return True, None
        except Exception as e:
            error_msg = f"Failed to export backup: {e}"
            logger.error(error_msg)
            return False, error_msg

    def export_to_backend(self, backend, *, connections=None,
                          passphrase: Optional[str] = None,
                          options: Optional[Dict[str, Any]] = None,
                          mirror_to=None):
        """Build the manifest and hand it to a ``BackupBackend`` (file or Bitwarden). Returns the
        backend's ``BackupEntry``; the backend raises on failure (e.g. ``BackupTooLargeForNote``),
        which the caller surfaces.

        When ``mirror_to`` (a secret ``SecretBackend``) is given and secrets are included, the
        manifest's credentials are ALSO copied into it as normal entries (login items), recorded
        in ``last_mirror_counts`` — the "mirror secrets" option for a Bitwarden export."""
        self.last_mirror_counts = None
        manifest = self._build_manifest(connections, options)
        entry = backend.export(manifest, passphrase=passphrase or None)
        logger.info("Backup exported via %s backend (%d credential(s), %d private key(s))",
                    getattr(backend, 'name', '?'),
                    len(manifest.get('credentials') or []),
                    len(manifest.get('private_keys') or []))
        if mirror_to is not None and self.normalize_backup_options(options)['secrets']:
            mirrored, failed = self.mirror_credentials_to_backend(manifest, mirror_to)
            self.last_mirror_counts = {'mirrored': mirrored, 'failed': failed}
        return entry

    def mirror_credentials_to_backend(self, manifest: Dict[str, Any], backend
                                      ) -> Tuple[int, int]:
        """Copy the manifest's credentials into ``backend`` as normal entries (updates existing) —
        the same ``Credential`` → ``credential_to_spec`` → ``store`` path as
        :meth:`_restore_credentials`, but targeting a specific backend. Returns
        ``(mirrored, failed)``."""
        from .credential_model import Credential, credential_to_spec
        mirrored = failed = 0
        for c in manifest.get('credentials') or []:
            secret = c.get('secret')
            if secret is None:
                continue
            try:
                cred = Credential(
                    id=c.get('id', ''), type=c.get('type', ''),
                    host=c.get('host'), username=c.get('username'),
                    secret=secret, metadata=dict(c.get('metadata') or {}))
                if backend.store(credential_to_spec(cred), secret):
                    mirrored += 1
                else:
                    failed += 1
            except Exception:
                logger.warning("Failed to mirror a credential to the target backend",
                               exc_info=True)
                failed += 1
        return mirrored, failed

    def import_from_backend(self, backend, entry, *, mode: str = 'replace',
                            create_backup: bool = True,
                            restore_options: Optional[Dict[str, Any]] = None,
                            passphrase: Optional[str] = None
                            ) -> Tuple[bool, Optional[str], int, int]:
        """Read a manifest from a ``BackupBackend`` and apply it via ``apply_imported_manifest``."""
        manifest = backend.read(entry, passphrase=passphrase or None)
        return self.apply_imported_manifest(
            manifest, mode=mode, create_backup=create_backup, restore_options=restore_options)

    def import_configuration(
        self, 
        import_path: str, 
        mode: str = 'replace',
        create_backup: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Import configuration from a JSON file
        
        Args:
            import_path: Path to the import file
            mode: 'replace' or 'merge'
            create_backup: Whether to create a backup before importing
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Validate import file
            import_path = os.path.expanduser(import_path)
            if not os.path.exists(import_path):
                return False, f"Import file not found: {import_path}"

            # Load import data
            try:
                with open(import_path, encoding='utf-8') as f:
                    import_data = json.load(f)
            except json.JSONDecodeError as e:
                return False, f"Invalid JSON file: {e}"

            # The legacy JSON path restores config only. Record any embedded secrets/keys it will
            # NOT restore, so the UI can tell the user to use an encrypted .spbk backup instead of
            # silently dropping them.
            if isinstance(import_data, dict):
                self.last_import_ignored_secrets = (
                    len(import_data.get('credentials') or [])
                    + len(import_data.get('private_keys') or []))
                if self.last_import_ignored_secrets:
                    logger.warning(
                        "Legacy JSON import contains %d secret/key entr(ies) that this format "
                        "does not restore; use an encrypted .spbk backup to include them",
                        self.last_import_ignored_secrets)

            return self._apply_parsed(import_data, mode, create_backup)

        except Exception as e:
            error_msg = f"Failed to import configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _restore_options_for_manifest(
        self,
        manifest: Dict[str, Any],
        restore_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, bool]:
        included = self.normalize_backup_options(manifest.get('backup_options'))
        # Legacy JSON/SPBK files predate option metadata; their config sections mean "included".
        if 'backup_options' not in manifest:
            included['app_settings'] = 'app_config' in manifest
            included['ssh_config'] = bool(manifest.get('ssh_config'))
            included['known_hosts'] = bool(manifest.get('known_hosts'))
            included['secrets'] = bool(manifest.get('credentials'))
            included['private_keys'] = bool(manifest.get('private_keys'))
        requested = dict(DEFAULT_RESTORE_OPTIONS)
        if restore_options:
            for key in BACKUP_OPTION_KEYS:
                if key in restore_options:
                    requested[key] = bool(restore_options[key])
        return {key: included[key] and requested[key] for key in BACKUP_OPTION_KEYS}

    def _apply_parsed(self, import_data: Dict[str, Any], mode: str,
                      create_backup: bool,
                      restore_options: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
        """Validate, auto-backup, then apply a parsed config payload (replace/merge) and reload.
        Shared by JSON import and ``.spbk`` restore."""
        is_valid, validation_error = self._validate_import_data(import_data)
        if not is_valid:
            return False, validation_error
        effective_options = self._restore_options_for_manifest(import_data, restore_options)

        if create_backup:
            backup_path = self._create_auto_backup()
            if backup_path:
                logger.info(f"Created automatic backup at {backup_path}")

        if mode == 'replace':
            success, error = self._import_replace(import_data, effective_options)
        elif mode == 'merge':
            success, error = self._import_merge(import_data, effective_options)
        else:
            return False, f"Invalid import mode: {mode}"

        if success and self.connection_manager:
            try:
                self.connection_manager.load_ssh_config()
            except Exception as e:
                logger.warning(f"Failed to reload SSH config: {e}")
        return success, error

    def _restore_credentials(self, manifest: Dict[str, Any]) -> int:
        """Re-store the manifest's credentials into the selected secret backend. Returns the
        number newly stored. Each is written via the same path normal saves use.

        **Non-destructive, like private-key restore:** a secret that already exists in the
        selected backend is left untouched (never clobbered) and counted in
        ``last_import_skipped_credentials`` — so re-importing an old backup can't silently
        revert a password the user has since rotated."""
        self.last_import_skipped_credentials = 0
        creds = manifest.get('credentials') or []
        if not creds:
            return 0
        try:
            from .secret_storage import get_secret_manager
            from .credential_model import Credential, credential_to_spec
        except Exception:
            logger.warning("Credential restore unavailable", exc_info=True)
            return 0
        mgr = get_secret_manager()
        # The "agent" backend's store() returns True but writes nothing. Don't call it and then
        # report phantom successes — surface the truth so the user can pick a real backend.
        try:
            persists = mgr.persists_secrets()
        except Exception:
            persists = True
        self.last_import_secrets_persisted = persists
        if not persists:
            logger.warning(
                "Selected secret backend does not persist secrets (agent); "
                "%d credential(s) were NOT restored", len(creds))
            return 0
        restored = 0
        skipped = 0
        source_home = manifest.get('source_home')
        target_home = _current_home()
        for c in creds:
            try:
                secret = c.get('secret')
                if secret is None:
                    continue
                cred = Credential(
                    id=c.get('id', ''), type=c.get('type', ''),
                    host=c.get('host'), username=c.get('username'),
                    secret=secret, metadata=dict(c.get('metadata') or {}))
                # A key passphrase is filed under the key's absolute path — rebase it onto this
                # machine's home so it matches the re-homed key file (and how connections look it
                # up). Password/sudo creds key on host+user and are left alone.
                if cred.type == 'key':
                    old_path = cred.metadata.get('key_path') or cred.id
                    new_path = _rebase_home_path(old_path, source_home, target_home)
                    if new_path != old_path:
                        cred.metadata['key_path'] = new_path
                        cred.id = new_path
                spec = credential_to_spec(cred)
                # Never overwrite a secret that already exists in the selected backend.
                if self._secret_already_present(mgr, spec):
                    skipped += 1
                    continue
                if mgr.store(spec, secret):
                    restored += 1
            except Exception:
                logger.warning("Failed to restore a credential", exc_info=True)
        self.last_import_skipped_credentials = skipped
        return restored

    @staticmethod
    def _secret_already_present(mgr, spec) -> bool:
        """True if the selected backend already holds a secret for ``spec`` (so restore leaves
        it alone). Best-effort: if the manager has no per-selection ``lookup`` we treat it as
        absent and let the store proceed."""
        lookup = getattr(mgr, 'lookup', None)
        if not callable(lookup):
            return False
        try:
            return lookup(spec) is not None
        except Exception:
            return False

    def _restore_private_keys(self, manifest: Dict[str, Any]) -> Tuple[int, int]:
        """Restore private keys embedded in a backup to their original paths.

        **Data loss is the red line:** an existing private (or public) key file at the target
        path is NEVER overwritten — regardless of replace/merge mode. We prefer a partial
        import over destroying a key the user already has. Returns
        ``(written, skipped_existing)`` — ``written`` new keys placed, ``skipped_existing``
        left untouched because a file was already there.
        """
        written = 0
        skipped = 0
        source_home = manifest.get('source_home')
        target_home = _current_home()
        for item in manifest.get('private_keys') or []:
            try:
                # Rebase the source machine's home onto this one so a laptop backup lands in
                # /home/<me>/.ssh instead of a non-existent /home/<them>/.ssh.
                path = _rebase_home_path(
                    os.path.expanduser(str(item.get('path') or '')), source_home, target_home)
                raw = item.get('content_b64')
                if not path or not raw:
                    continue
                # NEVER overwrite an existing private key — leave it exactly as it is.
                if os.path.exists(path):
                    logger.info("Private key already exists; leaving it untouched: %s", path)
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(base64.b64decode(raw.encode('ascii')))
                private_mode = int(item.get('mode') or 0o600) & 0o777
                # Private keys must never be restored with group/other access.
                os.chmod(path, (private_mode & 0o600) or 0o600)

                # The matching .pub: also never overwrite an existing file.
                public_path = _rebase_home_path(
                    os.path.expanduser(str(item.get('public_path') or '')),
                    source_home, target_home)
                public_raw = item.get('public_content_b64')
                if public_path and public_raw and not os.path.exists(public_path):
                    os.makedirs(os.path.dirname(public_path) or '.', exist_ok=True)
                    with open(public_path, 'wb') as f:
                        f.write(base64.b64decode(public_raw.encode('ascii')))
                    os.chmod(public_path, int(item.get('public_mode') or 0o644) & 0o777)
                written += 1
            except Exception:
                logger.warning("Failed to restore a private key", exc_info=True)
        return written, skipped

    def apply_imported_manifest(self, manifest: Dict[str, Any], mode: str = 'replace',
                                create_backup: bool = True,
                                restore_options: Optional[Dict[str, Any]] = None
                                ) -> Tuple[bool, Optional[str], int, int]:
        """Apply a decrypted ``.spbk`` manifest: config (replace/merge) **and** restore its
        credentials/private keys. The caller owns passphrase and option prompts."""
        effective_options = self._restore_options_for_manifest(manifest, restore_options)
        success, error = self._apply_parsed(
            manifest, mode, create_backup, restore_options=effective_options)
        restored = (
            self._restore_credentials(manifest)
            if success and effective_options['secrets'] else 0
        )
        restored_keys, skipped_keys = (
            self._restore_private_keys(manifest)
            if success and effective_options['private_keys'] else (0, 0)
        )
        # Existing keys we declined to overwrite — surfaced via an attribute to keep the
        # return shape stable (the import never overwrites a private key).
        self.last_import_skipped_keys = skipped_keys
        return success, error, restored, restored_keys

    def _validate_import_data(self, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate import data structure"""
        if not isinstance(data, dict):
            return False, "Import data must be a JSON object"

        # Check version
        version = data.get('version')
        if version is None:
            return False, "Missing 'version' field in import data"
        if not isinstance(version, int) or version > BACKUP_VERSION:
            return False, f"Unsupported backup version: {version}"

        # Check required fields. New .spbk files may intentionally omit settings,
        # but legacy JSON imports still require an app config section.
        if 'app_config' not in data:
            return False, "Missing 'app_config' field in import data"

        if not isinstance(data['app_config'], dict):
            return False, "'app_config' must be a JSON object"

        # Warn about platform/mode differences (but don't fail)
        current_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        import_isolated = data.get('isolated_mode', False)
        if current_isolated != import_isolated:
            logger.warning(
                f"Import isolated mode ({import_isolated}) differs from current mode ({current_isolated})"
            )

        return True, None

    @staticmethod
    def _atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
        """Write ``text`` via the shared tmp+fsync+rename helper so a crash mid-restore can't
        leave a half-written SSH/app config on disk."""
        from .backup_archive import _atomic_write_bytes
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        _atomic_write_bytes(path, text.encode('utf-8'), mode)

    def _imported_ssh_texts(self, import_data: Dict[str, Any]) -> List[str]:
        """All imported SSH-config text: every bundled tree file, or the single main blob for a
        legacy backup. Used by merge so hosts that lived in the source's Include fragments are
        considered too."""
        tree = import_data.get('ssh_config_files')
        if isinstance(tree, dict) and tree:
            return [v for v in tree.values() if v]
        main = import_data.get('ssh_config')
        return [main] if main else []

    def _restore_ssh_tree(self, import_data: Dict[str, Any],
                          source_home: Optional[str], target_home: str) -> bool:
        """Restore a bundled Include tree under the target's config root. The source's main file
        is written to *this* machine's main path (so a default↔isolated difference maps
        correctly); fragments keep their relative locations.

        Each file's ``Include`` lines are made portable (absolute-under-source-root → relative,
        unbundled/foreign → commented) and its local-path directives are home-rebased. Because
        this is a full *replace*, config files still reachable under the root afterwards that were
        NOT part of the backup are removed — otherwise leftover Include'd fragments would silently
        survive. The pre-import auto-backup captures those files first, so this is reversible.

        Returns False for a legacy backup with no tree (caller writes the single blob)."""
        tree = import_data.get('ssh_config_files')
        if not isinstance(tree, dict) or not tree:
            return False
        main_path = self.get_ssh_config_path()
        root = _ssh_config_root(main_path)
        main_rel = import_data.get('ssh_config_main_rel') or ''
        source_root = import_data.get('ssh_config_root') or None
        written: set = set()
        for rel, content in tree.items():
            if rel == main_rel:
                dest = os.path.abspath(main_path)
            else:
                dest = os.path.normpath(os.path.join(root, rel))
                if not _is_under(dest, root):
                    logger.warning("Skipping unsafe SSH config restore path: %s", rel)
                    continue
            text = _rewrite_includes(content or '', source_root)
            text = _rebase_home_in_text(text, source_home, target_home)
            self._atomic_write_text(dest, text, 0o600)
            written.add(dest)
        logger.info("Restored %d SSH config file(s) under %s", len(written), root)
        self._prune_orphaned_config_files(main_path, root, written)
        return True

    def _prune_orphaned_config_files(self, main_path: str, root: str, written: set) -> None:
        """Remove config files still Include-resolved under ``root`` that this backup did not
        write — so a Replace doesn't keep the target's pre-existing fragments. Never touches the
        main file, files outside the root, or files we don't own."""
        from .ssh_config_utils import resolve_ssh_config_files
        try:
            resolved = resolve_ssh_config_files(main_path)
        except Exception:
            return
        main_abs = os.path.abspath(main_path)
        for f in resolved:
            af = os.path.abspath(f)
            if af == main_abs or af in written or not _is_under(af, root):
                continue
            if not _owned_and_readable(af):
                continue
            try:
                os.remove(af)
                logger.info("Replace removed orphaned config fragment not in backup: %s", af)
            except OSError as exc:
                logger.warning("Could not remove orphaned config fragment %s: %s", af, exc)

    def _import_replace(
        self,
        import_data: Dict[str, Any],
        options: Optional[Dict[str, bool]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Replace all configuration with imported data"""
        try:
            options = self.normalize_backup_options(options)
            source_home = import_data.get('source_home')
            target_home = _current_home()
            # Import SSH config. Prefer the full Include tree (restores the modular layout);
            # fall back to the single main-file blob for legacy backups. Home-prefixed paths are
            # rebased onto this machine and every file is written atomically.
            if options['ssh_config']:
                if not self._restore_ssh_tree(import_data, source_home, target_home) \
                        and import_data.get('ssh_config'):
                    ssh_config_path = self.get_ssh_config_path()
                    ssh_text = _rebase_home_in_text(
                        import_data['ssh_config'], source_home, target_home)
                    self._atomic_write_text(ssh_config_path, ssh_text, 0o600)
                    logger.info(f"Replaced SSH config at {ssh_config_path}")

            # Import known_hosts if present
            if options['known_hosts'] and import_data.get('known_hosts'):
                known_hosts_path = self.get_known_hosts_path()
                if known_hosts_path:
                    self._atomic_write_text(
                        known_hosts_path, import_data['known_hosts'], 0o600)
                    logger.info(f"Replaced known_hosts at {known_hosts_path}")

            # Import app config
            if options['app_settings']:
                app_config = self._app_config_for_restore(import_data['app_config'])
                config_file = Path(get_config_dir()) / 'config.json'
                self._atomic_write_text(
                    str(config_file), json.dumps(app_config, indent=2), 0o600)
                logger.info(f"Replaced app config at {config_file}")

                # Reload config in memory
                self.config.config_data = self.config.load_json_config()

            logger.info("Configuration replaced successfully")
            return True, None

        except Exception as e:
            error_msg = f"Failed to replace configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _import_merge(
        self,
        import_data: Dict[str, Any],
        options: Optional[Dict[str, bool]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Merge imported configuration with existing"""
        try:
            options = self.normalize_backup_options(options)
            source_home = import_data.get('source_home')
            target_home = _current_home()
            # For SSH config: add only genuinely-new hosts, into a sshPilot-owned Include
            # fragment — reusing the app's real Include-aware view instead of a second parser.
            if options['ssh_config']:
                imported_texts = [
                    _rebase_home_in_text(t, source_home, target_home)
                    for t in self._imported_ssh_texts(import_data)
                ]
                if imported_texts:
                    self._merge_ssh_config_fragment(
                        self.get_ssh_config_path(), imported_texts)

            # For known_hosts, append if in isolated mode
            if options['known_hosts'] and import_data.get('known_hosts'):
                known_hosts_path = self.get_known_hosts_path()
                if known_hosts_path:
                    self._merge_known_hosts(known_hosts_path, import_data['known_hosts'])

            # Merge app config
            if options['app_settings']:
                app_config = self._app_config_for_restore(import_data['app_config'])
                self._merge_app_config(app_config)

                # Reload config in memory
                self.config.config_data = self.config.load_json_config()

            logger.info("Configuration merged successfully")
            return True, None

        except Exception as e:
            error_msg = f"Failed to merge configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _merge_ssh_config_fragment(self, main_path: str, imported_texts: List[str]) -> None:
        """Add only genuinely-new hosts from ``imported_texts`` into a sshPilot-owned Include
        fragment, then ensure a single ``Include`` references it.

        Dedup is against the fully Include-resolved set of existing Host names (so hosts already
        living in a fragment are seen). Wildcard/Match/global stanzas are dropped, partial-name
        collisions are reported. Idempotent: the fragment is itself Include-resolved, so re-import
        finds its hosts already present and adds nothing."""
        existing = _existing_host_names(main_path)
        new_text, dropped, collisions = _select_new_host_blocks(imported_texts, existing)
        self.last_merge_dropped_globals = dropped
        self.last_merge_collisions = collisions
        if dropped:
            logger.info("Merge dropped %d wildcard/Match/global stanza(s) from the import "
                        "(never injected into your config)", dropped)
        if collisions:
            logger.warning("Merge skipped %d multi-name host(s) that partially collide with "
                           "existing entries: %s", len(collisions),
                           "; ".join(" ".join(p) for p in collisions))
        if not new_text.strip():
            logger.info("SSH config merge: no new hosts to add")
            return

        root = _ssh_config_root(main_path)
        fragment = os.path.join(root, _IMPORT_FRAGMENT_NAME)
        existing_fragment = ''
        if os.path.exists(fragment):
            try:
                with open(fragment, encoding='utf-8') as f:
                    existing_fragment = f.read()
            except OSError:
                existing_fragment = ''
        if existing_fragment:
            body = existing_fragment
            if not body.endswith('\n'):
                body += '\n'
            body += '\n' + new_text + '\n'
        else:
            body = "# SSH hosts imported by sshPilot\n\n" + new_text + '\n'
        self._atomic_write_text(fragment, body, 0o600)
        self._ensure_include(main_path, fragment)
        logger.info("Merged SSH hosts into fragment %s", fragment)

    def _ensure_include(self, main_path: str, fragment_path: str) -> None:
        """Ensure a top-level ``Include`` for ``fragment_path`` at the start of the main config.

        Must be placed *before* any Host/Match block. Appending at EOF nests the Include
        inside the preceding Host (often ``Host *`` with ``User root``), and OpenSSH's
        first-match-wins then overrides per-host User values from the fragment.
        """
        ensure_import_include_at_top(main_path, fragment_path)

    def _merge_known_hosts(self, target_path: str, imported_hosts: str):
        """Merge known_hosts by appending unique entries"""
        try:
            existing_lines = set()
            if os.path.exists(target_path):
                with open(target_path, encoding='utf-8') as f:
                    existing_lines = set(line.strip() for line in f if line.strip())

            # Add new unique lines
            imported_lines = [line.strip() for line in imported_hosts.split('\n') if line.strip()]
            new_lines = [line for line in imported_lines if line not in existing_lines]

            if new_lines:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'a', encoding='utf-8') as f:
                    for line in new_lines:
                        f.write(line + '\n')
                os.chmod(target_path, 0o600)
                logger.info(f"Merged known_hosts - added {len(new_lines)} new entries")

        except Exception as e:
            logger.error(f"Failed to merge known_hosts: {e}")
            raise

    def _merge_app_config(self, imported_config: Dict[str, Any]):
        """Merge app configuration with existing"""
        try:
            current_config = self.config.config_data.copy()

            # Merge groups
            if 'connection_groups' in imported_config:
                self._merge_groups(
                    current_config.get('connection_groups', {}),
                    imported_config['connection_groups']
                )

            # Merge connections metadata
            if 'connections_meta' in imported_config:
                current_meta = current_config.get('connections_meta', {})
                imported_meta = imported_config['connections_meta']
                # Only add new connection metadata, don't overwrite existing
                for conn_key, meta in imported_meta.items():
                    if conn_key not in current_meta:
                        current_meta[conn_key] = meta
                current_config['connections_meta'] = current_meta

            # Merge shortcuts (keep existing, add new)
            if 'shortcuts' in imported_config:
                current_shortcuts = current_config.get('shortcuts', {})
                imported_shortcuts = imported_config['shortcuts']
                for action, keys in imported_shortcuts.items():
                    if action not in current_shortcuts:
                        current_shortcuts[action] = keys
                current_config['shortcuts'] = current_shortcuts

            # For other settings, we can be more conservative and keep existing values
            # But we can add new keys that don't exist
            for key, value in imported_config.items():
                if key not in ['connection_groups', 'connections_meta', 'shortcuts', 'config_version']:
                    if key not in current_config:
                        current_config[key] = value

            # Update config version to current
            current_config = self._app_config_for_restore(current_config)
            current_config['config_version'] = CONFIG_VERSION

            # Save merged config
            config_file = Path(get_config_dir()) / 'config.json'
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(current_config, f, indent=2)

            logger.info("Merged app configuration")

        except Exception as e:
            logger.error(f"Failed to merge app config: {e}")
            raise

    def _merge_groups(self, current_groups: Dict[str, Any], imported_groups: Dict[str, Any]):
        """Merge group data, preserving existing groups and adding new ones"""
        try:
            current_group_data = current_groups.get('groups', {})
            imported_group_data = imported_groups.get('groups', {})

            # Build mapping of group names to IDs for existing groups
            existing_names = {
                info['name'].lower(): group_id 
                for group_id, info in current_group_data.items()
            }

            # Import groups that don't exist by name
            import uuid
            for imported_id, imported_info in imported_group_data.items():
                group_name = imported_info.get('name', '')
                if group_name.lower() not in existing_names:
                    # Create new group with new UUID to avoid conflicts
                    new_id = str(uuid.uuid4())
                    new_info = imported_info.copy()
                    new_info['id'] = new_id
                    new_info['order'] = len(current_group_data)
                    # Preserve imported color
                    current_group_data[new_id] = new_info
                    logger.info(f"Added new group: {group_name}")

            # Update the groups in current config
            if 'groups' not in current_groups:
                current_groups['groups'] = {}
            current_groups['groups'] = current_group_data

        except Exception as e:
            logger.error(f"Failed to merge groups: {e}")
            raise

    def _create_auto_backup(self) -> Optional[str]:
        """Create automatic backup before import"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f"auto_backup_{timestamp}.json"
            backup_path = self.backup_dir / backup_filename
            
            success, error = self.export_configuration(str(backup_path))
            if success:
                return str(backup_path)
            else:
                logger.error(f"Failed to create auto backup: {error}")
                return None

        except Exception as e:
            logger.error(f"Failed to create auto backup: {e}")
            return None

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups"""
        backups = []
        try:
            if not self.backup_dir.exists():
                return backups

            candidates = sorted(set(self.backup_dir.glob('*.json'))
                                | set(self.backup_dir.glob('*.spbk')))
            for backup_file in candidates:
                try:
                    stat = backup_file.stat()
                    backups.append({
                        'path': str(backup_file),
                        'name': backup_file.name,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime),
                    })
                except Exception as e:
                    logger.warning(f"Failed to stat backup file {backup_file}: {e}")

            # Sort by modification time, newest first
            backups.sort(key=lambda x: x['modified'], reverse=True)

        except Exception as e:
            logger.error(f"Failed to list backups: {e}")

        return backups

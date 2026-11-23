"""Evaluate an unsaved root SSH configuration with OpenSSH itself.

Advanced Configuration shows, for a target host, what OpenSSH would use if
the draft were saved. The answer has to be the one a launch would get from
the saved file, so the draft is read in the launch's own context:

* **System-wide configuration.** A launch from the user's ``~/.ssh/config``
  passes no ``-F``, and OpenSSH then reads ``/etc/ssh/ssh_config`` after it.
  A draft can only be handed over with ``-F``, which skips that file, so it
  is appended by hand, read last and unconditionally (``Match all`` +
  ``Include``), exactly as the port-forward launch does for its own
  rewritten copy. Any other root (Isolated Mode, ``SSHPILOT_SSH_DIR``) is
  launched with ``-F`` and never sees the system file; neither does its
  draft.
* **Include.** The draft is written unchanged, so its ``Include`` lines are
  OpenSSH's to read. With ``-F``, as with the default file, a relative
  ``Include`` is looked up in ``~/.ssh``: the draft's location does not
  matter.
* **Settings.** The options Settings ▸ SSH Options force are passed on the
  command line, ahead of the configuration, as a launch passes them.

Nothing here decides whether a block applies or which value wins: both come
from ``ssh -G``. Which blocks of the draft apply is asked with a second
query that plants a ``SendEnv`` marker in each block (OpenSSH collects
``SendEnv``, so every block that applies reports its marker). The walk that
names the line behind each value follows those answers.

``ssh -G`` runs ``Match exec`` commands. A draft that adds one is not
evaluated until the caller allows that very command: see
:func:`new_exec_commands`.

The queries can be slow (DNS canonicalization, a ``Match exec`` command), and
the window asks again on every edit. An evaluation therefore has one time
budget for all its queries and can be abandoned: *cancelled* is polled while
OpenSSH runs, and a superseded evaluation kills its ``ssh`` at once.
"""
from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..api.models.connections import (
    EvaluateSshConfigDraftRequest,
    SshConfigDraftError,
    SshConfigDraftEvaluation,
    SshConfigDraftInclude,
    SshConfigDraftLine,
    SshConfigDraftLineState,
    SshConfigDraftSource,
    SshConfigDraftValue,
)
from ..platform.paths import openssh_user_dir
from ..platform.process import hidden_console_kwargs
from ..ssh_config_blocks import GLOBAL, scan_blocks
from ..ssh_config_document import (
    _openssh_argv_split,
    _split_keyword,
    split_config_lines,
)
from .connections.auth_preferences import canonical_keyword
from .ssh_config_effective import (
    ACCUMULATING_DIRECTIVES,
    _include_children,
    _safe_host_identifier,
    earlier_directives,
    launch_config_file,
    openssh_system_config,
    parse_openssh_config_errors,
)

#: Seconds one evaluation may spend in OpenSSH, all its queries together.
_EVALUATION_BUDGET = 10.0
_POLL_SECONDS = 0.05

#: Options that fill one slot: OpenSSH uses whichever it obtains first and
#: ignores the other from then on.
_SHARED_SLOTS = {"proxyjump": "proxy", "proxycommand": "proxy"}


class DraftEvaluationCancelled(Exception):
    """The evaluation was abandoned: a newer draft took its place."""


def _slot(setting: str) -> str:
    return _SHARED_SLOTS.get(setting, setting)

_INCLUDE_RE = re.compile(r"^(\s*include)(\s*=\s*|\s+)(.+?)\s*$", re.IGNORECASE)


# -- Match exec ---------------------------------------------------------------

def exec_commands(text: str) -> Tuple[str, ...]:
    """The commands of every ``Match ... exec`` criterion in *text*."""
    commands: List[str] = []
    for raw in split_config_lines(text or ""):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, remainder = _split_keyword(stripped)
        if keyword != "match":
            continue
        try:
            words = _openssh_argv_split(remainder)
        except ValueError:
            words = remainder.split()
        index = 0
        while index < len(words):
            name, separator, attached = words[index].partition("=")
            if name.lower().lstrip("!") == "exec":
                if separator:
                    commands.append(attached)
                elif index + 1 < len(words):
                    commands.append(words[index + 1])
                    index += 1
            index += 1
    return tuple(commands)


def new_exec_commands(
    draft_text: str, saved_text: str, allowed: Sequence[str] = ()
) -> Tuple[str, ...]:
    """``Match exec`` commands the draft has, the saved file has not, and
    the user has not agreed to.

    The saved configuration's commands already run on every connection;
    evaluating them is nothing new. A command that only the draft holds may
    be half typed, and must not run because a key was pressed. Agreement is
    to one command, word for word: a command edited afterwards, or another
    new one, is pending again.
    """
    known = set(exec_commands(saved_text)) | set(allowed)
    found: List[str] = []
    for command in exec_commands(draft_text):
        if command not in known and command not in found:
            found.append(command)
    return tuple(found)


# -- the launch context -------------------------------------------------------

@dataclass(frozen=True)
class DraftContext:
    """How a launch would read the root configuration at *root_path*."""

    root_path: str
    isolated: bool = False
    #: Settings' forced options, as ``-o Keyword=value`` pairs.
    overrides: Tuple[str, ...] = ()

    @property
    def system_config(self) -> Optional[str]:
        """The system-wide file a launch reads after the root, if any.

        It is read only when the launch passes no ``-F``: the same test the
        launch itself makes (:func:`launch_config_file`).
        """
        if self.isolated:
            return None
        if launch_config_file(self.root_path) is not None:
            return None
        return openssh_system_config()


def _override_args(overrides: Sequence[str]) -> List[str]:
    from .settings.ssh_overrides import select_override_args

    return select_override_args(list(overrides), flags=False) if overrides else []


def _forced(overrides: Sequence[str]) -> Dict[str, str]:
    from .settings.ssh_overrides import forced_options

    return forced_options(list(overrides)) if overrides else {}


def _write_private(path: str, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _system_copy(system: str, directory: str) -> Optional[str]:
    """A copy of the system-wide file for the draft to include.

    Read through the user's configuration, a relative ``Include`` in it
    would be looked up in ``~/.ssh``; on its own OpenSSH looks next to the
    file. The copy spells those paths out.
    """
    try:
        with open(system, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return None
    base = os.path.dirname(os.path.abspath(system))
    lines: List[str] = []
    for raw in split_config_lines(text, keepends=True):
        body = raw.rstrip("\r\n")
        match = _INCLUDE_RE.match(body)
        if match is None:
            lines.append(raw)
            continue
        try:
            patterns = _openssh_argv_split(match.group(3))
        except ValueError:
            lines.append(raw)
            continue
        absolute = [
            pattern if os.path.isabs(os.path.expanduser(pattern)) or pattern.startswith("~")
            else os.path.join(base, pattern)
            for pattern in patterns
        ]
        quoted = " ".join(f'"{pattern}"' for pattern in absolute)
        lines.append(f"{match.group(1)}{match.group(2)}{quoted}{raw[len(body):]}")
    path = os.path.join(directory, "system_ssh_config")
    _write_private(path, "".join(lines))
    return path


def _launch_text(draft_text: str, system_copy: Optional[str]) -> str:
    """The draft as a launch would read it: itself, then the system file."""
    if not system_copy:
        return draft_text
    text = draft_text
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    quoted = system_copy.replace('"', '\\"')
    return (
        text
        + "\n# sshpilot: the system-wide file, which -F would skip\n"
        + f'Match all\nInclude "{quoted}"\n'
    )


class _Run:
    """One evaluation's time budget and its way out."""

    def __init__(self, cancelled: Optional[Callable[[], bool]] = None) -> None:
        self.deadline = time.monotonic() + _EVALUATION_BUDGET
        self._cancelled = cancelled

    def check(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise DraftEvaluationCancelled()


def _stop(process: "subprocess.Popen") -> None:
    """End ``ssh`` and whatever a ``Match exec`` started under it."""
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.communicate(timeout=2)
    except Exception:
        pass


def _query(
    config_file: str,
    host: str,
    run: Optional[_Run] = None,
    *,
    overrides: Sequence[str] = (),
    user: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[Dict[str, List[str]], List[Tuple[str, str]], str]:
    """``ssh -G``: ``(values by keyword, ordered pairs, stderr on failure)``."""
    run = run or _Run()
    run.check()
    command = ["ssh", "-F", config_file, *_override_args(overrides)]
    if user:
        command.extend(["-l", user])
    if port is not None:
        command.extend(["-p", str(port)])
    command.extend(["-G", host])
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=os.name != "nt",
            **hidden_console_kwargs(),
        )
    except OSError as error:
        return {}, [], str(error)
    while True:
        try:
            stdout, stderr = process.communicate(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            try:
                run.check()
            except DraftEvaluationCancelled:
                _stop(process)
                raise
            if time.monotonic() >= run.deadline:
                _stop(process)
                return {}, [], "ssh -G timed out"
    if process.returncode != 0:
        return {}, [], stderr.strip() or f"ssh -G exited {process.returncode}"
    values: Dict[str, List[str]] = {}
    pairs: List[Tuple[str, str]] = []
    for raw in stdout.splitlines():
        key, _separator, value = raw.strip().partition(" ")
        if not key:
            continue
        key = key.lower()
        value = value.strip()
        values.setdefault(key, []).append(value)
        pairs.append((key, value))
    return values, pairs, ""


def _errors(stderr: str, draft_file: str, context_files: Dict[str, str]) -> Tuple[SshConfigDraftError, ...]:
    """OpenSSH's complaints, with temporary paths mapped back."""
    found: List[SshConfigDraftError] = []
    for item in parse_openssh_config_errors(stderr):
        path = str(item.get("file") or "")
        if os.path.abspath(path) == os.path.abspath(draft_file):
            path = ""
        else:
            path = context_files.get(os.path.abspath(path), path)
        found.append(SshConfigDraftError(
            file=path,
            line=int(item.get("line") or 0),
            diagnostic=str(item.get("diagnostic") or ""),
        ))
    if not found:
        diagnostic = stderr.replace(draft_file, "").strip()
        found.append(SshConfigDraftError(file="", line=0, diagnostic=diagnostic))
    return tuple(found)


def _with_probes(draft_text: str, token: str) -> Tuple[str, Dict[str, int]]:
    """The draft with a ``SendEnv`` marker under every Host/Match header.

    Returns the text and ``marker -> header line``. OpenSSH collects
    ``SendEnv`` from every block that applies, so the markers it reports
    are the blocks that apply, whatever their patterns or criteria.
    """
    lines = split_config_lines(draft_text, keepends=True)
    markers: Dict[str, int] = {}
    newline = "\r\n" if "\r\n" in draft_text else "\n"
    for block in sorted(scan_blocks(draft_text), key=lambda b: -b.line):
        if block.kind == GLOBAL:
            continue
        marker = f"SSHPILOT_PROBE_{token}_{block.line}"
        markers[marker] = block.line
        header = lines[block.line - 1]
        if not header.endswith(("\n", "\r")):
            lines[block.line - 1] = header + newline
        lines.insert(block.line, f"SendEnv {marker}{newline}")
    return "".join(lines), markers


def _effective_map(values: Dict[str, List[str]]) -> Dict[str, object]:
    return {
        key: (entries[0] if len(entries) == 1 else list(entries))
        for key, entries in values.items()
    }


def evaluate_draft(
    request: EvaluateSshConfigDraftRequest,
    context: DraftContext,
    saved_text: str = "",
    cancelled: Optional[Callable[[], bool]] = None,
) -> SshConfigDraftEvaluation:
    """Resolve ``request.host`` from the draft as a launch would.

    Raises :class:`DraftEvaluationCancelled` when *cancelled* turns true
    while OpenSSH is being asked.
    """
    run = _Run(cancelled)
    system = context.system_config
    base = dict(system_config=system or "", isolated=bool(context.isolated))
    forced = _forced(context.overrides)
    forced_values = tuple(SshConfigDraftValue(key, value) for key, value in forced.items())
    host = _safe_host_identifier(request.host)
    if not host:
        return SshConfigDraftEvaluation(
            errors=(SshConfigDraftError("", 0, "invalid host name"),),
            forced=forced_values, **base,
        )
    pending = new_exec_commands(request.text, saved_text, request.allowed_exec)
    if pending:
        return SshConfigDraftEvaluation(
            pending_exec=pending, forced=forced_values, **base
        )

    with tempfile.TemporaryDirectory(prefix="sshpilot-config-draft-") as directory:
        system_copy = _system_copy(system, directory) if system else None
        context_files = {os.path.abspath(system_copy): system} if system_copy else {}
        draft_file = os.path.join(directory, "config")
        _write_private(draft_file, _launch_text(request.text, system_copy))
        query_args = dict(overrides=context.overrides, user=request.user, port=request.port)

        values, pairs, stderr = _query(draft_file, host, run, **query_args)
        if stderr:
            return SshConfigDraftEvaluation(
                errors=_errors(stderr, draft_file, context_files),
                forced=forced_values, **base,
            )

        probed_text, markers = _with_probes(request.text, secrets.token_hex(6).upper())
        matched: Set[int] = set()
        if markers:
            probe_file = os.path.join(directory, "probe")
            _write_private(probe_file, _launch_text(probed_text, system_copy))
            probed, _pairs, probe_error = _query(probe_file, host, run, **query_args)
            if not probe_error:
                for entry in probed.get("sendenv", ()):
                    for name in entry.split():
                        if name in markers:
                            matched.add(markers[name])

        defaults_values, _pairs, _error = _query(os.devnull, host, run)
        lines, sources, includes = _walk(
            request.text, host, matched, forced, _effective_map(values), system,
        )

    defaults = tuple(sorted(
        key for key, entries in values.items()
        if key not in {source.key for source in sources}
        and key not in forced
        and defaults_values.get(key) == entries
    ))
    return SshConfigDraftEvaluation(
        values=tuple(SshConfigDraftValue(key, value) for key, value in pairs),
        defaults=defaults,
        matched_blocks=tuple(sorted(matched)),
        lines=lines,
        sources=sources,
        includes=includes,
        forced=forced_values,
        **base,
    )


def _walk(
    draft_text: str,
    host: str,
    matched: Set[int],
    forced: Dict[str, str],
    effective: Dict[str, object],
    system: Optional[str],
) -> Tuple[
    Tuple[SshConfigDraftLine, ...],
    Tuple[SshConfigDraftSource, ...],
    Tuple[SshConfigDraftInclude, ...],
]:
    """Name the line behind each option, in the order OpenSSH reads.

    The blocks that apply are OpenSSH's answer (*matched*). Within them the
    first line of an option is the one used; a line inside a file an
    ``Include`` reads counts at the Include's place. Lines of files outside
    the draft are found with the walk the rest of the app uses for "an
    earlier block decides this".
    """
    # Keyed by slot: ProxyJump and ProxyCommand compete for one, so the
    # first of the two decides and the other is not used.
    winners: Dict[str, SshConfigDraftSource] = {}
    forced_slots = {_slot(canonical_keyword(key)) for key in forced}
    lines: List[SshConfigDraftLine] = []
    includes: List[SshConfigDraftInclude] = []
    include_base = str(openssh_user_dir())

    def take_file(path: str) -> None:
        try:
            entries = earlier_directives(host, path, effective, _whole=True)
        except Exception:
            entries = []
        for entry in entries:
            setting = canonical_keyword(str(entry["key"]))
            if _slot(setting) in winners or _slot(setting) in forced_slots:
                continue
            winners[_slot(setting)] = SshConfigDraftSource(
                key=setting,
                file=str(entry["file"]),
                line=int(entry["line"]),
                block=str(entry["block"]),
            )

    for block in scan_blocks(draft_text):
        applies = block.kind == GLOBAL or block.line in matched
        header = "" if block.kind == GLOBAL else (
            ("Host " if block.kind == "host" else "Match ") + block.patterns
        )
        for item in block.lines:
            if not item.keyword.isalnum():
                continue
            if not applies:
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.INACTIVE))
                continue
            if item.keyword == "include":
                files = tuple(_include_children(include_base, item.text))
                includes.append(SshConfigDraftInclude(item.line, files))
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.INCLUDE))
                for path in files:
                    take_file(path)
                continue
            if item.keyword in ACCUMULATING_DIRECTIVES:
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.ACCUMULATED))
                continue
            setting = canonical_keyword(item.keyword)
            if _slot(setting) in forced_slots:
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.FORCED))
                continue
            winner = winners.get(_slot(setting))
            if winner is None:
                winners[_slot(setting)] = SshConfigDraftSource(setting, "", item.line, header)
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.APPLIED))
            else:
                lines.append(SshConfigDraftLine(
                    item.line, SshConfigDraftLineState.SHADOWED,
                    decided_file=winner.file,
                    decided_line=winner.line,
                    decided_block=winner.block,
                ))
    if system:
        take_file(system)
    return tuple(lines), tuple(winners.values()), tuple(includes)

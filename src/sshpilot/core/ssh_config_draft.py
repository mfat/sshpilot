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
import threading
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
from ..platform.fs import makedirs_private
from ..platform.paths import openssh_user_dir
from ..platform.process import hidden_console_kwargs
from ..ssh_config_blocks import GLOBAL, keyword_of, scan_blocks
from ..ssh_config_document import (
    _openssh_argv_split,
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
# keyword (possibly quoted), then its argument
_CONFIG_LINE_RE = re.compile(r'^("[^"]*"|[^\s=]+)(?:\s*=\s*|\s+|$)(.*)$', re.DOTALL)


# -- Match exec ---------------------------------------------------------------

#: Match criteria that take no argument; every other one takes exactly one.
_MATCH_NO_ARGUMENT = frozenset({"all", "canonical", "final"})
_INCLUDE_DEPTH = 16


def _match_exec(remainder: str) -> List[str]:
    """The commands of the ``exec`` criteria on one ``Match`` line.

    The line is read criterion by criterion, as OpenSSH reads it, so that an
    argument which happens to be the word ``exec`` (``Match host exec``) is
    not mistaken for the criterion.
    """
    try:
        words = _openssh_argv_split(remainder)
    except ValueError:
        words = remainder.split()
    commands: List[str] = []
    index = 0
    while index < len(words):
        name, separator, attached = words[index].partition("=")
        name = name.lower().lstrip("!")
        index += 1
        if name in _MATCH_NO_ARGUMENT:
            continue
        if separator:
            argument = attached
        elif index < len(words):
            argument = words[index]
            index += 1
        else:
            break
        if name == "exec":
            commands.append(argument)
    return commands


def exec_commands(text: str, *, follow_includes: bool = False) -> Tuple[str, ...]:
    """The commands of every ``Match ... exec`` criterion in *text*.

    With *follow_includes*, also those in the files its ``Include`` lines
    read (and theirs): a draft that adds an Include brings that file's
    commands with it.
    """
    commands: List[str] = []
    seen: Set[str] = set()

    def scan(body: str, depth: int) -> None:
        for raw in split_config_lines(body or ""):
            keyword = keyword_of(raw)
            if not keyword:
                continue
            stripped = raw.strip()
            match = _CONFIG_LINE_RE.match(stripped)
            remainder = match.group(2) if match else ""
            if keyword == "match":
                commands.extend(_match_exec(remainder))
            elif keyword == "include" and follow_includes and depth < _INCLUDE_DEPTH:
                for path in _include_children(str(openssh_user_dir()), f"Include {remainder}"):
                    path = os.path.abspath(path)
                    if path in seen:
                        continue
                    seen.add(path)
                    try:
                        with open(path, encoding="utf-8", newline="") as handle:
                            scan(handle.read(), depth + 1)
                    except (OSError, UnicodeError):
                        continue

    scan(text, 0)
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
    known = set(exec_commands(saved_text, follow_includes=True)) | set(allowed)
    found: List[str] = []
    for command in exec_commands(draft_text, follow_includes=True):
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
    #: The file the draft stands for, when it is not the root: one the root
    #: reads through ``Include``. The caller has checked that it is.
    target_path: Optional[str] = None

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


def _same_file(first: str, second: str) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.abspath(first) == os.path.abspath(second)


def _mirror_tree(
    root_text: str,
    root_path: str,
    target_path: str,
    target_text: str,
    directory: str,
    include_base: str,
) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    """The root as OpenSSH would read it with *target_path* holding
    *target_text*: ``(root copy's text, target copy's path, copy -> real)``.

    The draft of an included file is no configuration of its own: what it
    does depends on where the root reads it, and on what was read before.
    So the files between the root and it are copied, each ``Include`` that
    leads to it rewritten to name the copy, and OpenSSH is asked about the
    root copy. Files off that path are read where they are; every Include
    of a copied file names its files in full, in the order OpenSSH reads
    them.

    The root's text is ``None`` when the root never reads the file.
    """
    copies: Dict[str, Optional[str]] = {}
    real_of: Dict[str, str] = {}
    visiting: Set[str] = set()
    target_copy: List[str] = []

    def rewritten(text: str, depth: int) -> Tuple[str, bool]:
        lines: List[str] = []
        leads = False
        for raw in split_config_lines(text, keepends=True):
            body = raw.rstrip("\r\n")
            match = _INCLUDE_RE.match(body)
            if match is None:
                lines.append(raw)
                continue
            children = _include_children(include_base, body)
            named = [(child, copy_of(child, depth + 1)) for child in children]
            if not named:
                lines.append(raw)       # reads nothing: nothing to spell out
                continue
            leads = leads or any(copy for _child, copy in named)
            # Spelled out even when none leads to the target: a relative
            # path is looked up from ~/.ssh by OpenSSH, and must not come to
            # depend on where this copy lies for anyone else reading it.
            quoted = " ".join(
                '"' + (copy or os.path.abspath(child)).replace('"', '\\"') + '"'
                for child, copy in named
            )
            lines.append(f"{match.group(1)}{match.group(2)}{quoted}{raw[len(body):]}")
        return "".join(lines), leads

    def copy_of(path: str, depth: int) -> Optional[str]:
        real = os.path.abspath(path)
        if real in copies:
            return copies[real]
        if real in visiting or depth > 32:
            return None
        if _same_file(real, target_path):
            # Its own Include lines are OpenSSH's to read: nothing they
            # name can lead back to it.
            text, leads = target_text, True
        else:
            visiting.add(real)
            try:
                with open(real, encoding="utf-8", newline="") as handle:
                    text, leads = rewritten(handle.read(), depth)
            except (OSError, UnicodeError):
                text, leads = "", False
            visiting.discard(real)
        if not leads:
            copies[real] = None
            return None
        copy = os.path.join(directory, f"file{len(real_of)}")
        _write_private(copy, text)
        copies[real] = copy
        real_of[os.path.abspath(copy)] = real
        if _same_file(real, target_path):
            target_copy.append(copy)
        return copy

    visiting.add(os.path.abspath(root_path))
    text, leads = rewritten(root_text, 0)
    if not leads or not target_copy:
        return None, None, real_of
    return text, target_copy[0], real_of


def write_draft_for_launch(
    text: str, context: DraftContext, directory: str
) -> Optional[str]:
    """Write the draft into *directory* as a launch would read it, and
    return the file to pass with ``-F``.

    The same reading :func:`evaluate_draft` asks OpenSSH about: the
    system-wide file after the draft when a launch would read it, and for
    the draft of an included file, copies of the files that lead to it with
    the draft in its place. None when the root does not read that file.
    """
    system = context.system_config
    system_copy = _system_copy(system, directory) if system else None
    launch_text: Optional[str] = text
    if context.target_path is not None:
        try:
            with open(context.root_path, encoding="utf-8", newline="") as handle:
                root_text = handle.read()
        except (OSError, UnicodeError):
            root_text = ""
        folder = os.path.join(directory, "tree")
        makedirs_private(folder)
        launch_text, _copy, _real = _mirror_tree(
            root_text, context.root_path, context.target_path, text, folder,
            str(openssh_user_dir()),
        )
        if launch_text is None:
            return None
    path = os.path.join(directory, "config")
    _write_private(path, _launch_text(launch_text, system_copy))
    return path


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


# OpenSSH's built-in values for a host, by host name: they do not depend on
# any configuration, so one question per host is enough for a session.
_defaults_cache: "Dict[str, Dict[str, List[str]]]" = {}
_DEFAULTS_CACHE_LIMIT = 256


def _builtin_defaults(host: str, run: "_Run") -> Dict[str, List[str]]:
    cached = _defaults_cache.get(host)
    if cached is not None:
        return cached
    values, _pairs, error = _query(os.devnull, host, run)
    if error:
        return {}
    if len(_defaults_cache) >= _DEFAULTS_CACHE_LIMIT:
        _defaults_cache.clear()
    _defaults_cache[host] = values
    return values


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
        query_args = dict(overrides=context.overrides, user=request.user, port=request.port)
        token = secrets.token_hex(6).upper()
        probed_text, markers = _with_probes(request.text, token)

        # The draft of an included file: OpenSSH is asked about the root,
        # with the copies that lead to the draft in the file's place.
        target = context.target_path
        launch_text, probed_launch = request.text, probed_text
        answers_for = draft_file        # the file OpenSSH's complaints are about
        read_marker = ""
        if target is not None:
            try:
                with open(context.root_path, encoding="utf-8", newline="") as handle:
                    root_text = handle.read()
            except (OSError, UnicodeError):
                root_text = ""
            include_base = str(openssh_user_dir())
            # A marker above everything in the file: whether the root
            # reads it at all, for this host (an Include may stand under
            # a Host line).
            read_marker = f"SSHPILOT_PROBE_{token}_READ"
            markers[read_marker] = 0
            probed_text = f"SendEnv {read_marker}\n{probed_text}"
            trees = []
            for name, text in (("plain", request.text), ("probed", probed_text)):
                folder = os.path.join(directory, name)
                makedirs_private(folder)
                trees.append(_mirror_tree(
                    root_text, context.root_path, target, text, folder, include_base
                ))
            (launch_text, target_copy, real_of), (probed_launch, _copy, probed_real) = trees
            if launch_text is None or probed_launch is None:
                return SshConfigDraftEvaluation(
                    errors=(SshConfigDraftError(
                        "", 0, "the SSH configuration does not read this file"
                    ),),
                    forced=forced_values, **base,
                )
            context_files.update(real_of)
            context_files.update(probed_real)
            context_files[os.path.abspath(draft_file)] = context.root_path
            answers_for = target_copy
        _write_private(draft_file, _launch_text(launch_text, system_copy))

        # The two questions about the draft do not depend on each other:
        # they are asked at the same time. (A Match exec command therefore
        # runs twice at once, as it runs once per connection anyway.)
        probe_answer: List[tuple] = []
        probe_thread = None
        if markers:
            probe_file = os.path.join(directory, "probe")
            _write_private(probe_file, _launch_text(probed_launch, system_copy))

            def ask_probe() -> None:
                try:
                    probe_answer.append(_query(probe_file, host, run, **query_args))
                except DraftEvaluationCancelled:
                    pass

            probe_thread = threading.Thread(
                target=ask_probe, name="sshpilot-draft-probe", daemon=True
            )
            probe_thread.start()
        try:
            values, pairs, stderr = _query(draft_file, host, run, **query_args)
        finally:
            if probe_thread is not None:
                probe_thread.join()
        if stderr:
            return SshConfigDraftEvaluation(
                errors=_errors(stderr, answers_for, context_files),
                forced=forced_values, **base,
            )
        run.check()
        if markers and (not probe_answer or probe_answer[0][2]):
            # Without the second answer nothing says which blocks apply.
            # Reporting values with every block "skipped" would be a wrong
            # answer; no answer is the honest one.
            reason = probe_answer[0][2] if probe_answer else "ssh -G did not answer"
            return SshConfigDraftEvaluation(
                errors=(SshConfigDraftError("", 0, reason),),
                forced=forced_values, **base,
            )
        matched: Set[int] = set()
        read = target is None
        if probe_answer:
            for entry in probe_answer[0][0].get("sendenv", ()):
                for name in entry.split():
                    if name == read_marker:
                        read = True
                    elif name in markers:
                        matched.add(markers[name])

        defaults_values = _builtin_defaults(host, run)
        if target is None:
            lines, sources, includes = _walk(
                request.text, host, matched, forced, _effective_map(values), system,
            )
        else:
            lines, sources, includes = _walk_included(
                request.text, host, matched if read else None, forced,
                _effective_map(values), draft_file, target_copy, context_files,
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


def _walk_included(
    draft_text: str,
    host: str,
    matched: Optional[Set[int]],
    forced: Dict[str, str],
    effective: Dict[str, object],
    root_copy: str,
    target_copy: str,
    real_of: Dict[str, str],
) -> Tuple[
    Tuple[SshConfigDraftLine, ...],
    Tuple[SshConfigDraftSource, ...],
    Tuple[SshConfigDraftInclude, ...],
]:
    """:func:`_walk` for the draft of an included file.

    What a line of such a file does depends on what OpenSSH read before
    reaching it, in other files. The line behind each option is therefore
    looked for across the whole configuration (the root copy, with the
    draft where the file stands), with the walk the rest of the app uses;
    a line of the draft is the one used when that walk names it. Which of
    the draft's blocks apply is OpenSSH's answer, as ever: *matched* is
    ``None`` when the root does not read the file for this host at all.
    """
    winners: Dict[str, SshConfigDraftSource] = {}
    forced_slots = {_slot(canonical_keyword(key)) for key in forced}
    own = os.path.abspath(target_copy)
    try:
        entries = earlier_directives(host, root_copy, effective, _whole=True)
    except Exception:
        entries = []
    for entry in entries:
        setting = canonical_keyword(str(entry["key"]))
        if _slot(setting) in winners or _slot(setting) in forced_slots:
            continue
        path = os.path.abspath(str(entry["file"]))
        winners[_slot(setting)] = SshConfigDraftSource(
            key=setting,
            file="" if path == own else real_of.get(path, str(entry["file"])),
            line=int(entry["line"]),
            block=str(entry["block"]),
        )

    lines: List[SshConfigDraftLine] = []
    includes: List[SshConfigDraftInclude] = []
    include_base = str(openssh_user_dir())
    for block in scan_blocks(draft_text):
        applies = matched is not None and (block.kind == GLOBAL or block.line in matched)
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
                continue
            if item.keyword in ACCUMULATING_DIRECTIVES:
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.ACCUMULATED))
                continue
            setting = canonical_keyword(item.keyword)
            if _slot(setting) in forced_slots:
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.FORCED))
                continue
            winner = winners.get(_slot(setting))
            if winner is None or (not winner.file and winner.line == item.line):
                lines.append(SshConfigDraftLine(item.line, SshConfigDraftLineState.APPLIED))
            else:
                lines.append(SshConfigDraftLine(
                    item.line, SshConfigDraftLineState.SHADOWED,
                    decided_file=winner.file,
                    decided_line=winner.line,
                    decided_block=winner.block,
                ))
    return tuple(lines), tuple(winners.values()), tuple(includes)


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


# -- the host's own lines in a rendered file ---------------------------------


def own_option_lines(text: str, host: str) -> Tuple[Tuple[int, str, str], ...]:
    """``(line, keyword, value)`` of every option line of *host*'s own
    ``Host`` blocks in *text*: the blocks whose ``Host`` line names it
    literally (a pattern that matches it is another block)."""
    from ..ssh_config_document import split_host_tokens

    found: List[Tuple[int, str, str]] = []
    for block in scan_blocks(text):
        if block.kind != "host":
            continue
        try:
            tokens = split_host_tokens(block.patterns)
        except Exception:
            tokens = block.patterns.split()
        if host not in tokens:
            continue
        found.extend((item.line, item.keyword, item.value) for item in block.lines)
    return tuple(found)


def without_own_lines(text: str, host: str, keywords) -> str:
    """*text* with *host*'s own lines for *keywords* left out: what the host
    inherits for those keywords. Either spelling of a renamed keyword is
    one setting."""
    wanted = {canonical_keyword(keyword) for keyword in keywords}
    dropped = {
        line for line, keyword, _value in own_option_lines(text, host)
        if canonical_keyword(keyword) in wanted
    }
    if not dropped:
        return text
    return "".join(
        raw for number, raw in enumerate(
            split_config_lines(text, keepends=True), start=1
        )
        if number not in dropped
    )

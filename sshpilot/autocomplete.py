"""Terminal autocomplete engine for the embedded PyXterm backend (GTK-free).

Reconstructs the shell's edit line from the raw input stream (``LineTracker``),
queries pluggable ``SuggestionProvider``s (session history, shell history files,
Command Blocks snippets), and produces the JSON payload the in-page popup
(``window.sshpilotAC`` in ``xterm_shell.py``) renders.

There is no shell integration (no OSC 133): the tracker is a heuristic that
*invalidates* on anything it cannot model (escape sequences, Tab completion,
unknown control chars) and re-validates only on Enter / Ctrl+C / Ctrl+U — so
the popup fails quiet, never with a wrong completion. Ctrl+L (clear-screen)
is ignored: the shell redraws the same edit line.

Kept GTK-free so it is unit-testable headlessly (``tests/test_autocomplete.py``).
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Protocol, Set


class Suggestion(NamedTuple):
    text: str
    source: str  # "session" | "remote" | "history" | "snippet" (more later: ai, …)


class SuggestionProvider(Protocol):
    def suggestions(self, prefix: str, limit: int) -> List[Suggestion]: ...


def _match(entries: Iterable[str], prefix: str, limit: int, source: str) -> List[Suggestion]:
    """Substring-match ``prefix`` against ``entries`` (already ranked), skipping exact hits."""
    out: List[Suggestion] = []
    for entry in entries:
        if prefix in entry and entry != prefix:
            out.append(Suggestion(entry, source))
            if len(out) >= limit:
                break
    return out


class SessionProvider:
    """Commands committed (Enter) in this terminal tab, most recent first."""

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque = deque(maxlen=maxlen)

    def add(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            self._lines.remove(line)
        except ValueError:
            pass
        self._lines.appendleft(line)

    def suggestions(self, prefix: str, limit: int) -> List[Suggestion]:
        return _match(self._lines, prefix, limit, "session")


# zsh EXTENDED_HISTORY entries look like ": 1699999999:0;git status".
_ZSH_EXT = re.compile(r"^: \d+:\d+;")


def _recent_first_dedupe(lines: Iterable[str]) -> List[str]:
    """History lines (oldest first) -> unique commands, most recent first."""
    seen: Set[str] = set()
    out: List[str] = []
    for line in reversed(list(lines)):
        line = _ZSH_EXT.sub("", line).strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


class ShellHistoryProvider:
    """Local ~/.bash_history + ~/.zsh_history, most recent first, mtime-cached."""

    def __init__(self, paths: Optional[List[str]] = None) -> None:
        if paths is None:
            home = os.path.expanduser("~")
            paths = [os.path.join(home, ".bash_history"),
                     os.path.join(home, ".zsh_history")]
        self._paths = paths
        self._mtimes: dict = {}
        self._entries: List[str] = []

    def _refresh(self) -> None:
        mtimes = {}
        for path in self._paths:
            try:
                mtimes[path] = os.stat(path).st_mtime
            except OSError:
                continue
        if mtimes == self._mtimes:
            return
        self._mtimes = mtimes
        lines: List[str] = []
        for path in self._paths:
            if path not in mtimes:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines.extend(f.read().splitlines()[-5000:])
            except OSError:
                continue
        self._entries = _recent_first_dedupe(lines)

    def suggestions(self, prefix: str, limit: int) -> List[Suggestion]:
        self._refresh()
        return _match(self._entries, prefix, limit, "history")


def fetch_remote_history(connection, connection_manager=None, config=None,
                         timeout: float = 15) -> Optional[str]:
    """Read the remote host's ~/.bash_history + ~/.zsh_history over the app's
    single SSH path (``build_ssh_connection`` + sshpass, same shape as the
    plugin API's ``run_command``). **Blocking** — call from a worker thread.
    Returns the raw file text, or None on any failure.
    """
    import subprocess
    from .ssh_connection_builder import ConnectionContext, build_ssh_connection
    cleanup = None
    try:
        ctx = ConnectionContext(
            connection=connection, connection_manager=connection_manager,
            config=config, command_type='ssh', native_mode=True,
            remote_command="cat .bash_history .zsh_history 2>/dev/null | tail -c 262144",
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command)
        env = {**os.environ, **(prepared.env or {})}
        if prepared.use_sshpass and prepared.password:
            from .ssh_password_exec import wrap_argv_with_sshpass
            argv, cleanup = wrap_argv_with_sshpass(argv, prepared.password, env=env)
        result = subprocess.run(argv, env=env, capture_output=True, text=True,
                                errors="replace", timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else None
    except Exception:  # noqa: BLE001 — best-effort background fetch
        return None
    finally:
        if cleanup is not None:
            cleanup()


class RemoteHistoryProvider:
    """The remote host's own shell history, fetched once in the background.

    ``fetch`` is a blocking zero-arg callable returning the history file text
    (or None); it runs in a daemon thread on the first suggestion query, so
    keystrokes are never blocked. Results are cached process-wide per
    ``cache_key`` (host), so multiple tabs to one host share a single fetch.
    """
    # ponytail: a failed fetch caches [] for the process lifetime; add a retry
    # timer if stale-empty remote history ever bothers anyone.
    _cache: Dict[str, List[str]] = {}
    _pending: Set[str] = set()
    _lock = threading.Lock()

    def __init__(self, cache_key: str, fetch: Callable[[], Optional[str]]) -> None:
        self._key = cache_key
        self._fetch = fetch

    def _ensure_fetched(self) -> None:
        with self._lock:
            if self._key in self._cache or self._key in self._pending:
                return
            self._pending.add(self._key)
        threading.Thread(target=self._run, daemon=True,
                         name="sshpilot-remote-history").start()

    def _run(self) -> None:
        try:
            text = self._fetch() or ""
        except Exception:  # noqa: BLE001
            text = ""
        entries = _recent_first_dedupe(text.splitlines())
        with self._lock:
            self._cache[self._key] = entries
            self._pending.discard(self._key)

    def suggestions(self, prefix: str, limit: int) -> List[Suggestion]:
        self._ensure_fetched()
        with self._lock:
            entries = list(self._cache.get(self._key, ()))
        return _match(entries, prefix, limit, "remote")


class CommandBlockProvider:
    """Saved Command Blocks snippets, ranked by use_count. ``store`` may be None."""

    def __init__(self, store) -> None:
        self._store = store

    def suggestions(self, prefix: str, limit: int) -> List[Suggestion]:
        if self._store is None:
            return []
        try:
            cmds = sorted(self._store.get_commands(),
                          key=lambda c: c.get("use_count") or 0, reverse=True)
        except Exception:
            return []
        return _match((c.get("command", "") for c in cmds), prefix, limit, "snippet")


class LineTracker:
    """Reconstruct the shell's edit line from the raw keystroke stream.

    ``valid`` drops on anything unmodeled (arrows/escape sequences, Tab
    completion, unknown control chars) and returns only on Enter/Ctrl+C/Ctrl+U —
    the events that provably reset the shell's edit line. Ctrl+L is a no-op
    (clear-screen keeps the current edit buffer).
    """

    def __init__(self) -> None:
        self.line = ""
        self.valid = True

    def feed(self, data: str) -> Optional[str]:
        """Consume raw input; return the committed line when Enter is seen."""
        committed = None
        for ch in data:
            if ch in "\r\n":
                if self.valid and self.line.strip():
                    committed = self.line
                self.line = ""
                self.valid = True
            elif ch in "\x7f\x08":  # Backspace
                self.line = self.line[:-1]
            elif ch in "\x03\x15":  # Ctrl+C / Ctrl+U discard the line
                self.line = ""
                self.valid = True
            elif ch == "\x0c":  # Ctrl+L clear-screen: edit line unchanged
                pass
            elif ch == "\x17":  # Ctrl+W: delete trailing word
                stripped = self.line.rstrip(" ")
                cut = stripped.rfind(" ")
                self.line = stripped[: cut + 1] if cut >= 0 else ""
            elif ch.isprintable():
                self.line += ch
            else:  # ESC (arrows/editing), Tab completion, anything unmodeled
                self.line = ""
                self.valid = False
        return committed


# A prompt like "user@host's password: " means the shell is NOT at an edit line.
_PASSWORD_TAIL = re.compile(r"(password|passphrase|passcode)[^\n]*:\s*$", re.I)


class Autocompleter:
    """Ties tracker + providers together; ``feed`` returns the popup payload.

    Provider order is the source rank (session first, then history, snippets).
    Returns the JS payload dict, ``{"items": []}`` to hide a visible popup, or
    None when nothing changed.
    """

    def __init__(self, providers: List[SuggestionProvider],
                 session: Optional[SessionProvider] = None, limit: int = 8) -> None:
        self.tracker = LineTracker()
        self.providers = list(providers)
        self.session = session
        self.limit = limit
        self._visible = False

    def prefetch(self) -> None:
        """Warm slow providers (remote history) so results exist before the
        first keystroke — the fetch takes an SSH round-trip."""
        for provider in self.providers:
            ensure = getattr(provider, "_ensure_fetched", None)
            if callable(ensure):
                ensure()

    def _hide(self) -> Optional[dict]:
        if self._visible:
            self._visible = False
            return {"prefix": "", "items": []}
        return None

    def suggest(self, prefix: str) -> List[Suggestion]:
        results: List[Suggestion] = []
        seen = set()
        for provider in self.providers:
            for s in provider.suggestions(prefix, self.limit):
                if s.text not in seen:
                    seen.add(s.text)
                    results.append(s)
        # ponytail: prefix+substring only; add fuzzy scoring if users ask.
        # Stable sort keeps provider (source-rank) order within each group.
        results.sort(key=lambda s: 0 if s.text.startswith(prefix) else 1)
        return results[: self.limit]

    def feed(self, data: str, output_tail: str = "") -> Optional[dict]:
        committed = self.tracker.feed(data)
        if committed and self.session is not None:
            self.session.add(committed)
        prefix = self.tracker.line
        if (not self.tracker.valid or len(prefix) < 2
                or _PASSWORD_TAIL.search(output_tail or "")):
            return self._hide()
        items = self.suggest(prefix)
        if not items:
            return self._hide()
        self._visible = True
        return {
            "prefix": prefix,
            "items": [
                {
                    "text": s.text,
                    # Exact bytes JS must send to complete: append the tail for
                    # prefix matches; erase-and-retype for substring matches.
                    "suffix": (s.text[len(prefix):] if s.text.startswith(prefix)
                               else "\x7f" * len(prefix) + s.text),
                    "source": s.source,
                }
                for s in items
            ],
        }

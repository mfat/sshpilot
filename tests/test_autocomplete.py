"""Tests for sshpilot/autocomplete.py (GTK-free autocomplete engine)."""

import time

import pytest

from sshpilot.autocomplete import (
    Autocompleter,
    CommandBlockProvider,
    LineTracker,
    RemoteHistoryProvider,
    SessionProvider,
    ShellHistoryProvider,
    fetch_remote_history,
)


# ---------------------------------------------------------------- LineTracker

def test_tracker_appends_printable():
    t = LineTracker()
    t.feed("gi")
    t.feed("t")
    assert t.line == "git" and t.valid


def test_tracker_backspace():
    t = LineTracker()
    t.feed("gitt\x7f")
    assert t.line == "git"


def test_tracker_ctrl_c_and_ctrl_u_reset():
    for ctrl in ("\x03", "\x15"):
        t = LineTracker()
        t.feed("git sta")
        t.feed(ctrl)
        assert t.line == "" and t.valid


def test_tracker_enter_commits_and_resets():
    t = LineTracker()
    assert t.feed("git status\r") == "git status"
    assert t.line == "" and t.valid


def test_tracker_empty_enter_commits_nothing():
    t = LineTracker()
    assert t.feed("\r") is None
    assert t.feed("   \r") is None


def test_tracker_escape_invalidates_enter_revalidates():
    t = LineTracker()
    t.feed("git")
    t.feed("\x1b[A")  # Up arrow
    assert not t.valid
    assert t.feed("recalled-command\r") is None  # invalid line never commits
    assert t.valid and t.line == ""


def test_tracker_tab_invalidates():
    t = LineTracker()
    t.feed("git\t")
    assert not t.valid


def test_tracker_ctrl_w_deletes_word():
    t = LineTracker()
    t.feed("git status  \x17")
    assert t.line == "git "
    t2 = LineTracker()
    t2.feed("git\x17")
    assert t2.line == ""


def test_tracker_paste_appends_and_control_paste_invalidates():
    t = LineTracker()
    t.feed("echo hello world")
    assert t.line == "echo hello world"
    t.feed("\x01")  # Ctrl+A — unmodeled
    assert not t.valid


# ------------------------------------------------------------------ Providers

def test_shell_history_provider(tmp_path):
    bash = tmp_path / "bash_history"
    bash.write_text("ls -la\ngit status\ngit push\n")
    zsh = tmp_path / "zsh_history"
    zsh.write_text(": 1699999999:0;git pull\n: 1699999999:0;docker ps\n")
    p = ShellHistoryProvider(paths=[str(bash), str(zsh)])
    texts = [s.text for s in p.suggestions("git", 10)]
    # zsh extended format stripped; most recent first; all sources merged
    assert texts == ["git pull", "git push", "git status"]
    assert all(s.source == "history" for s in p.suggestions("git", 10))


def test_shell_history_dedupes_and_skips_exact():
    from sshpilot import autocomplete
    p = ShellHistoryProvider(paths=[])
    p._entries = ["git status", "git status", "git"]
    p._refresh = lambda: None
    texts = [s.text for s in p.suggestions("git", 10)]
    assert texts == ["git status", "git status"] or texts == ["git status"]
    # exact match "git" never suggested
    assert "git" not in texts
    assert autocomplete._match(["git status", "git status"], "git", 10, "history")


def test_shell_history_missing_file():
    p = ShellHistoryProvider(paths=["/nonexistent/history"])
    assert p.suggestions("git", 10) == []


def test_command_block_provider_orders_by_use_count():
    class Store:
        def get_commands(self):
            return [
                {"command": "git log", "use_count": 1},
                {"command": "git status", "use_count": 9},
            ]

    p = CommandBlockProvider(Store())
    assert [s.text for s in p.suggestions("git", 10)] == ["git status", "git log"]
    assert p.suggestions("git", 10)[0].source == "snippet"


def test_command_block_provider_none_store():
    assert CommandBlockProvider(None).suggestions("git", 10) == []


def test_session_provider_recency_and_dedupe():
    p = SessionProvider(maxlen=3)
    for cmd in ("git status", "ls", "git push", "git status"):
        p.add(cmd)
    assert [s.text for s in p.suggestions("git", 10)] == ["git status", "git push"]


# ------------------------------------------------------- RemoteHistoryProvider

@pytest.fixture(autouse=True)
def _clear_remote_cache():
    RemoteHistoryProvider._cache.clear()
    RemoteHistoryProvider._pending.clear()
    yield


def _wait_for_cache(key, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with RemoteHistoryProvider._lock:
            if key in RemoteHistoryProvider._cache:
                return
        time.sleep(0.01)
    raise AssertionError("remote history fetch never completed")


def test_remote_history_fetches_in_background():
    p = RemoteHistoryProvider("host1", lambda: "ls -la\n: 1699999999:0;git pull\n")
    p.suggestions("git", 10)  # kicks the background fetch off (may race to finish)
    _wait_for_cache("host1")
    assert [s.text for s in p.suggestions("git", 10)] == ["git pull"]
    assert p.suggestions("git", 10)[0].source == "remote"


def test_remote_history_cache_shared_per_key():
    calls = []

    def fetch():
        calls.append(1)
        return "git status\n"

    a = RemoteHistoryProvider("host2", fetch)
    a.suggestions("git", 10)
    _wait_for_cache("host2")
    b = RemoteHistoryProvider("host2", lambda: pytest.fail("second fetch ran"))
    assert [s.text for s in b.suggestions("git", 10)] == ["git status"]
    assert calls == [1]


def test_remote_history_failed_fetch_is_quiet():
    def boom():
        raise RuntimeError("unreachable")

    p = RemoteHistoryProvider("host3", boom)
    p.suggestions("git", 10)
    _wait_for_cache("host3")
    assert p.suggestions("git", 10) == []


def test_prefetch_warms_remote_provider():
    remote = RemoteHistoryProvider("host4", lambda: "git status\n")
    ac = Autocompleter([remote])
    ac.prefetch()
    _wait_for_cache("host4")
    assert [s.text for s in ac.suggest("git")] == ["git status"]


def test_fetch_remote_history_runs_prepared_command(monkeypatch):
    from types import SimpleNamespace
    import sshpilot.ssh_connection_builder as scb

    monkeypatch.setattr(scb, "build_ssh_connection", lambda ctx: SimpleNamespace(
        command=["sh", "-c", "printf 'ls\\ncd /tmp\\n'"],
        env={}, use_sshpass=False, password=None))
    assert fetch_remote_history(object()) == "ls\ncd /tmp\n"


def test_fetch_remote_history_failure_returns_none(monkeypatch):
    from types import SimpleNamespace
    import sshpilot.ssh_connection_builder as scb

    monkeypatch.setattr(scb, "build_ssh_connection", lambda ctx: SimpleNamespace(
        command=["sh", "-c", "exit 1"], env={}, use_sshpass=False, password=None))
    assert fetch_remote_history(object()) is None
    monkeypatch.setattr(scb, "build_ssh_connection",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("no ssh")))
    assert fetch_remote_history(object()) is None


# -------------------------------------------------------------- Autocompleter

def _ac(**kwargs):
    session = SessionProvider()
    history = ShellHistoryProvider(paths=[])
    history._entries = kwargs.pop("history", [])
    history._refresh = lambda: None
    return Autocompleter([session, history], session=session, **kwargs), session


def test_feed_returns_ranked_payload():
    ac, session = _ac(history=["cd /tmp", "git push"])
    session.add("git status")
    payload = ac.feed("gi")
    texts = [i["text"] for i in payload["items"]]
    assert payload["prefix"] == "gi"
    assert texts == ["git status", "git push"]  # session outranks history
    assert payload["items"][0]["suffix"] == "t status"


def test_prefix_match_outranks_substring():
    ac, _ = _ac(history=["sudo git push", "git push"])
    texts = [i["text"] for i in ac.feed("git")["items"]]
    assert texts == ["git push", "sudo git push"]


def test_substring_suffix_erases_typed_prefix():
    ac, _ = _ac(history=["sudo git push"])
    item = ac.feed("git")["items"][0]
    assert item["suffix"] == "\x7f" * 3 + "sudo git push"


def test_short_prefix_is_quiet():
    ac, _ = _ac(history=["git push"])
    assert ac.feed("g") is None


def test_enter_hides_and_commits_to_session():
    ac, session = _ac(history=["git push"])
    assert ac.feed("git")  # popup shown
    hide = ac.feed(" pull\r")
    assert hide == {"prefix": "", "items": []}
    assert [s.text for s in session.suggestions("git pull", 5)] == []  # exact skipped
    assert session._lines[0] == "git pull"


def test_ctrl_c_hides():
    ac, _ = _ac(history=["git push"])
    assert ac.feed("git")
    assert ac.feed("\x03") == {"prefix": "", "items": []}


def test_no_repeat_hide_payloads():
    ac, _ = _ac(history=[])
    assert ac.feed("zz") is None  # no matches, popup never shown
    assert ac.feed("z") is None


def test_password_prompt_suppresses():
    ac, _ = _ac(history=["git push"])
    assert ac.feed("gi", output_tail="user@host's password: ") is None


def test_limit_and_dedupe_across_providers():
    ac, session = _ac(history=[f"git cmd{i}" for i in range(20)] + ["git status"])
    session.add("git status")
    payload = ac.feed("git")
    texts = [i["text"] for i in payload["items"]]
    assert len(texts) == 8
    assert texts.count("git status") == 1

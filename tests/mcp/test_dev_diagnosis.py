"""Phase 2: diagnose_bug ranks evidence, admits uncertainty, keeps no secrets."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev.engine import DevIntelligence, EngineError
from sshpilot.mcp.dev.engine.diagnosis import parse_traceback, redact

PKG = "src/sshpilot/demo"
STORE = '''"""A tiny store with a bug."""


class Store:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items[key]


def lookup(store, key):
    return store.get(key)


def refresh():
    return 1
'''
OTHER = '''
class Panel:
    def refresh(self):
        return 2
'''
TEST = '''from sshpilot.demo.store import Store, lookup


def test_lookup_missing():
    assert lookup(Store(), "missing") is None
'''


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write(root, "src/sshpilot/__init__.py", "")
    _write(root, f"{PKG}/__init__.py", "")
    _write(root, f"{PKG}/store.py", STORE)
    _write(root, f"{PKG}/panel.py", OTHER)
    _write(root, "tests/demo/test_store.py", TEST)
    return root


@pytest.fixture()
def service(repo, tmp_path):
    svc = DevIntelligence(RepoScope(repo), index_dir=tmp_path / "index")
    yield svc
    svc.close()


def _real_traceback(repo: Path, secret: str = "") -> str:
    """Run the buggy code for real and capture its traceback."""
    code = f"from sshpilot.demo.store import Store, lookup; lookup(Store(), 'k {secret}')"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
        cwd=repo,
    )
    assert proc.returncode != 0
    return proc.stderr


def test_parse_traceback_frames_and_exception():
    frames, exception = parse_traceback(
        'Traceback (most recent call last):\n  File "/x/src/sshpilot/a.py", line 3, in f\n    g()\nValueError: bad\n'
    )
    assert [(f["file"], f["line"], f["func"]) for f in frames] == [("/x/src/sshpilot/a.py", 3, "f")]
    assert exception == {"type": "ValueError", "message": "bad"}


def test_real_traceback_resolves_to_the_failing_symbol(service, repo):
    result = service.diagnose_bug("lookup crashes", traceback=_real_traceback(repo))
    top = result["candidates"][0]
    assert top["id"] == f"{PKG}/store.py::Store.get"
    assert top["confidence"] == "high"
    assert top["evidence"][0]["kind"] == "traceback-frame"
    assert top["evidence"][0]["match"] == "exact-line"
    ids = [c["id"] for c in result["candidates"]]
    assert ids.index(f"{PKG}/store.py::lookup") == 1  # next frame outward
    assert result["observations"]["exception"]["type"] == "KeyError"
    assert result["confidence"] == "high"


def test_ambiguous_symptom_yields_labelled_candidates(service):
    result = service.diagnose_bug("refresh() returns the wrong value")
    ids = {c["id"] for c in result["candidates"]}
    assert {f"{PKG}/store.py::refresh", f"{PKG}/panel.py::Panel.refresh"} <= ids
    assert {c["confidence"] for c in result["candidates"]} == {"low"}
    assert all(e["kind"] == "identifier-mention" and e["matches"] == 2 for c in result["candidates"] for e in c["evidence"])
    assert any("tied" in u for u in result["uncertainties"])
    assert result["confidence"] == "low"


def test_unrelated_trace_is_low_confidence(service):
    trace = (
        'Traceback (most recent call last):\n'
        '  File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode\n'
        '    raise JSONDecodeError("Expecting value", s, err.value) from None\n'
        'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n'
    )
    result = service.diagnose_bug("something broke", traceback=trace)
    assert result["candidates"] == []
    assert result["confidence"] == "none"
    assert any("outside the checkout" in u for u in result["uncertainties"])
    assert any("no evidence" in u for u in result["uncertainties"])


def test_failing_test_id_points_at_test_and_callees(service):
    result = service.diagnose_bug("", test_name="tests/demo/test_store.py::test_lookup_missing")
    ids = [c["id"] for c in result["candidates"]]
    assert "tests/demo/test_store.py::test_lookup_missing" in ids
    assert f"{PKG}/store.py::lookup" in ids
    assert any(step.startswith("reproduce: python -m pytest -q tests/demo/test_store.py::test_lookup_missing") for step in result["next_steps"])


def test_stale_index_is_refreshed_before_mapping_frames(service, repo):
    service.index.refresh()
    _write(repo, f"{PKG}/store.py", "\n\n\n\n\n" + STORE)  # every line moves by 5
    result = service.diagnose_bug("crash", traceback=_real_traceback(repo))
    top = result["candidates"][0]
    assert top["id"] == f"{PKG}/store.py::Store.get"
    assert top["evidence"][0]["match"] == "exact-line"
    assert top["lines"][0] == 13


def test_drifted_line_is_flagged(service):
    trace = f'Traceback (most recent call last):\n  File "{PKG}/store.py", line 2, in lookup\nKeyError: x\n'
    result = service.diagnose_bug("crash", traceback=trace)
    top = result["candidates"][0]
    assert top["id"] == f"{PKG}/store.py::lookup"
    assert top["evidence"][0]["match"] == "function-name (line drifted)"
    assert any("another revision" in u for u in result["uncertainties"])


def test_native_gtk_error_gives_next_steps_not_a_diagnosis(service):
    result = service.diagnose_bug("(sshpilot:1234): Gtk-CRITICAL **: gtk_widget_get_parent: assertion 'GTK_IS_WIDGET (widget)' failed")
    assert result["observations"]["native_errors"] == ["gtk-critical"]
    assert any("--fatal-warnings" in step for step in result["next_steps"])
    assert any("native" in u for u in result["uncertainties"])


def test_changed_files_add_evidence(service):
    result = service.diagnose_bug("`lookup` is wrong", changed_paths=[f"{PKG}/store.py"])
    entry = next(c for c in result["candidates"] if c["id"] == f"{PKG}/store.py::lookup")
    assert [e["kind"] for e in entry["evidence"]] == ["identifier-mention", "changed-file"]
    assert entry["score"] == 30 + 20


def test_plain_english_words_are_only_weak_leads(service):
    result = service.diagnose_bug("the lookup does not refresh")
    assert result["candidates"], "keyword leads are offered"
    assert {c["confidence"] for c in result["candidates"]} == {"low"}
    assert {e["kind"] for c in result["candidates"] for e in c["evidence"]} == {"keyword-overlap"}
    assert all(c["score"] <= 25 for c in result["candidates"])
    assert result["confidence"] == "low"
    assert any("weak evidence" in u for u in result["uncertainties"])


def test_output_is_bounded(service, repo):
    result = service.diagnose_bug("crash", traceback=_real_traceback(repo), budget=800)
    assert result["budget"]["used_bytes"] <= 800 or result["budget"]["over_budget"]


def test_secrets_in_traceback_are_neither_returned_nor_persisted(service, repo):
    secret = "password=Sup3rSecretValue123"
    trace = _real_traceback(repo, secret=secret)
    assert "Sup3rSecretValue123" in trace
    result = service.diagnose_bug(f"login failed token: abcdEFGH1234 {secret}", traceback=trace, budget=20000)
    import json

    assert "Sup3rSecretValue123" not in json.dumps(result)
    assert "abcdEFGH1234" not in json.dumps(result)
    assert result["persisted_input"] is False
    raw = b"".join(path.read_bytes() for path in service.index.path.parent.iterdir())
    assert b"Sup3rSecretValue123" not in raw


def test_redact_private_key_block():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    assert "AAAA" not in redact(text)


def test_input_validation(service):
    with pytest.raises(EngineError):
        service.diagnose_bug("")
    with pytest.raises(EngineError):
        service.diagnose_bug("x" * 20000)
    with pytest.raises(EngineError):
        service.diagnose_bug("x", max_items=0)


def test_quoted_message_is_found_literally(service, repo):
    _write(repo, f"{PKG}/errors.py", 'def report():\n    raise RuntimeError("the vault could not be unlocked today")\n')
    result = service.diagnose_bug('I get "the vault could not be unlocked today" on start')
    top = result["candidates"][0]
    assert top["id"] == f"{PKG}/errors.py::report"
    assert top["evidence"][0]["kind"] == "message-literal"
    assert top["confidence"] == "low"  # a message match alone is a lead, not proof

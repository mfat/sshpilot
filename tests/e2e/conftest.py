"""Fixtures for the Dogtail/AT-SPI end-to-end suite.

These tests need a real graphical session with an accessibility bus, so they
are opt-in twice over: the ``e2e`` marker keeps them out of the default
``pytest`` run, and ``SSHPILOT_E2E_TESTS=1`` keeps an explicit ``-m e2e`` from
erroring on a machine that has no display.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    AppSession,
    Sandbox,
    accessibility_bus_available,
    import_dogtail,
)

ARTIFACTS_ROOT = Path(
    os.environ.get("SSHPILOT_E2E_ARTIFACTS")
    or Path(__file__).resolve().parents[2] / "build" / "e2e-artifacts"
)


@pytest.fixture(scope="session")
def dogtail_tree():
    if os.environ.get("SSHPILOT_E2E_TESTS") != "1":
        pytest.skip(
            "GUI accessibility tests are opt-in: run with SSHPILOT_E2E_TESTS=1 "
            "on a graphical session (see tests/e2e/README.md)"
        )
    pytest.importorskip("dogtail", reason="python3-dogtail is not installed")
    pytest.importorskip("pyatspi", reason="python3-pyatspi is not installed")
    if not accessibility_bus_available():
        pytest.skip("no AT-SPI accessibility bus in this session")
    return import_dogtail(ARTIFACTS_ROOT / "dogtail")


@pytest.fixture
def launch_app(dogtail_tree, request):
    """Launch an isolated SSH Pilot, optionally with extra environment.

    Most tests want the plain :func:`app` fixture. This exists for the one that
    needs a different environment than the pinned test defaults (see
    ``harness.TEST_LOCALE``).
    """
    sessions: list[AppSession] = []

    def _launch(*, env_overrides=None) -> AppSession:
        app_id = f"io.github.mfat.sshpilot.e2e{uuid.uuid4().hex[:8]}"
        sandbox = Sandbox.create(app_id, env_overrides=env_overrides)
        session = AppSession(sandbox, dogtail_tree)
        try:
            session.start()
        except Exception:
            session.stop()
            sandbox.cleanup()
            raise
        sessions.append(session)
        return session

    yield _launch

    failed = getattr(request.node, "_sshpilot_e2e_failed", False)
    for session in sessions:
        if failed:
            artifacts = ARTIFACTS_ROOT / request.node.name
            try:
                print(f"\n--- e2e diagnostics for {request.node.name} ---")
                print(session.failure_report(artifacts))
            except Exception as exc:
                print(f"failed to collect diagnostics: {exc!r}")
        session.stop()
        if failed:
            print(f"sandbox kept for inspection: {session.sandbox.root}")
        else:
            session.sandbox.cleanup()


@pytest.fixture
def app(launch_app):
    """A freshly launched, fully isolated SSH Pilot for one test."""
    return launch_app()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        item._sshpilot_e2e_failed = True

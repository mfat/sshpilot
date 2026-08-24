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
def app(dogtail_tree, request):
    """A freshly launched, fully isolated SSH Pilot for one test."""
    app_id = f"io.github.mfat.sshpilot.e2e{uuid.uuid4().hex[:8]}"
    sandbox = Sandbox.create(app_id)
    session = AppSession(sandbox, dogtail_tree)
    try:
        session.start()
    except Exception:
        session.stop()
        sandbox.cleanup()
        raise

    yield session

    failed = getattr(request.node, "_sshpilot_e2e_failed", False)
    if failed:
        artifacts = ARTIFACTS_ROOT / request.node.name
        try:
            print(f"\n--- e2e diagnostics for {request.node.name} ---")
            print(session.failure_report(artifacts))
        except Exception as exc:  # diagnostics must never mask the real failure
            print(f"failed to collect diagnostics: {exc!r}")
    session.stop()
    if not failed:
        sandbox.cleanup()
    else:
        print(f"sandbox kept for inspection: {sandbox.root}")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        item._sshpilot_e2e_failed = True

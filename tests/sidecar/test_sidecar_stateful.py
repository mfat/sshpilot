"""Entry points for the sidecar identity stateful stress suite.

Tiers (see AGENTS.md "Testing" and pytest.ini for the project's marker
conventions):

* Normal PR suite (no marker; runs in the default headless suite): modest
  example/step budget, always on.
* ``pytest -m stress`` (opt-in, like the existing ``integration``/``gui``
  markers): a heavier example/step budget.
* ``SSHPILOT_SIDECAR_NIGHTLY=1 pytest -m stress``: the same stress test, but
  with the nightly-scale budget (thousands of examples, 50-100 mutations per
  example, including the ``ssh -G`` differential oracle and restart/mode
  boundaries already wired into every rule sequence).
"""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.stateful import run_state_machine_as_test

from .state_machine import SidecarIdentityMachine

settings.register_profile(
    "sidecar_normal",
    max_examples=100,
    stateful_step_count=10,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
)
settings.register_profile(
    "sidecar_stress",
    max_examples=500,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
)
settings.register_profile(
    "sidecar_nightly",
    max_examples=2000,
    stateful_step_count=80,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
)


@pytest.mark.property
def test_sidecar_identity_reconciliation_normal():
    """Modest property search. Opt-in: ``pytest -m property``.

    A search, not a fixed case -- it draws new examples every run, so it costs
    tens of seconds each time and can only ever say "found nothing today".
    Anything it does find is promoted to a fixture in test_regressions.py,
    which is what the ordinary suite relies on.
    """

    run_state_machine_as_test(
        SidecarIdentityMachine, settings=settings.get_profile("sidecar_normal")
    )


@pytest.mark.stress
def test_sidecar_identity_reconciliation_stress():
    """Heavier property suite: opt in with ``pytest -m stress``.

    Set ``SSHPILOT_SIDECAR_NIGHTLY=1`` to run the nightly-scale budget
    instead of the mid-tier one.
    """

    profile = "sidecar_nightly" if os.environ.get("SSHPILOT_SIDECAR_NIGHTLY") else "sidecar_stress"
    run_state_machine_as_test(SidecarIdentityMachine, settings=settings.get_profile(profile))

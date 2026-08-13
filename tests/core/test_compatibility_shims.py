"""Compatibility shim smoke tests."""
from __future__ import annotations


def test_askpass_classify_prompt_shim_matches_core():
    from sshpilot.askpass_utils import classify_prompt as shim
    from sshpilot.core.interaction import classify_prompt as core

    text = "user@host's password:"
    assert shim(text) == core(text).value


def test_ssh_builder_reexports_process_spec():
    from sshpilot.ssh_connection_builder import ProcessSpec, build_ssh_process_spec
    from sshpilot.core.ssh import SSHLaunchRequest

    spec = build_ssh_process_spec(SSHLaunchRequest(destination="demo"))
    assert isinstance(spec, ProcessSpec)
    assert spec.argv[0] == "ssh"


def test_connection_manager_exposes_domain_attribute():
    # Importing ConnectionManager requires GI in normal app use; only check
    # that the core service is independently usable (adapter tested via GTK).
    from sshpilot.core.connections import ConnectionService

    assert ConnectionService(autosave=False) is not None

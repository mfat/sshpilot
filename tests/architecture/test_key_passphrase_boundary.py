"""Architecture guards for protected SSH-key passphrases."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from sshpilot.api.models import SessionId
from sshpilot.api.models.connections import StoreKeyPassphraseRequest
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    VerifyKeyPassphraseRequest,
)
from sshpilot.api.transport.codec import (
    generate_key_request_to_wire,
    store_key_passphrase_request_to_wire,
    verify_key_passphrase_request_to_wire,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "sshpilot"
SENTINEL = "KEY_PASSPHRASE_SENTINEL_8F1C29"


def _tree(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text(encoding="utf-8"))


def test_public_key_requests_have_no_passphrase_field_or_wire_key():
    assert "passphrase" not in {field.name for field in fields(GenerateKeyRequest)}
    assert "passphrase" not in {
        field.name for field in fields(StoreKeyPassphraseRequest)
    }
    generate_wire = generate_key_request_to_wire(
        GenerateKeyRequest(name="id_ed25519")
    )
    verify_wire = verify_key_passphrase_request_to_wire(
        VerifyKeyPassphraseRequest(
            key_path="/home/user/.ssh/id_ed25519",
            interaction_scope_id=SessionId("key-operation-verify-1"),
        )
    )
    store_wire = store_key_passphrase_request_to_wire(
        StoreKeyPassphraseRequest(
            key_path="/home/user/.ssh/id_ed25519",
            interaction_scope_id=SessionId("key-operation-store-1"),
        )
    )
    assert "passphrase" not in generate_wire
    assert "passphrase" not in verify_wire
    assert "passphrase" not in store_wire
    assert SENTINEL not in repr(generate_wire)
    assert SENTINEL not in repr(verify_wire)
    assert SENTINEL not in repr(store_wire)


def test_frontend_does_not_launch_ssh_keygen():
    violations = []
    excluded_roots = {"api", "core", "daemon", "platform"}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        if relative.parts[0] in excluded_roots:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if any(
                isinstance(item, ast.Constant) and item.value == "ssh-keygen"
                for item in ast.walk(call)
            ):
                violations.append(f"{relative}:{call.lineno}")
    assert not violations, f"frontend launches ssh-keygen: {violations}"


def test_keygen_n_and_p_values_cannot_be_dynamic_secrets():
    violations = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if '"ssh-keygen"' not in source and "'ssh-keygen'" not in source:
            continue
        tree = ast.parse(source)
        for sequence in (
            node for node in ast.walk(tree) if isinstance(node, (ast.List, ast.Tuple))
        ):
            elements = sequence.elts
            for index, item in enumerate(elements[:-1]):
                if not (
                    isinstance(item, ast.Constant)
                    and item.value in {"-N", "-P"}
                ):
                    continue
                following = elements[index + 1]
                if not (
                    isinstance(following, ast.Constant)
                    and following.value == ""
                ):
                    violations.append(
                        f"{path.relative_to(SRC)}:{getattr(item, 'lineno', 0)}"
                    )
    assert not violations, f"dynamic ssh-keygen -N/-P values: {violations}"


def test_key_subprocess_boundary_does_not_log_argv():
    violations = []
    for relative in ("core/keys/service.py", "daemon/key_service.py"):
        for call in (
            node for node in ast.walk(_tree(relative)) if isinstance(node, ast.Call)
        ):
            function = call.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr in {"debug", "info", "warning", "error", "exception"}
            ):
                continue
            names = {
                node.id
                for argument in call.args
                for node in ast.walk(argument)
                if isinstance(node, ast.Name)
            }
            if names & {"cmd", "argv", "spec", "request", "secret", "passphrase"}:
                violations.append(f"{relative}:{call.lineno}")
    assert not violations, f"key subprocess details logged: {violations}"


def test_protected_key_paths_reuse_broker_and_secret_frames():
    daemon_source = (SRC / "daemon/key_service.py").read_text(encoding="utf-8")
    controller_source = (SRC / "gtk/key_controller.py").read_text(encoding="utf-8")
    assert "prepare_operation_launch" in daemon_source
    assert "InteractionBroker" in daemon_source
    assert "send_interaction_secret" in controller_source
    assert "bytearray" in controller_source

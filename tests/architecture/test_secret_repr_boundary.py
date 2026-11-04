"""Credential-carrying dataclasses must not render the credential in repr().

A traceback, a debugger view or a stray ``logger.debug("%r", obj)`` would
otherwise print the secret verbatim (issue #1362). The value stays reachable
through attribute access for the code that needs it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sshpilot.api.models.interactions import RememberPolicy
from sshpilot.api.transport.endpoint import TcpEndpoint
from sshpilot.core.interaction.policy import InteractionOutcome, InteractionResponse
from sshpilot.credential_model import Credential
from sshpilot.daemon.dispatch import SecretResponseResult
from sshpilot.daemon.interaction_broker import InteractionResult, _PendingRemember
from sshpilot.daemon.privileged_file_service import _PromptResult
from sshpilot.ssh_connection_builder import NativeAuth, SSHConnectionCommand

SRC = Path(__file__).resolve().parents[2] / "src" / "sshpilot"
SENTINEL = "REPR_SECRET_SENTINEL_5D0A"

_SECRET_NAME = re.compile(r"(^|_)(secret|password|passphrase|token)$")
# Annotations that cannot hold a credential value.
_SAFE_ANNOTATIONS = {"bool", "int", "Optional[int]"}
# Fields whose name matches but whose value is not a credential.
_NOT_A_SECRET = {
    ("api/models/connections.py", "SplitConnectionRequest", "original_host_token"),
}


def _instances():
    secret_bytes = bytearray(SENTINEL.encode())
    policy = RememberPolicy.DO_NOT_STORE
    return [
        InteractionResponse(outcome=InteractionOutcome.ACCEPTED, secret=SENTINEL),
        SecretResponseResult(secret=bytearray(secret_bytes)),
        InteractionResult(
            decision=None, remember_policy=policy, secret=bytearray(secret_bytes)
        ),
        _PendingRemember(
            interaction_type=None, key="k", secret=bytearray(secret_bytes)
        ),
        _PromptResult(password=SENTINEL, remember_policy=policy),
        Credential(id="c", type="password", secret=SENTINEL),
        SSHConnectionCommand(command=["ssh"], env={}, password=SENTINEL),
        NativeAuth(env={}, extra_opts=[], password=SENTINEL),
        TcpEndpoint(port=1, token=SENTINEL),
    ]


def test_secret_bearing_objects_do_not_render_the_secret():
    for obj in _instances():
        assert SENTINEL not in repr(obj), type(obj).__name__
        assert SENTINEL not in str(obj), type(obj).__name__


def test_secret_remains_readable_by_attribute():
    response = InteractionResponse(
        outcome=InteractionOutcome.ACCEPTED, secret=SENTINEL
    )
    assert response.secret == SENTINEL
    assert _PromptResult(
        password=SENTINEL, remember_policy=RememberPolicy.DO_NOT_STORE
    ).password == SENTINEL


def _is_dataclass(node: ast.ClassDef) -> bool:
    return any("dataclass" in ast.unparse(d) for d in node.decorator_list)


def _has_custom_repr(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, ast.FunctionDef) and item.name == "__repr__"
        for item in node.body
    )


def _repr_disabled(value: ast.expr | None) -> bool:
    if not isinstance(value, ast.Call):
        return False
    return any(
        kw.arg == "repr"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in value.keywords
    )


def test_no_dataclass_renders_a_secret_named_field():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
                continue
            if _has_custom_repr(node):
                continue
            for item in node.body:
                if not (
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                ):
                    continue
                name = item.target.id
                if not _SECRET_NAME.search(name):
                    continue
                if ast.unparse(item.annotation) in _SAFE_ANNOTATIONS:
                    continue
                if (relative, node.name, name) in _NOT_A_SECRET:
                    continue
                if not _repr_disabled(item.value):
                    offenders.append(f"{relative}:{item.lineno} {node.name}.{name}")
    assert not offenders, (
        "credential fields must use field(repr=False): " + ", ".join(offenders)
    )

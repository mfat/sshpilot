"""Kubernetes exec protocol plugin.

Opens an interactive shell inside a pod — ``kubectl exec -it <pod> [-c
<container>] -- <shell>`` — in the VTE, with optional context/namespace. Pure
terminal seam, no in-app auth: kubectl uses the user's kubeconfig.
"""

from __future__ import annotations

import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List

from .._shell import command_split_diagnostic, split_command
from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)


class KubernetesProtocolBackend(ProtocolBackend):
    protocol_id = "k8s"
    display_name = "Kubernetes"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="pod", label=_("Pod"), kind="text", required=True,
                      placeholder=_("pod name")),
            FieldSpec(key="container", label=_("Container"), kind="text",
                      placeholder=_("(default container)")),
            FieldSpec(key="namespace", label=_("Namespace"), kind="text",
                      placeholder="default"),
            FieldSpec(key="kube_context", label=_("Context"), kind="text",
                      placeholder=_("(current context)"), group="advanced"),
            FieldSpec(key="kubeconfig", label=_("Kubeconfig"), kind="file",
                      group="advanced"),
            FieldSpec(key="command", label=_("Command"), kind="text", default="sh",
                      placeholder="sh"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("pod") or "").strip():
            errors.append(_("A pod name is required."))
        diagnostic = command_split_diagnostic(data.get("command"))
        if diagnostic:
            errors.append(
                _("{field} could not be parsed: {diagnostic}.").format(
                    field=_("Command"), diagnostic=diagnostic
                )
            )
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        pod = (data.get("pod") or "").strip()
        if not pod:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.POD_REQUIRED,
                "No pod configured for this connection.",
            )
        from .._flatpak import resolve_host_binary  # noqa: PLC0415
        kubectl_argv = resolve_host_binary("kubectl")
        if kubectl_argv is None:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.KUBECTL_UNAVAILABLE,
                "The 'kubectl' program is not installed. Install it to use "
                "Kubernetes connections.",
                parameters={"program": "kubectl"},
            )

        command = (data.get("command") or "sh").strip() or "sh"
        argv = list(kubectl_argv)
        kubeconfig = (data.get("kubeconfig") or "").strip()
        if kubeconfig:
            argv += ["--kubeconfig", os.path.expanduser(kubeconfig)]
        context = (data.get("kube_context") or "").strip()
        if context:
            argv += ["--context", context]
        namespace = (data.get("namespace") or "").strip()
        if namespace:
            argv += ["-n", namespace]
        argv += ["exec", "-it", pod]
        container = (data.get("container") or "").strip()
        if container:
            argv += ["-c", container]
        argv += ["--", *split_command(command, "command")]
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(KubernetesProtocolBackend())

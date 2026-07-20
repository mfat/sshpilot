"""Kubernetes exec protocol plugin.

Opens an interactive shell inside a pod — ``kubectl exec -it <pod> [-c
<container>] -- <shell>`` — in the VTE, with optional context/namespace. Pure
terminal seam, no in-app auth: kubectl uses the user's kubeconfig.
"""

from __future__ import annotations

import os
import shlex
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List

from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
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
                      placeholder="pod name"),
            FieldSpec(key="container", label=_("Container"), kind="text",
                      placeholder="(default container)"),
            FieldSpec(key="namespace", label=_("Namespace"), kind="text",
                      placeholder="default"),
            FieldSpec(key="kube_context", label=_("Context"), kind="text",
                      placeholder="(current context)", group="advanced"),
            FieldSpec(key="kubeconfig", label=_("Kubeconfig"), kind="file",
                      group="advanced"),
            FieldSpec(key="command", label=_("Command"), kind="text", default="sh",
                      placeholder="sh"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        if not (data.get("pod") or "").strip():
            return ["A pod name is required."]
        return []

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        pod = (data.get("pod") or "").strip()
        if not pod:
            raise ProtocolError("No pod configured for this connection.")
        from .._flatpak import resolve_host_binary  # noqa: PLC0415
        kubectl_argv = resolve_host_binary("kubectl")
        if kubectl_argv is None:
            raise ProtocolError(
                "The 'kubectl' program is not installed. Install it to use "
                "Kubernetes connections.")

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
        argv += ["--", *shlex.split(command)]
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(KubernetesProtocolBackend())

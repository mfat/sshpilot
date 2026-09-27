"""Kubernetes exec protocol plugin.

Opens an interactive shell inside a pod — ``kubectl exec -it <pod> [-c
<container>] -- <shell>`` — in the VTE, with optional context/namespace. Pure
terminal seam, no in-app auth: kubectl uses the user's kubeconfig.
"""

from __future__ import annotations

import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List, Tuple

from .._shell import command_split_diagnostic, split_command
from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from .._summary import run_lines
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)


def _kubectl(values: Dict[str, Any], *, namespace: bool = True) -> List[str]:
    """``kubectl`` with the form's kubeconfig, context and namespace."""
    argv = ["kubectl"]
    kubeconfig = str(values.get("kubeconfig") or "").strip()
    if kubeconfig:
        argv += ["--kubeconfig", os.path.expanduser(kubeconfig)]
    context = str(values.get("kube_context") or "").strip()
    if context:
        argv += ["--context", context]
    ns = str(values.get("namespace") or "").strip()
    if namespace and ns:
        argv += ["-n", ns]
    return argv


# ``kubectl get -o name`` kinds, and the short form ``kubectl exec`` takes.
_WORKLOADS = {"deployment.apps": "deploy", "statefulset.apps": "sts",
              "daemonset.apps": "ds", "service": "svc", "pod": ""}


def _contexts(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    # Read from the kubeconfig: no cluster is contacted.
    argv = _kubectl(dict(values, kube_context=""), namespace=False)
    return [(name, name) for name in run_lines(ctx, argv + ["config", "get-contexts", "-o", "name"])]


def _namespaces(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    names = run_lines(ctx, _kubectl(values, namespace=False)
                      + ["get", "namespaces", "-o", "name"])
    return [(n.split("/", 1)[-1], n.split("/", 1)[-1]) for n in names]


def _targets(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    """Workloads first -- they keep their name across restarts -- then pods."""
    out: List[Tuple[str, str]] = []
    for line in run_lines(ctx, _kubectl(values) + [
            "get", "deployments,statefulsets,daemonsets,services,pods", "-o", "name"]):
        kind, _, name = line.partition("/")
        short = _WORKLOADS.get(kind)
        if short is None or not name:
            continue
        value = f"{short}/{name}" if short else name
        out.append((value, value))
    return out


def _containers(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    target = str(values.get("pod") or "").strip()
    # A service has no pod template to read containers from.
    if not target or target.startswith("svc/"):
        return []
    path = ("{.spec.template.spec.containers[*].name}" if "/" in target
            else "{.spec.containers[*].name}")
    kind_target = target if "/" in target else f"pod/{target}"
    names = run_lines(ctx, _kubectl(values) + ["get", kind_target, "-o", f"jsonpath={path}"])
    return [(n, n) for line in names for n in line.split()]


class KubernetesProtocolBackend(ProtocolBackend):
    protocol_id = "k8s"
    display_name = "Kubernetes"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            # Pods are replaced under new names on every rollout; a workload
            # (deploy/web) keeps working, and kubectl exec picks one of its pods.
            FieldSpec(key="pod", label=_("Pod or workload"), kind="text", required=True,
                      placeholder=_("pod, deploy/name or svc/name"), suggest=_targets),
            FieldSpec(key="container", label=_("Container"), kind="text",
                      placeholder=_("(default container)"), suggest=_containers),
            FieldSpec(key="namespace", label=_("Namespace"), kind="text",
                      placeholder=_("(context's namespace)"), suggest=_namespaces),
            FieldSpec(key="kube_context", label=_("Context"), kind="text",
                      placeholder=_("(current context)"), group="advanced",
                      suggest=_contexts),
            FieldSpec(key="kubeconfig", label=_("Kubeconfig"), kind="file",
                      group="advanced"),
            FieldSpec(key="command", label=_("Command"), kind="text", default="sh",
                      placeholder="sh"),
        ]

    def summary(self, data: Dict[str, Any]) -> str:
        pod = str(data.get("pod") or "").strip()
        if not pod:
            return ""
        namespace = str(data.get("namespace") or "").strip()
        text = f"{namespace}/{pod}" if namespace else pod
        context = str(data.get("kube_context") or "").strip()
        return f"{text} · {context}" if context else text

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

#!/usr/bin/env python3
"""Headless proof consumer for ``sshpilot.core``.

Exercises GTK-free core services without importing ``gi``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _cmd_inspect_config(args: argparse.Namespace) -> int:
    from sshpilot.core.settings import load_settings
    from sshpilot.platform.paths import get_config_dir

    path = Path(args.path) if args.path else get_config_dir() / "config.json"
    config, migrated = load_settings(path)
    _print_json({"path": str(path), "migrated": migrated, "config": config})
    return 0


def _cmd_validate_connection(args: argparse.Namespace) -> int:
    from sshpilot.core.validation import SSHConnectionValidator

    validator = SSHConnectionValidator()
    results = {
        "nickname": validator.validate_connection_name(args.nickname),
        "hostname": validator.validate_hostname(args.host),
        "port": validator.validate_port(str(args.port)),
        "username": validator.validate_username(args.user),
    }
    ok = all(r.is_valid for r in results.values())
    payload = {
        name: {"ok": r.is_valid, "message": r.message, "severity": r.severity}
        for name, r in results.items()
    }
    if args.json:
        _print_json({"ok": ok, "fields": payload})
    else:
        for name, result in results.items():
            status = "ok" if result.is_valid else "FAIL"
            print(f"{status} {name}: {result.message} ({result.severity})")
    return 0 if ok else 1


def _cmd_list_keys(args: argparse.Namespace) -> int:
    from sshpilot.core.keys import KeyService
    from sshpilot.platform.paths import get_ssh_dir

    ssh_dir = Path(args.ssh_dir) if args.ssh_dir else get_ssh_dir()
    service = KeyService(ssh_dir)
    keys = [str(k.private_path) for k in service.discover_keys()]
    if args.json:
        _print_json({"keys": keys})
    else:
        for key in keys:
            print(key)
    return 0


def _cmd_inspect_connections(args: argparse.Namespace) -> int:
    from sshpilot.core.connections import ConnectionService

    service = ConnectionService(store_path=args.path, autosave=False)
    if args.path and Path(args.path).exists():
        service.load(args.path)
    conns = [c.to_dict() for c in service.list_connections()]
    if args.json:
        _print_json({"count": len(conns), "connections": conns})
    else:
        for conn in conns:
            print(f"{conn.get('nickname')} {conn.get('username')}@{conn.get('hostname')}:{conn.get('port')}")
    return 0


def _cmd_validate_import(args: argparse.Namespace) -> int:
    from sshpilot.core.import_export import load_payload, plan_import, MergeStrategy

    payload = load_payload(args.path)
    plan = plan_import(
        payload,
        strategy=MergeStrategy(args.strategy),
        include_secrets=bool(args.include_secrets),
    )
    if args.json:
        _print_json(plan.to_dict())
    else:
        print(f"ok={plan.ok} version={plan.schema_version} add={len(plan.connections_to_add)} "
              f"update={len(plan.connections_to_update)} conflicts={len(plan.conflicts)}")
        for err in plan.errors:
            print(f"ERROR {err.field}: {err.message}", file=sys.stderr)
    return 0 if plan.ok else 1


def _cmd_plan_import(args: argparse.Namespace) -> int:
    return _cmd_validate_import(args)


def _cmd_build_ssh_command(args: argparse.Namespace) -> int:
    from sshpilot.core.ssh import (
        AuthMethod,
        ForwardSpec,
        HostKeyMode,
        SSHLaunchRequest,
        build_ssh_process_spec,
    )

    identities = [p for p in (args.identity or []) if p]
    forwards = []
    for item in args.local_forward or []:
        listen, _, dest = item.partition(":")
        # local_forward format listen:host:port — keep remainder as destination
        rest = item.split(":", 1)
        if len(rest) != 2:
            print(f"Invalid local forward {item!r}", file=sys.stderr)
            return 1
        forwards.append(ForwardSpec(kind="local", listen=rest[0], destination=rest[1]))
    for item in args.remote_forward or []:
        rest = item.split(":", 1)
        if len(rest) != 2:
            print(f"Invalid remote forward {item!r}", file=sys.stderr)
            return 1
        forwards.append(ForwardSpec(kind="remote", listen=rest[0], destination=rest[1]))
    for item in args.dynamic_forward or []:
        forwards.append(ForwardSpec(kind="dynamic", listen=item))

    auth = AuthMethod.PASSWORD if args.password else AuthMethod.PUBLIC_KEY
    host_key = HostKeyMode(args.strict_host_key) if args.strict_host_key else None
    req = SSHLaunchRequest(
        destination=args.destination,
        config_file=args.config,
        username=args.user,
        port=args.port,
        identity_files=identities,
        certificate_file=args.certificate,
        proxy_jump=[j for j in (args.proxy_jump or []) if j],
        forwards=forwards,
        agent_forwarding=bool(args.agent_forward),
        auth_method=auth,
        host_key_mode=host_key,
        batch_mode=bool(args.batch),
        compression=bool(args.compression),
        keepalive_interval=args.keepalive,
        remote_command=args.remote_command,
        askpass_required=bool(args.askpass),
    )
    try:
        spec = build_ssh_process_spec(req)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        _print_json({"argv": list(spec.argv), "env": dict(spec.env)})
    else:
        print(" ".join(spec.argv))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sshpilot-core")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect-config", help="Load and print settings JSON")
    p_inspect.add_argument("--path", help="config.json path (default: XDG)")
    p_inspect.set_defaults(func=_cmd_inspect_config)

    p_val = sub.add_parser("validate-connection", help="Validate connection fields")
    p_val.add_argument("--nickname", required=True)
    p_val.add_argument("--host", required=True)
    p_val.add_argument("--user", required=True)
    p_val.add_argument("--port", default=22, type=int)
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=_cmd_validate_connection)

    p_keys = sub.add_parser("list-keys", help="Discover SSH private keys")
    p_keys.add_argument("--ssh-dir", help="SSH directory (default: ~/.ssh)")
    p_keys.add_argument("--json", action="store_true")
    p_keys.set_defaults(func=_cmd_list_keys)

    p_conns = sub.add_parser("inspect-connections", help="List connections from a domain store")
    p_conns.add_argument("--path", help="JSON connection store path")
    p_conns.add_argument("--json", action="store_true")
    p_conns.set_defaults(func=_cmd_inspect_connections)

    for name, help_text in (
        ("validate-import", "Validate an import/export JSON payload"),
        ("plan-import", "Dry-run import plan for a payload"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", help="JSON payload path")
        p.add_argument("--strategy", choices=("merge", "replace", "skip"), default="merge")
        p.add_argument("--include-secrets", action="store_true")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=_cmd_plan_import if name == "plan-import" else _cmd_validate_import)

    p_ssh = sub.add_parser("build-ssh-command", help="Compose an SSH argv from launch options")
    p_ssh.add_argument("destination")
    p_ssh.add_argument("--user")
    p_ssh.add_argument("--port", type=int)
    p_ssh.add_argument("--config")
    p_ssh.add_argument("--identity", action="append", default=[])
    p_ssh.add_argument("--certificate")
    p_ssh.add_argument("--proxy-jump", action="append", default=[])
    p_ssh.add_argument("--local-forward", action="append", default=[])
    p_ssh.add_argument("--remote-forward", action="append", default=[])
    p_ssh.add_argument("--dynamic-forward", action="append", default=[])
    p_ssh.add_argument("--agent-forward", action="store_true")
    p_ssh.add_argument("--password", action="store_true")
    p_ssh.add_argument("--strict-host-key", choices=("yes", "no", "ask", "accept-new"))
    p_ssh.add_argument("--batch", action="store_true")
    p_ssh.add_argument("--compression", action="store_true")
    p_ssh.add_argument("--keepalive", type=int)
    p_ssh.add_argument("--remote-command")
    p_ssh.add_argument("--askpass", action="store_true")
    p_ssh.add_argument("--json", action="store_true")
    p_ssh.set_defaults(func=_cmd_build_ssh_command)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

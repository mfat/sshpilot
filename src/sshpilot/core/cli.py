#!/usr/bin/env python3
"""Minimal headless proof consumer for ``sshpilot.core``.

Exercises the GTK-free core boundary without importing ``gi``.
Not a full product CLI — see Phase 12 docs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_inspect_config(args: argparse.Namespace) -> int:
    from sshpilot.core.settings import load_settings
    from sshpilot.platform.paths import get_config_dir

    path = Path(args.path) if args.path else get_config_dir() / "config.json"
    config, migrated = load_settings(path)
    print(json.dumps({"path": str(path), "migrated": migrated, "config": config}, indent=2))
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
    for name, result in results.items():
        status = "ok" if result.is_valid else "FAIL"
        print(f"{status} {name}: {result.message} ({result.severity})")
    return 0 if ok else 1


def _cmd_list_keys(args: argparse.Namespace) -> int:
    from sshpilot.core.keys import KeyService
    from sshpilot.platform.paths import get_ssh_dir

    ssh_dir = Path(args.ssh_dir) if args.ssh_dir else get_ssh_dir()
    service = KeyService(ssh_dir)
    for key in service.discover_keys():
        print(key.private_path)
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
    p_val.set_defaults(func=_cmd_validate_connection)

    p_keys = sub.add_parser("list-keys", help="Discover SSH private keys")
    p_keys.add_argument("--ssh-dir", help="SSH directory (default: ~/.ssh)")
    p_keys.set_defaults(func=_cmd_list_keys)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

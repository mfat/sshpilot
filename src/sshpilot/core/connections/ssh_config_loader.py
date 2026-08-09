"""Headless SSH config loading (GTK-free).

Loads the complete SSH configuration reachable from a root file — including
recursive ``Include`` resolution — into lossless connection records and
preservation rules. This is the daemon-owned read path: it never writes, never
imports GI/``Config``/``ConnectionManager``/``GroupManager``, never runs
subprocesses, and never falls back to a local cache. The host alias is the
connection ID and no UUID fields are produced.

Loading is strict: any unreadable participating file or malformed ``Host``
header aborts the whole load (no partial state is returned) and surfaces a
generic ``CoreError`` that never embeds filesystem paths. The deterministic
revision hashes every participating file's path-relative identity plus its
exact bytes, so any edit anywhere in the include tree changes the revision.
"""

from __future__ import annotations

import getpass
import glob
import hashlib
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ...ssh_config_document import (
    HostBlock,
    MatchBlock,
    SSHConfigDocument,
    _split_config_option,
    _split_keyword,
)
from ...ssh_config_formatter import MANAGED_HOST_OPTIONS
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord

_MAX_INCLUDE_DEPTH = 32
_ACCUMULATE_KEYS = frozenset(
    {
        "localforward",
        "remoteforward",
        "dynamicforward",
        "identityfile",
        "certificatefile",
    }
)

_TOKEN_RE = re.compile(r"%([%duilL])")


@dataclass(frozen=True)
class LoadedSshConfiguration:
    """Complete daemon-owned SSH configuration result."""

    connections: Tuple[ConnectionRecord, ...]
    rules: Tuple[Mapping[str, Any], ...]
    source_paths: frozenset
    root_revision: str


def _config_error(message: str) -> CoreError:
    return CoreError(ErrorCode.CONFIG_PARSE_ERROR, message)


def _expand_ssh_tokens(value: str) -> str:
    """Expand host-independent ssh_config(5) percent tokens (%% %d %u %i %l %L)."""
    if not value or "%" not in value:
        return value
    home = os.path.expanduser("~")
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    try:
        import socket

        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    mapping = {
        "%": "%",
        "d": home,
        "u": user,
        "i": str(os.getuid()) if hasattr(os, "getuid") else "",
        "l": hostname,
        "L": hostname.split(".")[0],
    }

    def _repl(match: "re.Match") -> str:
        token = match.group(1)
        return mapping.get(token, match.group(0))

    return _TOKEN_RE.sub(_repl, value)


# ---------------------------------------------------------------------------
# Include resolution (headless; unreadable files are fatal)
# ---------------------------------------------------------------------------

def _resolve_config_files(root_path: Path, *, max_depth: int = _MAX_INCLUDE_DEPTH) -> List[Path]:
    """Resolve *root_path* and its recursive Includes into ordered files.

    Missing exact includes are tolerated (ssh treats them as empty). Any file
    that exists but cannot be read raises a generic ``CoreError`` — the load
    is strict and returns no partial state. Include cycles and depth overflow
    stop recursion for that branch.
    """
    resolved: List[Path] = []
    visited = set()
    unreadable = []

    _MISSING = object()

    def _read_lines(path: Path):
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.readlines()
        except FileNotFoundError:
            return _MISSING  # missing include -> tolerated, treated as empty
        except (OSError, UnicodeError):
            unreadable.append(path)
            return None

    def _resolve(path: Path, depth: int, stack: List[Path]) -> None:
        abs_path = path.resolve()
        if abs_path in stack:
            return  # include cycle
        if depth > max_depth:
            return
        if abs_path in visited:
            return
        lines = _read_lines(abs_path)
        if lines is None:
            return
        if lines is _MISSING:
            return
        visited.add(abs_path)
        resolved.append(abs_path)
        stack.append(abs_path)
        base_dir = abs_path.parent
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            lowered = line.lower()
            if not lowered.startswith("include "):
                continue
            try:
                patterns = shlex.split(line[len("include "):])
            except ValueError:
                raise _config_error(
                    "SSH configuration contains an invalid Include header"
                ) from None
            for pattern in patterns:
                expanded = os.path.expanduser(os.path.expandvars(
                    _expand_ssh_tokens(pattern)
                ))
                if not os.path.isabs(expanded):
                    expanded = os.path.join(base_dir, expanded)
                for matched in sorted(glob.glob(expanded)):
                    matched_path = Path(matched)
                    if matched_path.is_dir():
                        for fname in sorted(glob.glob(os.path.join(matched, "*"))):
                            _resolve(Path(fname), depth + 1, stack)
                    else:
                        _resolve(matched_path, depth + 1, stack)
        stack.pop()

    _resolve(root_path, 1, [])
    if unreadable:
        # Never surface the offending path in the public message.
        raise _config_error("SSH configuration could not be read completely")
    return resolved


# ---------------------------------------------------------------------------
# Pure directive parsing (mirrors the legacy ConnectionManager parsing)
# ---------------------------------------------------------------------------

def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _unwrap_ssh_value(val: Any) -> Any:
    if isinstance(val, str) and len(val) >= 2:
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            return val[1:-1]
    return val


def _parse_forward_listen_spec(spec: str):
    if ":" in spec:
        bind_addr, port_str = spec.rsplit(":", 1)
        bind_addr = bind_addr.strip().strip("[]")
    else:
        bind_addr, port_str = "", spec
    port = _safe_int(port_str, None)
    return None if port is None else (bind_addr, port)


def _parse_host_port_dest(dest_spec: str):
    if ":" in dest_spec:
        host, port_str = dest_spec.rsplit(":", 1)
        port = _safe_int(port_str, None)
        return None if port is None else (host, port)
    return dest_spec, 22


def _parse_forwarding_rules_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for forward_type in ("localforward", "remoteforward", "dynamicforward"):
        if forward_type not in config:
            continue
        forward_specs = config[forward_type]
        if not isinstance(forward_specs, list):
            forward_specs = [forward_specs]
        for forward_spec in forward_specs:
            if forward_type == "dynamicforward":
                listen = _parse_forward_listen_spec(forward_spec.strip())
                if listen is None:
                    continue
                bind_addr, listen_port = listen
                rules.append(
                    {
                        "type": "dynamic",
                        "listen_addr": bind_addr or "localhost",
                        "listen_port": listen_port,
                        "enabled": True,
                    }
                )
                continue
            parts = forward_spec.split()
            if not parts:
                continue
            listen = _parse_forward_listen_spec(parts[0])
            if listen is None:
                continue
            bind_addr, listen_port = listen
            dest_spec = parts[1] if len(parts) >= 2 else None
            if forward_type == "localforward":
                if dest_spec is None:
                    continue
                dest = _parse_host_port_dest(dest_spec)
                if dest is None:
                    continue
                remote_host, remote_port = dest
                rules.append(
                    {
                        "type": "local",
                        "listen_addr": bind_addr or "localhost",
                        "listen_port": listen_port,
                        "remote_host": remote_host,
                        "remote_port": remote_port,
                        "enabled": True,
                    }
                )
            else:
                rule = {
                    "type": "remote",
                    "listen_addr": bind_addr,
                    "listen_port": listen_port,
                    "enabled": True,
                }
                if dest_spec is None:
                    rule["socks"] = True
                else:
                    dest = _parse_host_port_dest(dest_spec)
                    if dest is None:
                        continue
                    local_host, local_port = dest
                    rule["local_host"] = local_host
                    rule["local_port"] = local_port
                rules.append(rule)
    return rules


def _resolve_key_select_mode(config: Dict[str, Any], has_specific_key: bool) -> Tuple[int, bool]:
    try:
        ident_only_raw = config.get("identitiesonly")
        ident_only_normalized = ident_only_raw
        if ident_only_raw and not isinstance(ident_only_raw, str):
            ident_only_normalized = str(ident_only_raw)
        ident_only = ""
        if isinstance(ident_only_normalized, str):
            ident_only = ident_only_normalized.strip().lower()
        if ident_only in ("yes", "true", "1", "on"):
            return 1, False
        if ident_only in ("no", "false", "0", "off"):
            return 2 if has_specific_key else 0, True
        if ident_only_raw is None or (
            isinstance(ident_only_raw, str) and not ident_only_raw.strip()
        ):
            return 2 if has_specific_key else 0, False
        return 0, False
    except Exception:
        return 2 if has_specific_key else 0, False


def _resolve_auth_method_from_config(config: Dict[str, Any]) -> Tuple[int, List[str], bool]:
    try:
        prefer_auth_raw = str(config.get("preferredauthentications", "")).strip()
        prefer_auth_list = [
            p.strip().lower()
            for p in prefer_auth_raw.split(",")
            if p.strip()
        ]
        pubkey_auth = str(config.get("pubkeyauthentication", "")).strip().lower()
        pubkey_auth_no = pubkey_auth == "no"
        if pubkey_auth == "no":
            return 1, prefer_auth_list, pubkey_auth_no
        idx_pubkey = (
            prefer_auth_list.index("publickey") if "publickey" in prefer_auth_list else None
        )
        idx_password = (
            prefer_auth_list.index("password") if "password" in prefer_auth_list else None
        )
        if idx_pubkey is not None and (idx_password is None or idx_pubkey < idx_password):
            return 0, prefer_auth_list, pubkey_auth_no
        if idx_password is not None and (idx_pubkey is None or idx_password < idx_pubkey):
            return 1, prefer_auth_list, pubkey_auth_no
        return 0, prefer_auth_list, pubkey_auth_no
    except Exception:
        return 0, [], False


def _parse_host_config(
    config: Dict[str, Any],
    *,
    source: Optional[str] = None,
    isolated: bool = False,
    rules: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Parse one host configuration dict into connection data.

    Wildcard/negated host tokens append a rule block to *rules* and return
    ``None``, mirroring the legacy loader.
    """
    host_token = _unwrap_ssh_value(config.get("host", ""))
    if not host_token:
        return None
    raw_tokens = config.get("__host_tokens")
    tokens = [_unwrap_ssh_value(t) for t in raw_tokens] if raw_tokens else [host_token]
    config = dict(config)
    config.pop("__host_tokens", None)
    config.pop("aliases", None)

    if any("*" in t or "?" in t or t.startswith("!") for t in tokens):
        if rules is not None:
            rule_block = dict(config)
            rule_block["host"] = host_token
            if source:
                rule_block["source"] = source
            rules.append(rule_block)
        return None

    host = host_token
    has_explicit_hostname = (
        "hostname" in config and str(config["hostname"]).strip() != ""
    )
    hostname_value = config["hostname"] if has_explicit_hostname else None
    parsed_host = _unwrap_ssh_value(hostname_value) if has_explicit_hostname else ""

    def _expand_path_value(val: Any) -> str:
        return os.path.expanduser(
            os.path.expandvars(_expand_ssh_tokens(_unwrap_ssh_value(val)))
        )

    def _as_list(raw: Any) -> List[Any]:
        if raw is None:
            return []
        return list(raw) if isinstance(raw, list) else [raw]

    identity_files: List[str] = []
    identity_suppressed = False
    for entry in _as_list(config.get("identityfile")):
        unwrapped = _unwrap_ssh_value(entry)
        if isinstance(unwrapped, str) and unwrapped.strip().lower() == "none":
            identity_suppressed = True
            continue
        if unwrapped:
            identity_files.append(_expand_path_value(entry))

    certificate_files: List[str] = [
        _expand_path_value(entry)
        for entry in _as_list(config.get("certificatefile"))
        if _unwrap_ssh_value(entry)
    ]

    parsed: Dict[str, Any] = {
        "id": host,
        "nickname": host,
        "hostname": parsed_host,
        "host": host,
        "port": _safe_int(_unwrap_ssh_value(config.get("port", 22)), 22),
        "username": _unwrap_ssh_value(config.get("user", getpass.getuser())),
        "keyfile": identity_files[0] if identity_files else "",
        "identity_files": identity_files,
        "identity_file_none": identity_suppressed,
        "certificate": certificate_files[0] if certificate_files else "",
        "certificate_files": certificate_files,
        "forwarding_rules": _parse_forwarding_rules_from_config(config),
    }
    if has_explicit_hostname:
        parsed["aliases"] = []
    if source:
        parsed["source"] = source
    if isolated:
        parsed["isolated_mode"] = True

    try:
        fwd_x11_raw = str(config.get("forwardx11", "")).strip().lower()
        parsed["x11_forwarding"] = fwd_x11_raw in ("yes", "true", "1", "on")
        parsed["x11_forwarding_explicit_no"] = fwd_x11_raw == "no"
    except Exception:
        parsed["x11_forwarding"] = False

    if "proxycommand" in config:
        parsed["proxy_command"] = config["proxycommand"]
    if "proxyjump" in config:
        pj = config["proxyjump"]
        if isinstance(pj, list):
            parsed["proxy_jump"] = [p.strip() for p in pj]
        else:
            parsed["proxy_jump"] = [p.strip() for p in re.split(r"[\s,]+", pj)]

    for direct_key, parsed_key in (
        ("identityagent", "identity_agent"),
        ("addkeystoagent", "add_keys_to_agent"),
        ("pkcs11provider", "pkcs11_provider"),
        ("securitykeyprovider", "security_key_provider"),
    ):
        if direct_key in config:
            val = _unwrap_ssh_value(config.get(direct_key))
            if val is not None and str(val).strip():
                parsed[parsed_key] = str(val).strip()

    if "forwardagent" in config:
        fa_raw_wrapped = config.get("forwardagent")
        if fa_raw_wrapped is not None:
            fa_raw = str(_unwrap_ssh_value(fa_raw_wrapped)).strip()
            if fa_raw.lower() in ("no", "false", "0", "off"):
                parsed["forward_agent"] = False
                parsed["forward_agent_explicit_no"] = True
            else:
                parsed["forward_agent"] = True
                if fa_raw.lower() not in ("yes", "true", "1", "on", ""):
                    parsed["forward_agent_target"] = fa_raw

    try:
        def _unescape_cfg_value(val: str) -> str:
            if not isinstance(val, str):
                return val
            v = val.strip()
            if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            return v.replace('\\"', '"').replace("\\\\", "\\")

        if "__pre_command" in config:
            parsed["pre_command"] = config["__pre_command"]
        if "localcommand" in config:
            parsed["local_command"] = _unescape_cfg_value(config.get("localcommand", ""))
        if "remotecommand" in config:
            parsed["remote_command"] = _unescape_cfg_value(config.get("remotecommand", ""))
        if "requesttty" in config:
            tty_val = str(_unwrap_ssh_value(config.get("requesttty", ""))).strip().lower()
            if tty_val in ("true", "1", "on"):
                tty_val = "yes"
            elif tty_val in ("false", "0", "off"):
                tty_val = "no"
            if tty_val in ("yes", "no", "force", "auto"):
                parsed["request_tty"] = tty_val
    except Exception:
        pass

    keyfile_value = parsed.get("keyfile", "")
    keyfile_path = keyfile_value.strip() if isinstance(keyfile_value, str) else ""
    has_specific_key = bool(
        keyfile_path and not keyfile_path.lower().startswith("select key file")
    )
    parsed["key_select_mode"], parsed["identities_only_explicit_no"] = (
        _resolve_key_select_mode(config, has_specific_key)
    )

    auth_method, prefer_auth_list, pubkey_auth_no = _resolve_auth_method_from_config(
        config
    )
    parsed["preferred_authentications"] = prefer_auth_list
    parsed["pubkey_auth_no"] = pubkey_auth_no
    parsed["auth_method"] = auth_method

    extra_config_lines = []
    for key, value in config.items():
        if key.startswith("__"):
            continue
        if key.lower() in MANAGED_HOST_OPTIONS:
            continue
        if isinstance(value, list):
            extra_config_lines.extend(f"{key} {val}" for val in value)
        else:
            extra_config_lines.append(f"{key} {value}")
    if extra_config_lines:
        parsed["extra_ssh_config"] = "\n".join(extra_config_lines)

    return parsed


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------

def _compute_revision(files: List[Path]) -> str:
    """Deterministic SHA-256 over each file's path-relative identity + bytes."""
    hasher = hashlib.sha256()
    for path in files:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _config_error("SSH configuration could not be read completely") from exc
        rel = os.path.relpath(path)
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(data)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def load_ssh_configuration(
    root_path: Path,
    *,
    isolated: bool,
) -> LoadedSshConfiguration:
    """Load the complete SSH configuration rooted at *root_path*.

    Raises ``CoreError`` (never containing paths) when any participating file
    is unreadable or a ``Host`` header is malformed; no partial state is
    returned. ``ConnectionRecord.id`` is the Host alias and UUID fields are
    never produced.
    """
    root = Path(root_path)
    try:
        files = _resolve_config_files(root)
    except CoreError:
        raise
    except OSError as exc:
        raise _config_error("SSH configuration could not be read completely") from exc

    connections: Dict[str, Dict[str, Any]] = {}
    connection_order: List[str] = []
    rules: List[Dict[str, Any]] = []
    loaded_this_load: Dict[str, Dict[str, Any]] = {}

    def _merge_raw(into: Dict[str, Any], new: Dict[str, Any]) -> None:
        for k, v in new.items():
            if k in ("host", "__host_tokens"):
                continue
            if k in into:
                if k in _ACCUMULATE_KEYS or k not in MANAGED_HOST_OPTIONS:
                    base = into[k] if isinstance(into[k], list) else [into[k]]
                    extra = v if isinstance(v, list) else [v]
                    into[k] = base + extra
            else:
                into[k] = v

    def _materialise(token: str, raw_cfg: Dict[str, Any], tokens: List[str], cfg_file: Path) -> None:
        prior = loaded_this_load.get(token)
        if prior is not None:
            _merge_raw(prior["raw"], raw_cfg)
            host_cfg = dict(prior["raw"])
            host_cfg["host"] = token
            host_cfg["__host_tokens"] = [token]
            connection_data = _parse_host_config(
                host_cfg, source=prior["source"], isolated=isolated
            )
            if connection_data:
                connection_data["source"] = prior["source"]
                connections[token] = connection_data
            return

        raw_copy = dict(raw_cfg)
        raw_copy.pop("uuid", None)
        host_cfg = dict(raw_copy)
        host_cfg["host"] = token
        host_cfg["__host_tokens"] = list(tokens)
        connection_data = _parse_host_config(
            host_cfg, source=str(cfg_file), isolated=isolated, rules=rules
        )
        if not connection_data:
            return
        connection_data["source"] = str(cfg_file)
        if token not in connections:
            connection_order.append(token)
        connections[token] = connection_data
        loaded_this_load[token] = {
            "raw": raw_copy,
            "tokens": list(tokens),
            "source": str(cfg_file),
        }

    def flush_block(tokens: List[str], config: Dict[str, Any], cfg_file: Path) -> None:
        cleaned = [t.strip() for t in tokens if t and t.strip()]
        if not cleaned:
            return
        if any("*" in t or "?" in t or t.startswith("!") for t in cleaned):
            host_cfg = dict(config)
            host_cfg["host"] = cleaned[0]
            host_cfg["__host_tokens"] = list(cleaned)
            _parse_host_config(
                host_cfg, source=str(cfg_file), isolated=isolated, rules=rules
            )
            return
        for token in cleaned:
            _materialise(token, config, cleaned, cfg_file)

    def _absorb_option_lines(raw_lines, config: Dict[str, Any]) -> None:
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# sshpilot:PreCommand "):
                    config["__pre_command"] = line[len("# sshpilot:PreCommand "):].strip()
                continue
            if _split_keyword(line)[0] == "include":
                continue  # Include lines are handled by resolution
            key, value = _split_config_option(line)
            if key is None:
                continue
            key = key.lower()
            if key in config:
                if key in _ACCUMULATE_KEYS or key not in MANAGED_HOST_OPTIONS:
                    if not isinstance(config[key], list):
                        config[key] = [config[key]]
                    config[key].append(value)
            else:
                config[key] = value

    for cfg_file in files:
        try:
            doc = SSHConfigDocument.parse_file(cfg_file)
        except (OSError, UnicodeDecodeError) as exc:
            raise _config_error("SSH configuration could not be read completely") from exc

        pending_tokens: List[str] = []
        pending_config: Dict[str, Any] = {}

        for node in doc.nodes:
            if isinstance(node, MatchBlock):
                if pending_tokens and pending_config:
                    flush_block(pending_tokens, pending_config, cfg_file)
                pending_tokens, pending_config = [], {}
                block_lines = [line.rstrip("\r\n") for line in node.lines]
                while block_lines and block_lines[-1].strip() == "":
                    block_lines.pop()
                rules.append({"raw": "\n".join(block_lines), "source": str(cfg_file)})
                continue
            if isinstance(node, HostBlock):
                _, remainder = _split_keyword(node.lines[0].lstrip())
                try:
                    tokens = shlex.split(remainder) if remainder else []
                except ValueError as exc:
                    raise _config_error(
                        "SSH configuration contains an invalid Host header"
                    ) from exc
                if not tokens:
                    _absorb_option_lines(node.lines[1:], pending_config)
                    continue
                if pending_tokens and pending_config:
                    flush_block(pending_tokens, pending_config, cfg_file)
                pending_tokens, pending_config = tokens, {}
                _absorb_option_lines(node.lines[1:], pending_config)
                continue
            _absorb_option_lines(node.lines, pending_config)

        if pending_tokens and pending_config:
            flush_block(pending_tokens, pending_config, cfg_file)

    records = tuple(
        ConnectionRecord.from_dict(connections[token], connection_id=token)
        for token in connection_order
    )
    return LoadedSshConfiguration(
        connections=records,
        rules=tuple(rules),
        source_paths=frozenset(files),
        root_revision=_compute_revision(files),
    )

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
    watch_paths: frozenset = frozenset()


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

def _resolve_config_files(
    root_path: Path,
    *,
    max_depth: int = _MAX_INCLUDE_DEPTH,
    content_overrides: Optional[Mapping[Path, bytes]] = None,
    watch_paths: Optional[set[Path]] = None,
) -> List[Path]:
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
    overrides = {
        Path(path).resolve(): bytes(content)
        for path, content in (content_overrides or {}).items()
    }

    def _read_lines(path: Path):
        override = overrides.get(path.resolve())
        if override is not None:
            try:
                return override.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                unreadable.append(path)
                return None
        try:
            return path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError:
            return _MISSING  # missing include -> tolerated, treated as empty
        except (OSError, UnicodeError):
            unreadable.append(path)
            return None

    def _resolve(path: Path, depth: int, stack: List[Path]) -> None:
        abs_path = path.resolve()
        if watch_paths is not None:
            watch_paths.add(abs_path)
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
                expanded = os.path.abspath(expanded)
                if watch_paths is not None:
                    if glob.has_magic(expanded):
                        candidate = expanded
                        while glob.has_magic(candidate):
                            parent = os.path.dirname(candidate)
                            if parent == candidate:
                                break
                            candidate = parent
                        watch_paths.add(Path(candidate))
                    else:
                        watch_paths.add(Path(expanded))
                        watch_paths.add(Path(expanded).parent)
                for matched in sorted(glob.glob(expanded)):
                    matched_path = Path(matched)
                    if matched_path.is_dir():
                        if watch_paths is not None:
                            watch_paths.add(matched_path.resolve())
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
    # ``ssh -G <host>`` receives this token as an argv element.  OpenSSH may
    # interpret a leading dash as another option, so imported configuration
    # must fail closed instead of materialising an unsafe connection.
    if host.startswith("-"):
        raise _config_error("SSH configuration contains an invalid Host alias")
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

    # Directives authored after an ``Include`` line: in force for this host,
    # but outside the block span the editor and writer own.
    outside_span_keys = config.get("__outside_span_keys") or frozenset()

    identity_files: List[str] = []
    identity_suppressed = False
    raw_identity_files: List[str] = []
    for entry in _as_list(config.get("identityfile")):
        unwrapped = _unwrap_ssh_value(entry)
        if unwrapped:
            raw_identity_files.append(str(unwrapped))
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
        # An absent ``User`` must read as absent. Substituting the local user
        # here made every unauthored block claim a username OpenSSH would not
        # use (a global ``Host * User ...`` wins instead), and that fabricated
        # value was then written back into the block on the next save.
        "username": _unwrap_ssh_value(config.get("user", "")),
        "keyfile": identity_files[0] if identity_files else "",
        "identity_files": identity_files,
        "identity_file_none": identity_suppressed,
        "certificate": certificate_files[0] if certificate_files else "",
        "certificate_files": certificate_files,
        "forwarding_rules": _parse_forwarding_rules_from_config(config),
        # Literal values are private evidence for the identity prototype.
        # They are consumed by ConnectionRecord.from_dict and never emitted
        # as public connection data or written back to SSH config.
        "__identity_raw_port": (
            _unwrap_ssh_value(config["port"]) if "port" in config else None
        ),
        "__identity_raw_username": (
            _unwrap_ssh_value(config["user"]) if "user" in config else None
        ),
        "__identity_raw_identity_files": raw_identity_files,
        # Directives this host actually authored. ``config`` holds exactly the
        # block's own options — wildcard/negated blocks were diverted to
        # ``rules`` above — so its keys are the authorship evidence. The launch
        # path emits argv only for these, never for the defaults filled in
        # below, because OpenSSH resolves everything else from the config file
        # (where an earlier ``Host *`` may legitimately win).
        "__authored_directives": tuple(sorted(
            key
            for key in config
            if not key.startswith("__")
            and key not in {"host", "aliases"}
            and key not in outside_span_keys
        )),
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
        if key in outside_span_keys:
            # Authored outside this block's span (after an Include). Still in
            # force via the file itself; the editor does not own it, so the
            # writer must not copy it into the block.
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

def _compute_revision(
    files: List[Path], content_overrides: Optional[Mapping[Path, bytes]] = None
) -> str:
    """Deterministic SHA-256 over each file's path-relative identity + bytes."""
    hasher = hashlib.sha256()
    overrides = {
        Path(key).resolve(): bytes(value)
        for key, value in (content_overrides or {}).items()
    }
    for path in files:
        try:
            data = overrides.get(path.resolve())
            if data is None:
                data = path.read_bytes()
        except OSError as exc:
            raise _config_error("SSH configuration could not be read completely") from exc
        rel = os.path.relpath(path)
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(data)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _static_value(value: str) -> bool:
    """Whether a config value is literal enough for reconciliation evidence."""

    if "$" in value:
        return False
    if "%" in value and any(token != "%%" for token in re.findall(r"%[A-Za-z%]", value)):
        return False
    return True


def _host_option_values(block: HostBlock, key: str) -> List[str]:
    values: List[str] = []
    for raw_line in block.lines[1:]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        option, value = _split_config_option(line)
        if option and option.lower() == key and value is not None:
            values.append(_unwrap_ssh_value(value))
    return values


def _static_identity_evidence(
    files: List[Path], content_overrides: Optional[Mapping[Path, bytes]] = None
) -> Dict[str, Dict[str, str]]:
    """Conservatively classify evidence the legacy loader cannot prove.

    This is intentionally a small safety analysis, not an OpenSSH evaluator.
    Any global/wildcard/Match/Include ambiguity disables Rule 2 for concrete
    records. Exact alias continuity remains available independently.
    """

    blocks: Dict[str, List[HostBlock]] = {}
    global_reason: Optional[str] = None

    def mark_global_reason(reason: str) -> None:
        nonlocal global_reason
        priority = {
            "global_configuration": 1,
            "inherited_configuration": 2,
            "include_semantics": 3,
            "dynamic_match": 4,
        }
        if global_reason is None or priority[reason] > priority[global_reason]:
            global_reason = reason

    overrides = {
        Path(key).resolve(): bytes(value)
        for key, value in (content_overrides or {}).items()
    }
    for cfg_file in files:
        try:
            raw = overrides.get(cfg_file.resolve())
            doc = (
                SSHConfigDocument.parse_text(raw.decode("utf-8"), path=str(cfg_file))
                if raw is not None
                else SSHConfigDocument.parse_file(cfg_file)
            )
        except (OSError, UnicodeDecodeError):
            mark_global_reason("include_semantics")
            continue
        seen_host = False
        for node in doc.nodes:
            if isinstance(node, MatchBlock):
                mark_global_reason("dynamic_match")
                continue
            if isinstance(node, HostBlock):
                seen_host = True
                if any(
                    "*" in token or "?" in token or token.startswith("!")
                    for token in node.tokens
                ):
                    mark_global_reason("inherited_configuration")
                for token in node.tokens:
                    if not ("*" in token or "?" in token or token.startswith("!")):
                        blocks.setdefault(token, []).append(node)
                continue
            for raw_line in node.lines:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                keyword, value = _split_keyword(line)
                if keyword == "include":
                    mark_global_reason("include_semantics")
                elif not seen_host and value:
                    mark_global_reason("global_configuration")

    evidence: Dict[str, Dict[str, str]] = {}
    for alias, alias_blocks in blocks.items():
        result = {
            "destination_status": "unavailable",
            "destination_reason": "not_provided",
            "username_literal": "",
            "username_is_explicit": "0",
            "identity_mode": "unspecified",
            "identity_status": "unavailable",
            "identity_reason": "not_provided",
        }
        if global_reason is not None:
            result["destination_reason"] = global_reason
            result["identity_reason"] = global_reason
            evidence[alias] = result
            continue
        if len(alias_blocks) != 1:
            result["destination_reason"] = "repeated_host"
            result["identity_reason"] = "repeated_host"
            evidence[alias] = result
            continue
        block = alias_blocks[0]
        hostnames = _host_option_values(block, "hostname")
        ports = _host_option_values(block, "port")
        users = _host_option_values(block, "user")
        identities = _host_option_values(block, "identityfile")
        if len(hostnames) != 1:
            result["destination_reason"] = "missing_hostname"
        elif not _static_value(hostnames[0]):
            result["destination_reason"] = "host_dependent_hostname"
        else:
            port = 22
            if len(ports) > 1:
                result["destination_reason"] = "repeated_host"
            elif ports:
                try:
                    port = int(str(ports[0]).strip(), 10)
                except (TypeError, ValueError):
                    result["destination_reason"] = "invalid_port"
                if not 1 <= port <= 65535:
                    result["destination_reason"] = "invalid_port"
            if result["destination_reason"] == "not_provided":
                result["destination_status"] = "trustworthy"
                result["destination_reason"] = "explicit_static"
        if len(users) == 1 and _static_value(users[0]):
            result["username_literal"] = users[0]
            result["username_is_explicit"] = "1"
        if any(not _static_value(value) for value in identities):
            result["identity_mode"] = "dynamic"
            result["identity_status"] = "dynamic"
            result["identity_reason"] = "host_or_runtime_dependent"
        elif identities and all(
            str(value).strip().lower() == "none" for value in identities
        ):
            result["identity_mode"] = "explicit_none"
            result["identity_status"] = "safe_static_literal"
            result["identity_reason"] = "explicit_none"
        elif any(str(value).strip().lower() == "none" for value in identities):
            # Mixed ``none`` and file directives have order/merge semantics
            # that this analyzer does not prove safely.
            result["identity_mode"] = "dynamic"
            result["identity_status"] = "dynamic"
            result["identity_reason"] = "mixed_none_and_files"
        elif identities:
            result["identity_mode"] = "explicit_files"
            result["identity_status"] = "safe_static_literal"
            result["identity_reason"] = "static_literal"
        else:
            result["identity_mode"] = "unspecified"
            result["identity_status"] = "unavailable"
            result["identity_reason"] = "no_identityfile_directive"
        evidence[alias] = result
    return evidence


def load_ssh_configuration(
    root_path: Path,
    *,
    isolated: bool,
    _content_overrides: Optional[Mapping[Path, bytes]] = None,
) -> LoadedSshConfiguration:
    """Load the complete SSH configuration rooted at *root_path*.

    Raises ``CoreError`` (never containing paths) when any participating file
    is unreadable or a ``Host`` header is malformed; no partial state is
    returned. ``ConnectionRecord.id`` is the Host alias and UUID fields are
    never produced.
    """
    root = Path(root_path)
    watch_paths: set[Path] = set()
    try:
        files = _resolve_config_files(
            root,
            content_overrides=_content_overrides,
            watch_paths=watch_paths,
        )
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
        if any(token.startswith("-") for token in cleaned):
            raise _config_error("SSH configuration contains an invalid Host alias")
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
            raw = {
                Path(key).resolve(): bytes(value)
                for key, value in (_content_overrides or {}).items()
            }.get(cfg_file.resolve())
            doc = (
                SSHConfigDocument.parse_text(raw.decode("utf-8"), path=str(cfg_file))
                if raw is not None
                else SSHConfigDocument.parse_file(cfg_file)
            )
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
            # A RawSpan after a Host block holds directives that follow an
            # ``Include`` line. OpenSSH still scopes them to the enclosing Host
            # (verified: they apply to that host and not to later ones), so they
            # belong in this record's semantics — but they sit *outside* the
            # block's editable span, which the surgical writer owns. Re-emitting
            # them inside the span left the originals in place and appended a
            # fresh copy on every save, growing the file without bound. Record
            # the keys this span introduced so they stay out of
            # ``extra_ssh_config`` and are never written back.
            before = set(pending_config)
            _absorb_option_lines(node.lines, pending_config)
            introduced = {
                key
                for key in set(pending_config) - before
                if not key.startswith("__")
            }
            # Only mark when this span actually introduced directives: an empty
            # ``pending_config`` must stay empty, because a block that
            # contributed nothing is deliberately never materialised.
            if introduced:
                pending_config.setdefault("__outside_span_keys", set()).update(
                    introduced
                )

        if pending_tokens and pending_config:
            flush_block(pending_tokens, pending_config, cfg_file)

    static_evidence = _static_identity_evidence(files, _content_overrides)
    for token in connection_order:
        values = static_evidence.get(token, {})
        connections[token].update(
            {
                "__identity_destination_status": values.get(
                    "destination_status", "unavailable"
                ),
                "__identity_destination_reason": values.get(
                    "destination_reason", "not_provided"
                ),
                "__identity_username_literal": values.get(
                    "username_literal", ""
                )
                or None,
                "__identity_username_is_explicit": values.get(
                    "username_is_explicit", "0"
                )
                == "1",
                "__identity_file_evidence_status": values.get(
                    "identity_status", "unavailable"
                ),
                "__identity_file_evidence_mode": values.get(
                    "identity_mode", "unspecified"
                ),
                "__identity_file_evidence_reason": values.get(
                    "identity_reason", "not_provided"
                ),
            }
        )

    records = tuple(
        ConnectionRecord.from_dict(connections[token], connection_id=token)
        for token in connection_order
    )
    return LoadedSshConfiguration(
        connections=records,
        rules=tuple(rules),
        source_paths=frozenset(files),
        root_revision=_compute_revision(files, _content_overrides),
        watch_paths=frozenset(watch_paths or files),
    )

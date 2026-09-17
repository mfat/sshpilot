"""The easyenv CLI's config file, shared with this plugin.

The token lives where the CLI keeps it, in the CLI's format, rather than in
sshPilot's keyring. One file, mode 0600, is exactly what ``easyenv auth login``
writes, so signing in here signs the CLI in and the other way round, and there
is no second copy of the credential to go stale.

The file is YAML written by a flat Go struct. Only its top-level scalars are
read and written, which a few lines handle without a YAML library; anything
else in the file is kept as it was.
"""

from __future__ import annotations

import json
import os

DEFAULT_SERVER = "https://api.easyenv.io"

CONFIG_KEYS = ("server", "service_token", "default_account", "web_url")


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "easyenv", "config.yaml")


def _yaml_scalar(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    if raw[0] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw[0] == "'":
        return raw[1:-1].replace("''", "'") if raw.endswith("'") else raw[1:]
    # An unquoted scalar ends at a comment.
    return raw.split(" #", 1)[0].strip()


def read_config(path: str | None = None) -> dict:
    """The CLI's settings, with the CLI's environment overrides applied."""
    path = path or config_path()
    values: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line or line[0] in " \t#" or ":" not in line:
                    continue
                key, _, rest = line.partition(":")
                if key.strip() in CONFIG_KEYS:
                    values[key.strip()] = _yaml_scalar(rest)
    except FileNotFoundError:
        pass
    for key, env in (("server", "EASYENV_SERVER"),
                     ("service_token", "EASYENV_TOKEN"),
                     ("default_account", "EASYENV_ACCOUNT"),
                     ("web_url", "EASYENV_WEB_URL")):
        if os.environ.get(env):
            values[key] = os.environ[env]
    if not values.get("server"):
        values["server"] = DEFAULT_SERVER
    return values


def write_config(updates: dict, path: str | None = None) -> str:
    """Set or clear top-level keys in the CLI's config, keeping everything else.

    A value of ``None`` or ``""`` removes the key, which is how
    ``easyenv auth logout`` leaves the file.

    Written to a temporary file and renamed, with 0600 from the moment it
    exists: the token must never sit in a file anyone else can read, not even
    between a create and a chmod.
    """
    path = path or config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []

    pending = dict(updates)
    kept = []
    for line in lines:
        key = line.partition(":")[0].strip() if (line and line[0] not in " \t#") else ""
        if key in pending:
            value = pending.pop(key)
            if value:
                kept.append(f"{key}: {json.dumps(str(value))}")
            continue
        kept.append(line)
    for key, value in pending.items():
        if value:
            kept.append(f"{key}: {json.dumps(str(value))}")

    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + ("\n" if kept else ""))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path

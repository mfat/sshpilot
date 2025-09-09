import os
import glob
import shlex
import subprocess
from typing import Dict, List, Set, Union


def resolve_ssh_config_files(main_path: str) -> List[str]:
    """Return a list of SSH config files including those referenced by Include.

    Paths are expanded and resolved relative to their parent file. Duplicate files
    are ignored. The main file is always first in the returned list.
    """
    resolved: List[str] = []
    visited: Set[str] = set()

    def _resolve(path: str):
        abs_path = os.path.abspath(os.path.expanduser(path))
        if abs_path in visited:
            return
        visited.add(abs_path)
        try:
            with open(abs_path, 'r') as f:
                lines = f.readlines()
        except OSError:
            return
        resolved.append(abs_path)
        base_dir = os.path.dirname(abs_path)
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            lowered = line.lower()
            if lowered.startswith('include '):
                patterns = shlex.split(line[len('include '):])
                for pattern in patterns:
                    pattern = os.path.expanduser(pattern)
                    if not os.path.isabs(pattern):
                        pattern = os.path.join(base_dir, pattern)
                    for matched in sorted(glob.glob(pattern)):
                        _resolve(matched)

    _resolve(main_path)
    return resolved


def get_effective_ssh_config(host: str) -> Dict[str, Union[str, List[str]]]:
    """Return effective SSH options for *host* using ``ssh -G``.

    The output is parsed into a dictionary with lowercased keys. Options that
    appear multiple times (e.g. ``IdentityFile``) are stored as lists.
    """
    try:
        result = subprocess.run(
            ['ssh', '-G', host], capture_output=True, text=True, check=True
        )
    except Exception:
        return {}

    config: Dict[str, Union[str, List[str]]] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ' ' in line:
            key, value = line.split(None, 1)
        else:
            key, value = line, ''
        key = key.lower()
        value = value.strip()
        if key in config:
            existing = config[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                config[key] = [existing, value]
        else:
            config[key] = value
    return config

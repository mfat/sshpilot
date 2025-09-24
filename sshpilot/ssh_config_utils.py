import os
import glob
import shlex
import logging
import subprocess
from typing import Dict, List, Optional, Set, Union


logger = logging.getLogger(__name__)


def resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    """Return a list of SSH config files including those referenced by Include.

    Paths are expanded and resolved relative to their parent file. Duplicate files
    are ignored. The main file is always first in the returned list. A recursion
    guard prevents cycles and limits include depth.
    """
    resolved: List[str] = []
    visited: Set[str] = set()

    def _resolve(path: str, depth: int, stack: List[str]):
        abs_path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        if abs_path in stack:
            logger.warning("Include cycle detected: %s -> %s", " -> ".join(stack), abs_path)
            return
        if depth > max_depth:
            logger.warning("Maximum include depth (%d) exceeded at %s", max_depth, abs_path)
            return
        if abs_path in visited:
            return
        try:
            with open(abs_path, 'r') as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Cannot read include file %s: %s", abs_path, exc)
            return
        visited.add(abs_path)
        resolved.append(abs_path)
        base_dir = os.path.dirname(abs_path)
        stack.append(abs_path)
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            lowered = line.lower()
            if lowered.startswith('include '):
                patterns = shlex.split(line[len('include '):])
                for pattern in patterns:
                    expanded = os.path.expanduser(os.path.expandvars(pattern))
                    if not os.path.isabs(expanded):
                        expanded = os.path.join(base_dir, expanded)
                    matches = glob.glob(expanded)
                    if not matches:
                        logger.warning("Include pattern %s does not match any files", pattern)
                    for matched in sorted(matches):
                        if os.path.isdir(matched):
                            dir_matches = sorted(glob.glob(os.path.join(matched, '*')))
                            if not dir_matches:
                                logger.warning("Include directory %s is empty", matched)
                            for fname in dir_matches:
                                _resolve(fname, depth + 1, stack)
                        else:
                            _resolve(matched, depth + 1, stack)
        stack.pop()

    _resolve(main_path, 1, [])
    return resolved


def get_effective_ssh_config(
    host: str, config_file: Optional[str] = None

) -> Dict[str, Union[str, List[str]]]:
    """Return effective SSH options for *host* using ``ssh -G``.

    The output is parsed into a dictionary with lowercased keys. Options that
    appear multiple times (e.g. ``IdentityFile``) are stored as lists.
    """
    cmd = ['ssh']
    if config_file:
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(config_file)))
        if os.path.isfile(expanded):
            cmd.extend(['-F', expanded])
        else:
            logger.warning("Requested SSH config override %s does not exist", expanded)
    cmd.extend(['-G', host])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

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

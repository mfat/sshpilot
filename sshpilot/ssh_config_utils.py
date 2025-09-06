import os
import glob
import shlex
from typing import List, Set


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

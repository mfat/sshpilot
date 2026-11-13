"""OpenSSH options that re-enable old algorithms for legacy devices.

Old switches, routers and appliances only offer key exchange, host key and
cipher algorithms that current OpenSSH has switched off, so a connection ends
in "no matching key exchange method" (or host key type, or cipher). The
Preferences switch "Legacy device compatibility" appends those algorithms
again.

Two things make this more than a fixed list:

* Every option uses the ``+`` form, which *adds* to OpenSSH's defaults. Modern
  algorithms stay first in the preference order, so a current server still
  negotiates exactly what it would have without the switch.
* Naming an algorithm the installed OpenSSH no longer ships is a hard error
  ("Unsupported KEX algorithm", "Bad key types") that would stop *every*
  connection. So the candidates are filtered through ``ssh -Q``, and each
  finished option is test-parsed with ``ssh -G`` before it is used. Both only
  query the local binary: nothing connects anywhere.

The daemon computes the fragment once at startup and hands it to the SSH
overrides service. These are local queries of the ``ssh`` binary, not
connection launches, so they do not go through ``SshLauncher``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

from ..platform.process import hidden_console_kwargs

logger = logging.getLogger(__name__)

#: ``(option names to try in order, ssh -Q query, legacy algorithms)``.
#: Weakest last: the order is the order they are offered in.
LEGACY_ALGORITHMS: Tuple[Tuple[Tuple[str, ...], str, Tuple[str, ...]], ...] = (
    (
        ("KexAlgorithms",),
        "kex",
        (
            "diffie-hellman-group14-sha1",
            "diffie-hellman-group-exchange-sha1",
            "diffie-hellman-group1-sha1",
        ),
    ),
    (("HostKeyAlgorithms",), "key", ("ssh-rsa", "ssh-dss")),
    (
        # Renamed in OpenSSH 8.5; older clients only know the second name.
        ("PubkeyAcceptedAlgorithms", "PubkeyAcceptedKeyTypes"),
        "key",
        ("ssh-rsa", "ssh-dss"),
    ),
    (
        ("Ciphers",),
        "cipher",
        ("aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc"),
    ),
)

_TIMEOUT = 5.0

Runner = Callable[[Sequence[str]], Optional[Tuple[int, str]]]


def _run(argv: Sequence[str]) -> Optional[Tuple[int, str]]:
    """Run a local ``ssh`` query; ``None`` when it could not run at all."""
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
            **hidden_console_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode, result.stdout or ""


def _supported(query: str, run: Runner, ssh: str) -> Optional[frozenset]:
    answer = run([ssh, "-Q", query])
    if answer is None or answer[0] != 0:
        return None
    return frozenset(line.strip() for line in answer[1].splitlines() if line.strip())


def _parses(option: str, run: Runner, ssh: str) -> bool:
    answer = run([ssh, "-G", "-F", os.devnull, "-o", option, "sshpilot-legacy-check"])
    return answer is not None and answer[0] == 0


def legacy_algorithm_args(*, ssh: str = "ssh", run: Runner = _run) -> List[str]:
    """The ``-o`` fragment that re-enables legacy algorithms on this machine.

    Empty when ``ssh`` cannot be queried: an unverified option is never
    returned, because a wrong one breaks every connection.
    """
    args: List[str] = []
    for option_names, query, candidates in LEGACY_ALGORITHMS:
        supported = _supported(query, run, ssh)
        if not supported:
            continue
        usable = [name for name in candidates if name in supported]
        if not usable:
            continue
        for option_name in option_names:
            option = f"{option_name}=+{','.join(usable)}"
            if _parses(option, run, ssh):
                args.extend(["-o", option])
                break
        else:
            logger.debug("legacy option for %s not accepted by ssh", query)
    return args


__all__ = ["LEGACY_ALGORITHMS", "legacy_algorithm_args"]

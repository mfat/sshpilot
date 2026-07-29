"""SSH launch policy helpers (GTK-free).

Command construction that needs askpass / secret backends remains in
``sshpilot.ssh_connection_builder`` (already imports without ``gi`` at
module load). Core exposes the portable :class:`ProcessSpec` adapter.
"""
from .process_spec import ProcessSpec

__all__ = ["ProcessSpec"]

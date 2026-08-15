"""Daemon-owned live SSH configuration-scope transitions."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

from sshpilot.api.models.daemon import (
    OperationMode,
    OperationModeResult,
    SetOperationModeRequest,
)

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore

logger = logging.getLogger(__name__)


class OperationModeService:
    """Serialize mode transitions and keep persisted/runtime state coherent."""

    def __init__(
        self,
        repository: ConnectionRepository,
        *,
        config_path: Path,
        default_root: Path,
        isolated_root: Path,
    ) -> None:
        self._repository = repository
        self._config_path = Path(config_path)
        self._default_root = Path(default_root)
        self._isolated_root = Path(isolated_root)
        self._lock = threading.RLock()
        self._resource_probe: Callable[[], Tuple[str, ...]] = lambda: ()
        self._on_committed: Optional[Callable[[], None]] = None

    def set_runtime_hooks(
        self,
        *,
        resource_probe: Callable[[], Tuple[str, ...]],
        on_committed: Callable[[], None],
    ) -> None:
        self._resource_probe = resource_probe
        self._on_committed = on_committed

    @property
    def active_mode(self) -> OperationMode:
        return (
            OperationMode.ISOLATED
            if self._repository.ssh_config_isolated
            else OperationMode.DEFAULT
        )

    def apply(self, request: SetOperationModeRequest) -> OperationModeResult:
        if type(request) is not SetOperationModeRequest:
            raise TypeError("a SetOperationModeRequest is required")
        with self._lock:
            current = self.active_mode
            target_description = self._description(request.mode)
            if request.mode is current:
                return OperationModeResult(
                    accepted=True,
                    active_mode=current,
                    generation=self._repository.snapshot().generation,
                    target_description=target_description,
                )
            blockers = tuple(self._resource_probe() or ())
            if blockers:
                return OperationModeResult(
                    accepted=False,
                    active_mode=current,
                    generation=self._repository.snapshot().generation,
                    conflict=True,
                    message=(
                        "Operation mode cannot change while live daemon resources "
                        f"are active: {', '.join(blockers)}"
                    ),
                    target_description=target_description,
                )
            target = self._root_for(request.mode)
            target_created = False
            seeded = False
            old_mode = current
            old_config = None
            published = False
            try:
                target_created, seeded = self._prepare_target(request, target)
                old_config = self._read_config()
                new_config = self._with_mode(old_config, request.mode)
                self._write_config(new_config)
                try:
                    self._repository.transition_ssh_config(
                        SshConfigStore(target, isolated=request.mode is OperationMode.ISOLATED),
                        request.mode is OperationMode.ISOLATED,
                    )
                    published = True
                except Exception:
                    self._write_config(self._with_mode(old_config, old_mode))
                    raise
                if self._on_committed is not None:
                    self._on_committed()
                return OperationModeResult(
                    accepted=True,
                    active_mode=request.mode,
                    generation=self._repository.snapshot().generation,
                    seeded=seeded,
                    target_description=target_description,
                )
            except Exception as error:
                if published:
                    # Reconfiguration hooks are part of the transaction: if a
                    # dependent daemon service cannot adopt the new snapshot,
                    # restore the old repository and persisted mode before
                    # reporting failure.
                    try:
                        old_target = self._root_for(old_mode)
                        self._repository.transition_ssh_config(
                            SshConfigStore(old_target, isolated=old_mode is OperationMode.ISOLATED),
                            old_mode is OperationMode.ISOLATED,
                        )
                    except Exception as rollback_error:
                        logger.error(
                            "Operation-mode rollback could not restore the old repository: %s",
                            rollback_error,
                        )
                if old_config is not None:
                    try:
                        self._write_config(self._with_mode(old_config, old_mode))
                    except Exception as rollback_error:
                        logger.error(
                            "Operation-mode rollback could not restore config.json: %s",
                            rollback_error,
                        )
                if target_created:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                return OperationModeResult(
                    accepted=False,
                    active_mode=self.active_mode,
                    generation=self._repository.snapshot().generation,
                    message=f"Operation mode transition failed: {error}",
                    target_description=target_description,
                )

    def status(self) -> OperationModeResult:
        """Return confirmed daemon mode without inferring it from a path."""
        with self._lock:
            mode = self.active_mode
            return OperationModeResult(
                accepted=True,
                active_mode=mode,
                generation=self._repository.snapshot().generation,
                target_description=self._description(mode),
            )

    def _root_for(self, mode: OperationMode) -> Path:
        return self._isolated_root if mode is OperationMode.ISOLATED else self._default_root

    @staticmethod
    def _description(mode: OperationMode) -> str:
        return (
            "Isolated SSH configuration"
            if mode is OperationMode.ISOLATED
            else "Default user SSH configuration"
        )

    def _prepare_target(
        self, request: SetOperationModeRequest, target: Path
    ) -> tuple[bool, bool]:
        parent = target.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if request.mode is OperationMode.ISOLATED:
            os.chmod(parent, 0o700)
        if target.is_symlink():
            raise OSError("the SSH configuration target must not be a symlink")
        if target.exists():
            if not target.is_file():
                raise OSError("the SSH configuration target is not a file")
            if request.mode is OperationMode.ISOLATED:
                os.chmod(target, 0o600)
            return False, False
        source = self._default_root
        data = source.read_bytes() if request.seed_isolated_config and source.exists() else b""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return True, bool(request.seed_isolated_config and data)

    def _read_config(self) -> dict:
        try:
            with self._config_path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("daemon configuration is not an object")
        return value

    @staticmethod
    def _with_mode(config: dict, mode: OperationMode) -> dict:
        result = dict(config)
        ssh = dict(result.get("ssh") or {})
        ssh["use_isolated_config"] = mode is OperationMode.ISOLATED
        result["ssh"] = ssh
        return result

    def _write_config(self, config: dict) -> None:
        parent = self._config_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".config.", dir=str(parent))
        path = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self._config_path)
            try:
                dir_fd = os.open(parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

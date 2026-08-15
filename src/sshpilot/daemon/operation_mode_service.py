"""Daemon-owned live SSH configuration-scope transitions."""

from __future__ import annotations

import json
import logging
import os
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
from sshpilot.core.settings.store import (
    save_settings,
    settings_transaction_lock,
)
from sshpilot.core.settings.defaults import CONFIG_VERSION, get_default_config
from sshpilot.core.settings.migration import ensure_config_defaults

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
        self._on_rollback: Optional[Callable[[], None]] = None

    def set_runtime_hooks(
        self,
        *,
        resource_probe: Callable[[], Tuple[str, ...]],
        on_committed: Callable[[], None],
        on_rollback: Optional[Callable[[], None]] = None,
    ) -> None:
        self._resource_probe = resource_probe
        self._on_committed = on_committed
        self._on_rollback = on_rollback

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
        # This lock is shared by every daemon settings writer.  Keep it around
        # the complete read/modify/write and runtime publication transaction so
        # a plugin, identity, SSH-override, or secret-backend update cannot be
        # lost while the mode transition is in flight.
        with self._lock, settings_transaction_lock(self._config_path):
            current = self.active_mode
            target_description = self._description(request.mode)
            if request.mode is current:
                persisted = self._read_persisted_mode()
                if persisted is not current:
                    # A same-mode request is also a persistence reconciliation
                    # request. Never report the runtime mode as healthy while
                    # config.json is missing, malformed, or advertises the
                    # other scope.
                    try:
                        self._write_config(self._with_mode(self._read_config(), current))
                        persisted = current
                    except Exception as error:
                        return OperationModeResult(
                            accepted=False,
                            active_mode=current,
                            generation=self._repository.snapshot().generation,
                            message=(
                                "The daemon is running in "
                                f"{current.value} mode, but its persisted operation "
                                "mode could not be reconciled. Restart or manual "
                                f"recovery is required: {error}"
                            ),
                            target_description=target_description,
                            persisted_mode=persisted,
                            rollback_completed=False,
                            recovery_required=True,
                        )
                return OperationModeResult(
                    accepted=True,
                    active_mode=current,
                    generation=self._repository.snapshot().generation,
                    target_description=target_description,
                    persisted_mode=persisted,
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
                    persisted_mode=current,
                )
            target = self._root_for(request.mode)
            target_created = False
            seeded = False
            old_mode = current
            old_config = None
            config_existed = self._config_path.exists()
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
                    persisted_mode=request.mode,
                )
            except Exception as error:
                rollback_errors = []
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
                        rollback_errors.append(f"runtime: {rollback_error}")
                        logger.error(
                            "Operation-mode rollback could not restore the old repository: %s",
                            rollback_error,
                        )
                    if self._on_rollback is not None:
                        try:
                            self._on_rollback()
                        except Exception as rollback_error:
                            rollback_errors.append(f"dependent services: {rollback_error}")
                            logger.error(
                                "Operation-mode rollback could not refresh dependent services: %s",
                                rollback_error,
                            )
                if old_config is not None:
                    try:
                        if config_existed:
                            self._write_config(self._with_mode(old_config, old_mode))
                        else:
                            self._config_path.unlink(missing_ok=True)
                    except Exception as rollback_error:
                        rollback_errors.append(f"persisted config: {rollback_error}")
                        logger.error(
                            "Operation-mode rollback could not restore config.json: %s",
                            rollback_error,
                        )
                if target_created:
                    try:
                        target.unlink()
                    except OSError:
                        rollback_errors.append("target cleanup failed")
                persisted_mode = self._read_persisted_mode()
                rollback_completed = not rollback_errors and (
                    persisted_mode is self.active_mode
                )
                recovery_required = not rollback_completed or (
                    persisted_mode is not self.active_mode
                )
                if recovery_required:
                    message = (
                        "Operation mode transition failed and rollback is incomplete. "
                        f"Runtime mode is {self.active_mode.value}; persisted mode is "
                        f"{persisted_mode.value if persisted_mode else 'unknown'}. "
                        "Restart or manual recovery is required. "
                        f"Cause: {error}"
                    )
                else:
                    message = f"Operation mode transition failed: {error}"
                return OperationModeResult(
                    accepted=False,
                    active_mode=self.active_mode,
                    generation=self._repository.snapshot().generation,
                    message=message,
                    target_description=target_description,
                    persisted_mode=persisted_mode,
                    rollback_completed=rollback_completed,
                    recovery_required=recovery_required,
                )

    def status(self) -> OperationModeResult:
        """Return confirmed daemon mode without inferring it from a path."""
        with self._lock, settings_transaction_lock(self._config_path):
            mode = self.active_mode
            persisted = self._read_persisted_mode()
            if persisted is not mode:
                return OperationModeResult(
                    accepted=False,
                    active_mode=mode,
                    generation=self._repository.snapshot().generation,
                    message=(
                        "The daemon runtime and persisted operation modes differ "
                        f"(runtime={mode.value}, persisted="
                        f"{persisted.value if persisted else 'unknown'}). "
                        "Restart or manual recovery is required."
                    ),
                    target_description=self._description(mode),
                    persisted_mode=persisted,
                    rollback_completed=False,
                    recovery_required=True,
                )
            return OperationModeResult(
                accepted=True,
                active_mode=mode,
                generation=self._repository.snapshot().generation,
                target_description=self._description(mode),
                persisted_mode=persisted,
            )

    def _read_persisted_mode(self) -> Optional[OperationMode]:
        # Missing config.json is not equivalent to a healthy DEFAULT setting.
        # Callers must be able to distinguish an absent canonical settings
        # tree and persist it before claiming that runtime and disk agree.
        if not self._config_path.exists():
            return None
        try:
            value = self._read_config().get("ssh", {}).get("use_isolated_config", False)
        except Exception:
            return None
        if type(value) is not bool:
            return None
        return OperationMode.ISOLATED if value else OperationMode.DEFAULT

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
        # Read without the generic loader's obsolete-file backup side effect.
        # A transition must be able to restore the exact pre-transaction state
        # if publication or a dependent-service hook fails.  Missing files are
        # represented by the complete canonical current-version tree, which is
        # then persisted on a successful transition.
        if not self._config_path.exists():
            return get_default_config()
        try:
            with self._config_path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return get_default_config()
        if not isinstance(value, dict):
            raise ValueError("daemon configuration is not an object")
        if "config_version" not in value:
            value["config_version"] = CONFIG_VERSION
        value, _updated = ensure_config_defaults(value)
        return value

    @staticmethod
    def _with_mode(config: dict, mode: OperationMode) -> dict:
        result = dict(config)
        ssh = dict(result.get("ssh") or {})
        ssh["use_isolated_config"] = mode is OperationMode.ISOLATED
        result["ssh"] = ssh
        return result

    def _write_config(self, config: dict) -> None:
        save_settings(self._config_path, config)

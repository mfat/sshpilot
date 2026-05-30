"""Session management for sshPilot.

This module provides :class:`SessionManager`, which stores named "sessions"
(snapshots of the set of open tabs) and a single auto-captured "previous"
session.  Sessions are persisted to a dedicated JSON file in the user's
configuration directory so the main ``config.json`` stays uncluttered.

A session payload is a plain dict of the form::

    {
        "tabs": [
            {"type": "ssh", "nickname": "my-server", "custom_title": null},
            {"type": "local"},
            {
                "type": "split",
                "layout": "horizontal",
                "custom_title": null,
                "panes": [
                    [{"nickname": "server1"}, {"nickname": "server2"}],
                    [{"nickname": "server3"}]
                ]
            }
        ]
    }
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)

SESSIONS_FILENAME = "sessions.json"


class SessionManager:
    """Manages named tab sessions and the auto-captured previous session."""

    def __init__(self, config=None):
        # ``config`` is accepted for symmetry with the other managers but the
        # session store uses its own JSON file rather than the shared config.
        self.config = config
        self.sessions: Dict[str, dict] = {}
        self.previous: Optional[dict] = None
        self._path = os.path.join(get_config_dir(), SESSIONS_FILENAME)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load sessions from disk, tolerating a missing or invalid file."""
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}

            raw_sessions = data.get("sessions", {})
            if isinstance(raw_sessions, dict):
                self.sessions = {
                    str(name): payload
                    for name, payload in raw_sessions.items()
                    if isinstance(payload, dict)
                }

            raw_previous = data.get("previous")
            if isinstance(raw_previous, dict):
                self.previous = raw_previous
        except Exception as exc:
            logger.error(f"Failed to load sessions from {self._path}: {exc}")
            self.sessions = {}
            self.previous = None

    def _save(self) -> None:
        """Persist sessions to disk."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            payload = {
                "sessions": self.sessions,
                "previous": self.previous,
            }
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as exc:
            logger.error(f"Failed to save sessions to {self._path}: {exc}")

    # ── named sessions ─────────────────────────────────────────────────────────

    def list_session_names(self) -> List[str]:
        """Return saved session names sorted case-insensitively."""
        return sorted(self.sessions.keys(), key=str.lower)

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def get_session(self, name: str) -> Optional[dict]:
        return self.sessions.get(name)

    def save_session(self, name: str, data: dict) -> None:
        """Create or overwrite a named session."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Session name must not be empty")
        if not isinstance(data, dict):
            raise TypeError("Session data must be a dict")
        self.sessions[name] = data
        self._save()
        logger.info(f"Saved session '{name}' ({len(data.get('tabs', []))} tabs)")

    def delete_session(self, name: str) -> bool:
        """Remove a named session. Returns True if it existed."""
        if name in self.sessions:
            del self.sessions[name]
            self._save()
            logger.info(f"Deleted session '{name}'")
            return True
        return False

    # ── previous session (auto captured on quit) ──────────────────────────────

    def save_previous(self, data: dict) -> None:
        """Store the auto-captured previous session."""
        if not isinstance(data, dict):
            return
        self.previous = data
        self._save()

    def get_previous(self) -> Optional[dict]:
        return self.previous

"""Daemon-backed mutable services for GTK plugins.

The connection presentation store stays read-only. This adapter supplies the
legacy synchronous plugin contract while routing every mutation and secret
operation through the daemon client that owns persistence.
"""

from __future__ import annotations

from threading import RLock

from sshpilot.api.connection_identity import connection_id_for
from sshpilot.api.models import (
    CreateConnectionRequest,
    EDITABLE_CONFIG_FIELDS,
    UNSET,
    StoreConnectionPasswordRequest,
    UpdateConnectionRequest,
)
from sshpilot.api.models.connections import extract_plugin_data


# The columns a connection carries outside its config patch.  Narrower than
# ``CONNECTION_CORE_DATA_FIELDS`` on purpose: an SSH config patch may legally
# carry ``aliases``, which is not a plugin FieldSpec value.
_CORE_FIELDS = frozenset({"nickname", "hostname", "host", "username", "port", "protocol"})


class DaemonConnectionServices:
    """Mutable daemon facade paired with a read-only presentation projection."""

    def __init__(self, projection) -> None:
        self._projection = projection
        self._client = None
        self._lock = RLock()

    def attach_client(self, client) -> None:
        with self._lock:
            self._client = client

    def detach_client(self) -> None:
        with self._lock:
            self._client = None

    def _require_client(self):
        with self._lock:
            client = self._client
        if client is None:
            raise RuntimeError("The daemon connection service is unavailable")
        return client

    @property
    def connections(self):
        return self._projection.connections

    def get_connections(self):
        return self._projection.get_connections()

    def find_connection_by_nickname(self, nickname):
        return self._projection.find_connection_by_nickname(nickname)

    def get_connection_by_id(self, connection_id):
        return self._projection.get_connection_by_id(connection_id)

    get_connection_by_uuid = get_connection_by_id

    def connect_after(self, signal_name, callback):
        return self._projection.connect_after(signal_name, callback)

    def disconnect(self, handler_id):
        return self._projection.disconnect(handler_id)

    @staticmethod
    def _plugin_data(protocol, data):
        # Delegate to the daemon's own projection filter so the write side can
        # never accept a key the read side strips (which would reopen blank
        # and then be saved back empty).
        return extract_plugin_data(protocol, data)

    @staticmethod
    def _config_patch(data):
        return {
            key: value
            for key, value in dict(data).items()
            if key in EDITABLE_CONFIG_FIELDS and key not in _CORE_FIELDS
        }

    def add_connection_from_data(self, data):
        client = self._require_client()
        values = dict(data)
        protocol = str(values.get("protocol", "ssh") or "ssh")
        request = CreateConnectionRequest(
            nickname=str(values.get("nickname", "") or ""),
            hostname=str(values.get("hostname", values.get("host", "")) or ""),
            username=str(values.get("username", "") or ""),
            port=int(values.get("port", 22) or 22),
            protocol=protocol,
            display_name=str(values.get("display_name", "") or ""),
            config_patch=self._config_patch(values) if protocol == "ssh" else {},
            plugin_data=self._plugin_data(protocol, values),
        )
        result = client.create_connection(request)
        password = values.get("password")
        if protocol == "ssh" and password:
            client.store_connection_password(
                StoreConnectionPasswordRequest(
                    connection_id=result.connection_id,
                    password=str(password),
                )
            )
        return client.get_connection(result.connection_id)

    create_connection = add_connection_from_data

    def update_connection(self, connection, data):
        client = self._require_client()
        values = dict(data)
        protocol = str(
            values.get("protocol", getattr(connection, "protocol", "ssh")) or "ssh"
        )
        request = UpdateConnectionRequest(
            nickname=values.get("nickname", getattr(connection, "nickname", "")),
            hostname=values.get(
                "hostname",
                values.get("host", getattr(connection, "hostname", "")),
            ),
            username=values.get("username", getattr(connection, "username", "")),
            port=int(values.get("port", getattr(connection, "port", 22)) or 22),
            display_name=(values["display_name"] if "display_name" in values else UNSET),
            config_patch=self._config_patch(values) if protocol == "ssh" else {},
            plugin_data=self._plugin_data(protocol, values),
        )
        connection_id = connection_id_for(connection)
        client.update_connection(connection_id, request)
        password = values.get("password")
        if protocol == "ssh" and password:
            client.store_connection_password(
                StoreConnectionPasswordRequest(
                    connection_id=connection_id,
                    password=str(password),
                )
            )
        return True

    def store_plugin_secret(self, plugin_id, key, value):
        return self._require_client().store_plugin_secret(plugin_id, key, value)

    def get_plugin_secret(self, plugin_id, key):
        return self._require_client().get_plugin_secret(plugin_id, key)

    def delete_plugin_secret(self, plugin_id, key):
        return self._require_client().delete_plugin_secret(plugin_id, key)

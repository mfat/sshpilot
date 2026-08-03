from sshpilot.api.models.connections import StoreConnectionPasswordRequest, DeleteConnectionPasswordRequest

class GtkCredentialService:
    def __init__(self, daemon_client):
        self._client = daemon_client

    def store_connection_password(self, connection, password: str) -> bool:
        req = StoreConnectionPasswordRequest(connection_id=connection.id, password=password)
        try:
            return self._client.store_connection_password(req)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to store connection password: {e}")
            return False

    def delete_connection_password(self, connection) -> bool:
        req = DeleteConnectionPasswordRequest(connection_id=connection.id)
        try:
            return self._client.delete_connection_password(req)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to delete connection password: {e}")
            return False

class GtkPluginSecretService:
    def __init__(self, daemon_client):
        self._client = daemon_client

    def get(self, plugin_id: str, key: str):
        try:
            return self._client.get_plugin_secret(plugin_id, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to get plugin secret: {e}")
            return None

    def set(self, plugin_id: str, key: str, value: str):
        try:
            return self._client.store_plugin_secret(plugin_id, key, value)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to store plugin secret: {e}")
            return False

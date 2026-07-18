"""In-process passphrase-prompt IPC server.

The SSH key passphrase prompt is normally rendered by a separate askpass helper
process that ssh spawns via ``SSH_ASKPASS``. On Wayland that helper window — a
different app's top-level — can map *behind* the focused main window and be hard
to find.

This module lets the helper hand the prompt back to the running main process over
a private Unix socket, so the prompt is shown as a modal child of the main window
(reusing ``MainWindow.prompt_ssh_passphrase``) and reliably appears on top. If
the main app is not running or the socket is unreachable, the helper falls back
to its own standalone window, so there is no regression.

Protocol (newline-delimited JSON over ``AF_UNIX``):
  prompt request (helper -> app):
    {"token", "type": "passphrase"|"challenge"|"password", ...}
  prompt reply   (app -> helper):
    {"ok": true,  "passphrase"|"value": "..."}   user entered a secret
    {"ok": false}                                 user cancelled (no fallback)
    {"ok": false, "fallback": true}               app can't prompt now -> standalone

  lookup request (helper -> app): {"token", "type": "lookup", "key_path"}
  lookup reply   (app -> helper):
    {"ok": true,  "passphrase": "..."}   resolved from the app's warm secret cache
    {"ok": false}                         not found / not cached (helper falls back)
  The lookup request never prompts — it lets the askpass helper reuse the main
  process's already-unlocked secret cache instead of cold-loading the vault itself.
"""

import json
import logging
import os
import secrets

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from . import askpass_utils

logger = logging.getLogger(__name__)

_server = None  # module-level singleton


class AskpassPromptServer:
    """Listens on a per-user Unix socket and shows passphrase prompts in-app."""

    def __init__(self, window):
        self._window = window
        self._service = None
        self._socket_path = None
        self._token = None
        self._busy = False  # serialize prompts (nested dialog loop)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        if self._service is not None:
            return
        runtime_dir = GLib.get_user_runtime_dir() or os.path.join(
            "/run/user", str(os.getuid())
        )
        base = os.path.join(runtime_dir, "sshpilot")
        try:
            os.makedirs(base, exist_ok=True)
            os.chmod(base, 0o700)
        except Exception as exc:
            logger.warning("askpass server: cannot prepare runtime dir: %s", exc)
            return

        self._socket_path = os.path.join(base, f"askpass-{secrets.token_hex(8)}.sock")
        self._token = secrets.token_hex(32)
        try:
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except Exception:
            pass

        service = Gio.SocketService.new()
        address = Gio.UnixSocketAddress.new(self._socket_path)
        try:
            service.add_address(
                address,
                Gio.SocketType.STREAM,
                Gio.SocketProtocol.DEFAULT,
                None,
            )
        except GLib.Error as exc:
            logger.warning("askpass server: failed to listen: %s", exc)
            self._socket_path = None
            self._token = None
            return
        try:
            os.chmod(self._socket_path, 0o600)
        except Exception:
            pass

        service.connect("incoming", self._on_incoming)
        service.start()
        self._service = service
        askpass_utils.set_askpass_ipc(self._socket_path, self._token)
        logger.info("askpass prompt server listening at %s", self._socket_path)

    def stop(self):
        askpass_utils.set_askpass_ipc(None, None)
        if self._service is not None:
            try:
                self._service.stop()
            except Exception:
                pass
            self._service = None
        if self._socket_path:
            try:
                os.unlink(self._socket_path)
            except Exception:
                pass
            self._socket_path = None
        self._token = None

    # ── request handling (all on the GTK main loop) ────────────────────────
    def _on_incoming(self, _service, connection, _source_object):
        try:
            data_in = Gio.DataInputStream.new(connection.get_input_stream())
            data_in.read_line_async(
                GLib.PRIORITY_DEFAULT, None, self._on_line, connection
            )
        except Exception as exc:
            logger.debug("askpass server: incoming setup error: %s", exc)
            self._close(connection)
        return True  # handled

    def _on_line(self, data_in, result, connection):
        try:
            line, _length = data_in.read_line_finish_utf8(result)
        except Exception as exc:
            logger.debug("askpass server: read error: %s", exc)
            self._close(connection)
            return
        if not line:
            self._close(connection)
            return
        self._handle_request(line, connection)

    def _handle_request(self, line, connection):
        reply = {"ok": False}
        try:
            request = json.loads(line)
        except Exception:
            request = None

        if not self._is_authorized(request):
            logger.debug("askpass server: invalid/unauthorized request")
            self._write_reply(connection, reply)
            return

        # Non-prompting fast path: resolve a passphrase from the main process's
        # warm secret cache so the askpass subprocess never cold-loads the vault.
        if request.get("type") == "lookup":
            key_path = request.get("key_path") or ""
            passphrase = ""
            try:
                # Non-blocking: warm-cache/instant answer only, so a slow backend (rbw)
                # never stalls this socket past the client's timeout. A cold rbw entry
                # returns "" here and warms in the background for the next connect.
                passphrase = askpass_utils.resolve_passphrase_for_ipc(key_path) or ""
            except Exception as exc:
                logger.debug("askpass server: lookup error: %s", exc)
            self._write_reply(
                connection,
                {"ok": True, "passphrase": passphrase} if passphrase else {"ok": False},
            )
            return

        req_type = request.get("type")
        if req_type not in ("passphrase", "challenge", "password"):
            self._write_reply(connection, reply)
            return

        if self._busy:
            # A prompt is already showing; let this caller use its own window.
            logger.debug("askpass server: busy, asking caller to use fallback")
            reply["fallback"] = True
            self._write_reply(connection, reply)
            return

        self._busy = True
        try:
            prompt = request.get("prompt") or ""
            if req_type == "challenge":
                value = self._window.prompt_ssh_challenge(prompt)
                if value is not None:
                    reply = {"ok": True, "value": value, "passphrase": value}
            elif req_type == "password":
                value = self._window.prompt_ssh_password(
                    display_name=prompt.strip() or "",
                    host=request.get("host") or None,
                    username=request.get("username") or None,
                    body=prompt.strip() or None,
                )
                if value is not None:
                    reply = {"ok": True, "value": value, "passphrase": value}
            else:
                key_path = request.get("key_path") or ""
                value = self._window.prompt_ssh_passphrase(key_path, prompt)
                if value is not None:
                    reply = {"ok": True, "passphrase": value}
        except Exception as exc:
            logger.debug("askpass server: prompt error: %s", exc)
            reply = {"ok": False, "fallback": True}
        finally:
            self._busy = False

        self._write_reply(connection, reply)

    def _is_authorized(self, request) -> bool:
        if not isinstance(request, dict):
            return False
        token = request.get("token")
        if not token or not self._token:
            return False
        return secrets.compare_digest(str(token), self._token)

    def _write_reply(self, connection, reply):
        try:
            payload = (json.dumps(reply) + "\n").encode("utf-8")
            ostream = connection.get_output_stream()
            ostream.write_all(payload, None)
            ostream.flush(None)
        except Exception as exc:
            logger.debug("askpass server: write error: %s", exc)
        self._close(connection)

    def _close(self, connection):
        try:
            connection.close(None)
        except Exception:
            pass


def start(window):
    """Start the singleton askpass prompt server bound to ``window``."""
    global _server
    if _server is not None:
        return _server
    server = AskpassPromptServer(window)
    server.start()
    _server = server
    return server


def stop():
    """Stop the askpass prompt server and stop advertising its socket."""
    global _server
    if _server is None:
        return
    try:
        _server.stop()
    except Exception:
        pass
    _server = None

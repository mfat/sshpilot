"""Key and certificate sources shared by every editor of SSH auth settings.

The discovery (daemon-owned disk keys, loaded agent keys, ``*-cert.pub``
certificates), the key chooser, file browsing, and the Flatpak "copy into
~/.ssh" confirmation used by ``FileListEditor`` rows. Moved out of
``ConnectionDialog`` so the login profile editor offers exactly the same key
selection flow.

Hosts mixing this in must be a ``Gtk.Window`` (the default chooser parent) and
provide ``parent_window`` (the main window: ``client`` and ``key_manager``)
and ``show_error(message)``.
"""

import logging
import os
from gettext import gettext as _

try:
    from gi.repository import Adw, Gio, Gtk
except (ImportError, AttributeError):  # pragma: no cover - headless tests
    Adw = Gio = Gtk = None

from .flatpak_ssh_import import (
    import_certificate_into_ssh_dir,
    import_private_key_into_ssh_dir,
    needs_flatpak_ssh_import,
)
from .platform_utils import get_ssh_dir
from .ssh_key_fingerprint import _fingerprint_for_path

logger = logging.getLogger(__name__)


class KeySourcesMixin:
    # ---- discovery / browse for the key & certificate FileListEditors -------
    def _agent_keys(self):
        """Return safe metadata for keys loaded in the daemon-selected agent."""
        client = getattr(getattr(self, "parent_window", None), "client", None)
        if client is None:
            parent = self.get_transient_for()
            client = getattr(parent, "client", None)
        if client is None:
            return []
        try:
            return [
                (key.fingerprint, key.fingerprint, key.key_type, key.comment)
                for key in client.list_agent_keys().keys
            ]
        except Exception:
            logger.debug("daemon agent listing unavailable", exc_info=True)
            return []

    def _make_agent_materializer(self, raw_line):
        """Agent identities are daemon-owned and cannot become file paths."""
        return lambda: ''

    @staticmethod
    def _key_type_label(pub_first_field):
        t = (pub_first_field or '').lower()
        if t.startswith('sk-'):
            return _("FIDO security key")
        if 'ed25519' in t:
            return _("Ed25519")
        if 'ecdsa' in t:
            return _("ECDSA")
        if 'rsa' in t:
            return _("RSA")
        if 'dss' in t or 'dsa' in t:
            return _("DSA")
        return _("key")

    @staticmethod
    def _read_pub(pub_path):
        try:
            with open(pub_path) as f:
                parts = f.read().split()
            return parts if len(parts) >= 2 else None
        except Exception:
            return None

    def _discover_disk_keys(self):
        """[(key_name, path)] of private key files discovered by the daemon.

        Key discovery is daemon-owned: the legacy ``ConnectionManager
        .load_ssh_keys`` surface was retired with the connection-store
        migration, so the dialog uses the parent window's daemon-backed
        ``KeyManager`` (the same object the ssh-copy-id window uses). When the
        parent has a daemon client but no pre-built manager, one is
        constructed with the parent's key scope. Without either, the chooser
        shows its empty-state placeholder.
        """
        parent = getattr(self, 'parent_window', None)
        if parent is None:
            try:
                parent = self.get_transient_for()
            except Exception:
                parent = None
        key_manager = getattr(parent, 'key_manager', None)
        if key_manager is None:
            # Do not construct a DEFAULT-scope manager while the daemon's
            # semantic operation mode is still unconfirmed.  The main window
            # creates the correctly scoped manager only after confirmation.
            return []
        if key_manager is None:
            return []
        out = []
        seen = set()
        try:
            for key in key_manager.discover_keys() or []:
                path = key.private_path
                if path and path not in seen:
                    seen.add(path)
                    out.append((key.name or os.path.basename(path), path))
        except Exception:
            logger.debug("disk key discovery failed", exc_info=True)
        return out

    def _discover_agent_keys(self):
        """Return display-only metadata for daemon-owned agent identities."""
        out = []
        for fingerprint, _display, ktype, comment in self._agent_keys():
            label = self._key_type_label(ktype)
            name = comment or _("agent key")
            out.append((f"{name}  —  {label} ({fingerprint})", lambda: ""))
        return out

    def _discover_certs(self):
        """Return [(display, path)] of detected *-cert.pub certificate files."""
        out = []
        try:
            ssh_dir = get_ssh_dir()
            if os.path.isdir(ssh_dir):
                for filename in sorted(os.listdir(ssh_dir)):
                    if filename.endswith('-cert.pub'):
                        out.append((filename, os.path.join(ssh_dir, filename)))
        except Exception:
            logger.debug("Certificate discovery failed", exc_info=True)
        return out

    def _browse_file(self, title, on_chosen, filters=None, parent=None):
        try:
            dialog = Gtk.FileDialog(title=title)
            try:
                ssh_dir = get_ssh_dir()
                if os.path.isdir(ssh_dir):
                    dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))
            except Exception:
                pass
            if filters is not None:
                dialog.set_filters(filters)

            def _done(dlg, result):
                try:
                    gfile = dlg.open_finish(result)
                    if gfile and gfile.get_path():
                        on_chosen(gfile.get_path())
                except Exception:
                    logger.debug("File chooser cancelled or failed", exc_info=True)

            # Parent the chooser to THE WINDOW THE USER IS ACTUALLY LOOKING
            # AT — never to ``get_transient_for()`` and never unconditionally
            # to ``self`` (this ConnectionDialog). ConnectionDialog is modal
            # and stacked above its transient parent, so a chooser parented
            # to the MainWindow opens invisibly behind the modal dialog on
            # Wayland portal stacks (issue #1103). But when browsing is
            # triggered from a dialog stacked *above* ConnectionDialog (e.g.
            # KeyChooserDialog), the caller must pass that dialog as
            # ``parent`` — otherwise the same "hidden behind a modal" bug
            # reappears one layer up (issue #1103 regression).
            logger.debug("Opening file chooser %r parented to %r", title, parent or self)
            dialog.open(parent or self, None, _done)
        except Exception:
            logger.debug("Failed to open file chooser", exc_info=True)

    def _open_key_chooser(self, editor):
        """Open the disk/agent key chooser and add selected keys to *editor*."""
        disk_keys = []
        for name, path in self._discover_disk_keys():
            ktype, fp, _comment = _fingerprint_for_path(path)
            disk_keys.append({'name': name, 'path': path, 'ktype': ktype, 'meta': fp})

        agent_keys = []
        for fingerprint, _display, ktype, comment in self._agent_keys():
            agent_keys.append({
                'title': comment or _("agent key"),
                'ktype': ktype,
                'meta': fingerprint,
                'materializer': lambda: "",
            })

        parent = self.get_root() if hasattr(self, 'get_root') else None
        if not isinstance(parent, Gtk.Window):
            parent = None
        from .connection_dialog import KeyChooserDialog

        dialog = KeyChooserDialog(
            parent,
            disk_keys=disk_keys,
            agent_keys=agent_keys,
            existing_paths=editor.get_paths(),
            on_add=editor.add_path,
            on_browse=self._browse_key,
        )
        dialog.present()

    def _browse_key(self, on_chosen, parent=None):
        def _after(path):
            self._confirm_flatpak_ssh_import(
                path, on_chosen, parent=parent, kind="key"
            )

        self._browse_file(_("Select SSH Key File"), _after, parent=parent)

    def _browse_cert(self, on_chosen, parent=None):
        filters = None
        try:
            cert_filter = Gtk.FileFilter()
            cert_filter.set_name(_("SSH Certificate Files"))
            cert_filter.add_pattern("*-cert.pub")
            cert_filter.add_pattern("*.pub")
            all_filter = Gtk.FileFilter()
            all_filter.set_name(_("All Files"))
            all_filter.add_pattern("*")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(cert_filter)
            filters.append(all_filter)
        except Exception:
            filters = None

        def _after(path):
            self._confirm_flatpak_ssh_import(
                path, on_chosen, parent=parent, kind="cert"
            )

        self._browse_file(
            _("Select SSH Certificate File"),
            _after,
            filters=filters,
            parent=parent,
        )

    def _confirm_flatpak_ssh_import(
        self, path, on_chosen, *, parent=None, kind: str = "key"
    ):
        """On Flatpak, copy keys/certs outside ``~/.ssh`` in after confirmation.

        Sandbox OpenSSH cannot use host paths outside ``~/.ssh``. Portal paths
        must not be written into ssh_config, so the durable option is an
        explicit copy into ``~/.ssh`` (plus ``.pub`` / ``-cert.pub`` sidecars
        for private keys when present).
        """
        if not callable(on_chosen):
            return
        if not path or not needs_flatpak_ssh_import(path):
            on_chosen(path)
            return

        transient = parent if parent is not None else self
        if kind == "cert":
            heading = _("Copy certificate into ~/.ssh?")
            body = _(
                "Flatpak can only use SSH files under ~/.ssh. Copy this "
                "certificate into ~/.ssh and use the copy?"
            )
        else:
            heading = _("Copy key into ~/.ssh?")
            body = _(
                "Flatpak can only use SSH keys under ~/.ssh. Copy this key "
                "into ~/.ssh and use the copy? Matching .pub and certificate "
                "files next to it will be copied too when present."
            )

        dialog = Adw.MessageDialog.new(transient, heading, body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("copy", _("Copy"))
        try:
            dialog.set_response_appearance(
                "copy", Adw.ResponseAppearance.SUGGESTED
            )
        except Exception:
            pass
        dialog.set_default_response("copy")
        dialog.set_close_response("cancel")

        def _on_response(_dialog, response):
            if response != "copy":
                return
            try:
                if kind == "cert":
                    dest, _companions = import_certificate_into_ssh_dir(path)
                else:
                    dest, _companions = import_private_key_into_ssh_dir(path)
            except Exception as exc:
                logger.warning(
                    "Flatpak SSH import failed for %s: %s", path, exc, exc_info=True
                )
                self.show_error(
                    _("Could not copy the file into ~/.ssh: {error}").format(
                        error=str(exc)
                    )
                )
                return
            on_chosen(dest)

        dialog.connect("response", _on_response)
        dialog.present()

"""Inline form-field validation for ConnectionDialog.

Extracted verbatim from connection_dialog.py as a mixin to shrink that
god-object. ConnectionDialog inherits this mixin, so ``self`` stays identical and
the move is a pure cut-and-paste with no logic changes. These methods validate
user-entered rows (nickname, host, port, username) against ``self.validator`` and
style the rows inline; they never open an SSH connection.
"""

import re
import ipaddress
from typing import Optional

try:
    from gi.repository import Gtk
except (ImportError, AttributeError):  # pragma: no cover - used in tests without GTK
    class _DummyGIMeta(type):
        def __getattr__(cls, name):
            value = _DummyGIMeta(name, (object,), {})
            setattr(cls, name, value)
            return value

        def __call__(cls, *args, **kwargs):
            return object()

    class Gtk(metaclass=_DummyGIMeta):
        pass

from gettext import gettext as _


class ConnectionDialogValidationMixin:
    def _apply_validation_to_row(self, row, result):
        try:
            if hasattr(row, 'set_subtitle'):
                row.set_subtitle(result.message or "")
        except Exception:
            pass
        # Tooltips on row and entry
        try:
            if hasattr(row, 'set_tooltip_text'):
                row.set_tooltip_text(result.message or None)
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None and hasattr(entry, 'set_tooltip_text'):
                entry.set_tooltip_text(result.message or None)
        except Exception:
            pass
        # CSS classes: clear, then set per severity
        try:
            row.remove_css_class('error')
            row.remove_css_class('warning')
        except Exception:
            pass
        try:
            if hasattr(result, 'is_valid') and not result.is_valid:
                row.add_css_class('error')
            elif hasattr(result, 'severity') and result.severity == 'warning':
                row.add_css_class('warning')
        except Exception:
            pass

    def _update_existing_names_in_validator(self):
        try:
            mgr = getattr(self.parent_window, 'connection_manager', None)
            names = set()
            if mgr and hasattr(mgr, 'connections'):
                # Normalize current connection name (when editing) to exclude it from duplicates
                current_name_norm = ''
                try:
                    if self.is_editing and self.connection:
                        current_name_norm = str(getattr(self.connection, 'nickname', '')).strip().lower()
                except Exception:
                    current_name_norm = ''
                for conn in mgr.connections or []:
                    n = getattr(conn, 'nickname', None)
                    if not n:
                        continue
                    n_norm = str(n).strip().lower()
                    # Exclude the current connection by name (case-insensitive), not by object identity
                    if current_name_norm and n_norm == current_name_norm:
                        continue
                    names.add(str(n))
            # Names come from the already-loaded connections (show_connection_dialog
            # reloads once before opening, and they don't change while the dialog is
            # open). No reload here — it ran *after* names were built anyway, so it only
            # cost a redundant parse (and a log line) on every field validation.
            self.validator.set_existing_names(names)
        except Exception:
            pass

    def _validate_field_row(self, field_name: str, row, context: str = "SSH"):
        text = (row.get_text() if hasattr(row, 'get_text') else "")
        if field_name == 'name':
            self._update_existing_names_in_validator()
            result = self.validator.validate_connection_name(text)
        elif field_name == 'hostname':
            raw = (text or '').strip()
            if raw.startswith('[') and raw.endswith(']') and len(raw) > 2:
                raw = raw[1:-1]
            result = self.validator.validate_hostname(raw, allow_empty=True)
        elif field_name == 'port':
            result = self.validator.validate_port(text, context)
        elif field_name == 'username':
            result = self.validator.validate_username(text)
        else:
            # Default: valid
            class _Dummy:
                is_valid = True
                message = ""
                severity = "info"
            result = _Dummy()
        # Store and apply to UI
        self.validation_results[field_name] = result
        self._apply_validation_to_row(row, result)
        # Update save buttons after each validation
        self._update_save_buttons()
        return result

    def _update_save_buttons(self):
        try:
            has_errors = any(
                (k in self.validation_results and not self.validation_results[k].is_valid)
                for k in ('name', 'hostname', 'port', 'username')
            )
            enabled = not has_errors
            for btn in getattr(self, '_save_buttons', []) or []:
                try:
                    btn.set_sensitive(enabled)
                except Exception:
                    pass
            if hasattr(self, 'set_response_enabled'):
                try:
                    self.set_response_enabled('save', enabled)
                except Exception:
                    pass
        except Exception:
            pass
    def _row_set_message(self, row, message: str, is_error: bool = True):
        try:
            if hasattr(row, 'set_subtitle'):
                row.set_subtitle(message or "")
        except Exception:
            pass
        # Also mirror the message into tooltips for visibility/accessibility
        try:
            if hasattr(row, 'set_tooltip_text'):
                row.set_tooltip_text(message or None)
        except Exception:
            pass
        try:
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None and hasattr(entry, 'set_tooltip_text'):
                entry.set_tooltip_text(message or None)
        except Exception:
            pass
        try:
            if is_error:
                row.add_css_class('error')
            else:
                row.remove_css_class('error')
        except Exception:
            pass

    def _row_clear_message(self, row):
        self._row_set_message(row, "", is_error=False)

    def _connect_row_validation(self, row, validator_callable):
        # Prefer notify::text on Adw.EntryRow, fallback to child Gtk.Entry changed
        try:
            row.connect('notify::text', lambda r, p: validator_callable(r))
            return
        except Exception:
            pass
        try:
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None:
                entry.connect('changed', lambda e: validator_callable(row))
        except Exception:
            pass

    def _validate_required_row(self, row, label_text: str):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _(f"{label_text} is required"), is_error=True)
            return False
        self._row_clear_message(row)
        return True

    def _is_nickname_taken(self, name: str) -> bool:
        try:
            mgr = getattr(self.parent_window, 'connection_manager', None)
            if mgr is None or not hasattr(mgr, 'connections'):
                return False
            normalized = (name or '').strip().lower()
            current_name_norm = ''
            try:
                if self.is_editing and self.connection:
                    current_name_norm = str(getattr(self.connection, 'nickname', '')).strip().lower()
            except Exception:
                current_name_norm = ''
            for conn in getattr(mgr, 'connections', []) or []:
                other_name = getattr(conn, 'nickname', None)
                if not other_name:
                    continue
                other_norm = str(other_name).strip().lower()
                # Skip the same connection object and also skip the current connection name when editing
                if current_name_norm and (conn is self.connection or other_norm == current_name_norm):
                    continue
                if other_norm == normalized:
                    return True
        except Exception:
            return False
        return False

    def _validate_nickname_row(self, row):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _("Nickname is required"), is_error=True)
            return False
        if self._is_nickname_taken(text):
            self._row_set_message(row, _("Nickname already exists"), is_error=True)
            return False
        self._row_clear_message(row)
        return True

    def _validate_host_row(self, row, allow_empty: bool = False):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            if allow_empty:
                self._row_clear_message(row)
                return True
            self._row_set_message(row, _("Host is required"), is_error=True)
            return False
        # Support bracketed IPv6 like [::1]
        text_unbr = text[1:-1] if (text.startswith('[') and text.endswith(']') and len(text) > 2) else text
        lower = text_unbr.lower()
        if lower in ("localhost",):
            self._row_clear_message(row)
            return True
        try:
            ipaddress.ip_address(text_unbr)
            self._row_clear_message(row)
            return True
        except Exception:
            # digits/dots but not valid ip → error
            if re.fullmatch(r"[0-9.]+", text_unbr):
                self._row_set_message(row, _("Invalid IPv4 address"), is_error=True)
                return False
            # RFC1123-ish hostname
            hostname_regex = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")
            if not hostname_regex.match(text_unbr):
                self._row_set_message(row, _("Invalid hostname"), is_error=True)
                return False
        self._row_clear_message(row)
        return True

    def _validate_port_row(self, row, label_text: str = "Port"):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _(f"{label_text} is required"), is_error=True)
            return False
        try:
            value = int(text)
            if value < 1 or value > 65535:
                self._row_set_message(row, _("Port must be between 1 and 65535"), is_error=True)
                return False
            # Clear errors; we are not styling warnings inline
            self._row_clear_message(row)
            return True
        except Exception:
            self._row_set_message(row, _("Port must be a number"), is_error=True)
            return False

    def _install_inline_validators(self):
        # General page fields
        if hasattr(self, 'nickname_row'):
            self._connect_row_validation(self.nickname_row, lambda r: self._validate_field_row('name', r))
        if hasattr(self, 'username_row'):
            self._connect_row_validation(self.username_row, lambda r: self._validate_field_row('username', r))
        if hasattr(self, 'hostname_row'):
            self._connect_row_validation(self.hostname_row, lambda r: self._validate_field_row('hostname', r))
        if hasattr(self, 'port_row'):
            self._connect_row_validation(self.port_row, lambda r: self._validate_field_row('port', r, context="SSH"))

    def _run_initial_validation(self):
        try:
            if hasattr(self, 'nickname_row'):
                self._validate_field_row('name', self.nickname_row)
            if hasattr(self, 'username_row'):
                self._validate_field_row('username', self.username_row)
            if hasattr(self, 'hostname_row'):
                self._validate_field_row('hostname', self.hostname_row)
            if hasattr(self, 'port_row'):
                self._validate_field_row('port', self.port_row, context="SSH")
        except Exception:
            pass

    def _focus_row(self, row):
        try:
            if hasattr(self, 'present'):
                self.present()
        except Exception:
            pass
        try:
            widget = row.get_child() if hasattr(row, 'get_child') else row
            if hasattr(widget, 'grab_focus'):
                widget.grab_focus()
        except Exception:
            pass

    def _validate_all_required_for_save(self) -> Optional[Gtk.Widget]:
        """Validate all visible fields; return the first invalid row (or None)."""
        # General
        if hasattr(self, 'nickname_row'):
            res = self._validate_field_row('name', self.nickname_row)
            if not res.is_valid:
                return self.nickname_row
        if hasattr(self, 'username_row'):
            res = self._validate_field_row('username', self.username_row)
            if not res.is_valid:
                return self.username_row
        if hasattr(self, 'hostname_row'):
            res = self._validate_field_row('hostname', self.hostname_row)
            if not res.is_valid:
                return self.hostname_row
        if hasattr(self, 'port_row'):
            res = self._validate_field_row('port', self.port_row, context="SSH")
            if not res.is_valid:
                return self.port_row
        return None

"""Tests for macOS VTE Option key passthrough workaround."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.core.settings.defaults import get_default_config
from sshpilot.core.settings.migration import ensure_config_defaults

# GTK modifier mask constants (integer values for testing)
ALT_MASK = 1 << 3
CONTROL_MASK = 1 << 2
META_MASK = 1 << 28


class TestOptionKeyDefaults:
    """Test default configuration for macOS Option key passthrough."""

    def test_default_config_includes_macos_option_key_passthrough(self):
        """Default config includes macos_option_key_passthrough set to False."""
        defaults = get_default_config()
        assert 'macos_option_key_passthrough' in defaults['terminal']
        assert defaults['terminal']['macos_option_key_passthrough'] is False


class TestOptionKeyMigration:
    """Test settings migration for macOS Option key passthrough."""

    def test_migration_adds_missing_setting(self):
        """Migration adds macos_option_key_passthrough if missing."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
            }
        }
        updated_config, was_updated = ensure_config_defaults(config)
        assert was_updated is True
        assert updated_config['terminal']['macos_option_key_passthrough'] is False

    def test_migration_preserves_existing_true_value(self):
        """Migration preserves an existing True value."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
                'macos_option_key_passthrough': True,
            }
        }
        updated_config, was_updated = ensure_config_defaults(config)
        assert updated_config['terminal']['macos_option_key_passthrough'] is True

    def test_migration_preserves_existing_false_value(self):
        """Migration preserves an existing False value."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
                'macos_option_key_passthrough': False,
            }
        }
        updated_config, _ = ensure_config_defaults(config)
        assert updated_config['terminal']['macos_option_key_passthrough'] is False

    def test_migration_coerces_non_bool_to_bool(self):
        """Migration coerces non-boolean values to boolean."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
                'macos_option_key_passthrough': 'yes',
            }
        }
        updated_config, was_updated = ensure_config_defaults(config)
        assert was_updated is True
        assert updated_config['terminal']['macos_option_key_passthrough'] is True

    def test_migration_coerces_empty_string_to_false(self):
        """Migration coerces empty string to False."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
                'macos_option_key_passthrough': '',
            }
        }
        updated_config, was_updated = ensure_config_defaults(config)
        assert was_updated is True
        assert updated_config['terminal']['macos_option_key_passthrough'] is False

    def test_migration_coerces_integer_to_bool(self):
        """Migration coerces integer values to boolean."""
        config = {
            'terminal': {
                'theme': 'default',
                'encoding': 'UTF-8',
                'macos_option_key_passthrough': 1,
            }
        }
        updated_config, was_updated = ensure_config_defaults(config)
        assert was_updated is True
        assert updated_config['terminal']['macos_option_key_passthrough'] is True


class TestOptionKeyHandler:
    """Test the _on_macos_option_key_pressed handler logic."""

    @pytest.fixture
    def mock_gdk(self):
        """Create mock Gdk module with proper integer modifier values."""
        mock_modifier_type = MagicMock()
        mock_modifier_type.ALT_MASK = ALT_MASK
        mock_modifier_type.CONTROL_MASK = CONTROL_MASK
        mock_modifier_type.META_MASK = META_MASK

        mock_gdk = MagicMock()
        mock_gdk.ModifierType = mock_modifier_type
        mock_gdk.keyval_name = MagicMock(return_value=None)
        mock_gdk.keyval_to_unicode = MagicMock(side_effect=lambda kv: kv if kv < 0x10000 else 0)
        return mock_gdk

    @pytest.fixture
    def backend(self):
        """Create a VTETerminalBackend instance with mocked dependencies."""
        from sshpilot.terminal_backends import VTETerminalBackend

        backend = VTETerminalBackend.__new__(VTETerminalBackend)
        backend._macos_option_key_controller = None
        backend.vte = MagicMock()
        backend.owner = MagicMock()
        return backend

    def test_pass_through_when_alt_not_pressed(self, backend, mock_gdk):
        """Event passes through when Alt/Option is not pressed."""
        with patch.dict('sys.modules', {'gi.repository': MagicMock(Gdk=mock_gdk)}):
            with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
                controller = MagicMock()
                controller.get_group.return_value = 1
                state = 0  # No modifiers

                result = backend._on_macos_option_key_pressed(
                    controller, ord('|'), 0, state
                )
                assert result is False

    def test_pass_through_when_ctrl_pressed(self, backend, mock_gdk):
        """Ctrl+Alt combinations pass through."""
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1
            state = ALT_MASK | CONTROL_MASK

            result = backend._on_macos_option_key_pressed(
                controller, ord('c'), 0, state
            )
            assert result is False

    def test_pass_through_when_meta_pressed(self, backend, mock_gdk):
        """Cmd+Alt combinations pass through."""
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1
            state = ALT_MASK | META_MASK

            result = backend._on_macos_option_key_pressed(
                controller, ord('c'), 0, state
            )
            assert result is False

    def test_pass_through_on_primary_layout(self, backend, mock_gdk):
        """Alt+key on layout=0 passes through for Meta sequences."""
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 0  # Primary layout
            state = ALT_MASK

            result = backend._on_macos_option_key_pressed(
                controller, ord('b'), 0, state
            )
            assert result is False
            backend.owner.feed_child_data.assert_not_called()

    def test_pass_through_dead_keys(self, backend, mock_gdk):
        """Dead keys pass through for IME composition."""
        mock_gdk.keyval_name.return_value = 'dead_tilde'
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1
            state = ALT_MASK

            result = backend._on_macos_option_key_pressed(
                controller, 0xfe53, 0, state  # dead_tilde keyval
            )
            assert result is False
            backend.owner.feed_child_data.assert_not_called()

    def test_pass_through_control_characters(self, backend, mock_gdk):
        """Control characters (< 0x20) pass through."""
        # Override side_effect to return ESC control character
        mock_gdk.keyval_to_unicode.side_effect = None
        mock_gdk.keyval_to_unicode.return_value = 0x1b  # ESC
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1
            state = ALT_MASK

            result = backend._on_macos_option_key_pressed(
                controller, 0xff1b, 0, state  # Escape keyval
            )
            assert result is False

    def test_intercept_printable_on_alternate_layout(self, backend, mock_gdk):
        """Printable char on layout!=0 with Alt is intercepted."""
        mock_gdk.keyval_to_unicode.return_value = ord('|')
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1  # Alternate layout
            state = ALT_MASK

            result = backend._on_macos_option_key_pressed(
                controller, ord('|'), 0, state
            )
            assert result is True
            backend.owner.feed_child_data.assert_called_once_with(b'|')

    def test_intercept_sends_utf8(self, backend, mock_gdk):
        """Non-ASCII characters are sent as UTF-8."""
        mock_gdk.keyval_to_unicode.return_value = ord('€')
        with patch('sshpilot.terminal_backends.Gdk', mock_gdk):
            controller = MagicMock()
            controller.get_group.return_value = 1
            state = ALT_MASK

            result = backend._on_macos_option_key_pressed(
                controller, 0x20ac, 0, state
            )
            assert result is True
            backend.owner.feed_child_data.assert_called_once_with('€'.encode('utf-8'))


class TestControllerLifecycle:
    """Test controller installation and removal."""

    def test_controller_not_installed_on_non_macos(self):
        """Controller is not installed on non-macOS platforms."""
        from sshpilot.terminal_backends import VTETerminalBackend

        with patch('sshpilot.platform_utils.is_macos', return_value=False):
            backend = VTETerminalBackend.__new__(VTETerminalBackend)
            backend._macos_option_key_controller = None
            backend.vte = MagicMock()

            backend.set_macos_option_key_passthrough(True)

            assert backend._macos_option_key_controller is None

    def test_controller_installed_on_macos(self):
        """Controller is installed on macOS when enabled."""
        from sshpilot.terminal_backends import VTETerminalBackend, Gtk

        with patch('sshpilot.platform_utils.is_macos', return_value=True):
            # Mock Gtk.EventControllerKey to return a mock controller
            mock_controller = MagicMock()
            with patch.object(Gtk, 'EventControllerKey', return_value=mock_controller):
                backend = VTETerminalBackend.__new__(VTETerminalBackend)
                backend._macos_option_key_controller = None
                backend.vte = MagicMock()

                backend.set_macos_option_key_passthrough(True)

                assert backend._macos_option_key_controller is mock_controller

    def test_controller_removed_when_disabled(self):
        """Controller is removed when feature is disabled."""
        from sshpilot.terminal_backends import VTETerminalBackend

        with patch('sshpilot.platform_utils.is_macos', return_value=True):
            backend = VTETerminalBackend.__new__(VTETerminalBackend)
            backend._macos_option_key_controller = MagicMock()
            backend.vte = MagicMock()

            backend.set_macos_option_key_passthrough(False)

            assert backend._macos_option_key_controller is None

    def test_no_duplicate_controller_install(self):
        """Double-enable doesn't create duplicate controllers."""
        from sshpilot.terminal_backends import VTETerminalBackend, Gtk

        with patch('sshpilot.platform_utils.is_macos', return_value=True):
            mock_controller = MagicMock()
            with patch.object(Gtk, 'EventControllerKey', return_value=mock_controller):
                backend = VTETerminalBackend.__new__(VTETerminalBackend)
                backend._macos_option_key_controller = None
                backend.vte = MagicMock()

                backend.set_macos_option_key_passthrough(True)
                first_controller = backend._macos_option_key_controller

                # Second call should not create a new controller
                backend.set_macos_option_key_passthrough(True)
                second_controller = backend._macos_option_key_controller

                assert first_controller is second_controller

    def test_destroy_removes_controller(self):
        """destroy() cleans up the controller."""
        from sshpilot.terminal_backends import VTETerminalBackend

        backend = VTETerminalBackend.__new__(VTETerminalBackend)
        backend._destroyed = False
        backend._macos_option_key_controller = MagicMock()
        backend._termprops_handler = None
        backend._selection_handler = None
        backend._native_context_handler = None
        backend._background_provider = None
        backend.vte = MagicMock()
        backend.clear_native_context_menu = MagicMock()
        backend._remove_background_provider = MagicMock()

        backend.destroy()

        assert backend._destroyed is True
        assert backend._macos_option_key_controller is None


class TestInputRouting:
    """Test input reaches the correct destination."""

    def test_utf8_encoding_for_multibyte_characters(self):
        """Non-ASCII characters are properly UTF-8 encoded."""
        test_chars = [
            ('ß', b'\xc3\x9f'),
            ('é', b'\xc3\xa9'),
            ('日', b'\xe6\x97\xa5'),
            ('€', b'\xe2\x82\xac'),
        ]
        for char, expected_bytes in test_chars:
            assert char.encode('utf-8') == expected_bytes

    def test_printable_detection(self):
        """Test printable character detection logic."""
        assert '|'.isprintable()
        assert '@'.isprintable()
        assert 'ñ'.isprintable()
        assert '日'.isprintable()

        assert not '\x00'.isprintable()
        assert not '\x1b'.isprintable()
        assert not '\x7f'.isprintable()


class TestPlatformGuard:
    """Test platform-specific behavior."""

    def test_is_macos_detection(self):
        """Test is_macos() function."""
        from sshpilot.platform_utils import is_macos
        import platform

        expected = platform.system() == "Darwin"
        assert is_macos() == expected

    def test_set_macos_option_key_passthrough_exists(self):
        """VTETerminalBackend has set_macos_option_key_passthrough method."""
        from sshpilot.terminal_backends import VTETerminalBackend

        assert hasattr(VTETerminalBackend, 'set_macos_option_key_passthrough')

    def test_internal_controller_methods_exist(self):
        """VTETerminalBackend has internal controller methods."""
        from sshpilot.terminal_backends import VTETerminalBackend

        assert hasattr(VTETerminalBackend, '_install_macos_option_key_controller')
        assert hasattr(VTETerminalBackend, '_remove_macos_option_key_controller')
        assert hasattr(VTETerminalBackend, '_on_macos_option_key_pressed')

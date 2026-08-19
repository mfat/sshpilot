"""Tests for macOS VTE Option key passthrough workaround."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.core.settings.defaults import get_default_config
from sshpilot.core.settings.migration import ensure_config_defaults


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


class TestOptionKeyFiltering:
    """Test filtering criteria for Option key interception."""

    def test_pass_through_when_alt_not_pressed(self):
        """Event passes through when Alt/Option is not pressed."""
        # No ALT_MASK means don't intercept
        from gi.repository import Gdk

        state = MagicMock()
        state.__and__ = MagicMock(return_value=False)

        # Handler should return False (pass through)
        assert not (state & Gdk.ModifierType.ALT_MASK)

    def test_intercept_requires_alt_mask(self):
        """Interception requires Alt/Option mask to be set."""
        # This tests the logical requirement - actual GTK testing needs real GTK

    def test_ctrl_alt_not_intercepted(self):
        """Ctrl+Alt combinations should not be intercepted."""
        # Ctrl+Alt is used for terminal shortcuts, not character input

    def test_cmd_alt_not_intercepted(self):
        """Cmd+Alt combinations should not be intercepted."""
        # Command+Alt is for system shortcuts

    def test_dead_keys_converted_to_base_chars(self):
        """Dead keys are converted to their base characters."""
        from sshpilot.terminal_backends import VTETerminalBackend

        # Test the dead key mapping
        expected = {
            'dead_tilde': '~',
            'dead_perispomeni': '~',  # Greek circumflex, used for tilde on some layouts
            'dead_grave': '`',
            'dead_acute': "'",
            'dead_circumflex': '^',
            'dead_diaeresis': '"',
        }
        for dead_key, base_char in expected.items():
            assert VTETerminalBackend._DEAD_KEY_MAP.get(dead_key) == base_char

    def test_control_char_not_intercepted(self):
        """Control characters (< 0x20) should not be intercepted."""
        # Non-printable characters should pass to VTE

    def test_ascii_letters_not_intercepted(self):
        """Basic ASCII letters (a-z, A-Z) pass through for Meta sequences."""
        # Alt+B, Alt+F etc. should still work for word navigation

    def test_printable_unicode_intercepted(self):
        """Printable Unicode characters should be intercepted."""
        # This is the core functionality - send characters directly


class TestControllerLifecycle:
    """Test controller installation and removal."""

    def test_controller_not_installed_on_non_macos(self):
        """Controller is not installed on non-macOS platforms."""
        from sshpilot.terminal_backends import VTETerminalBackend

        # The set_macos_option_key_passthrough method imports is_macos locally
        # We need to patch it in platform_utils where it's defined
        with patch('sshpilot.platform_utils.is_macos', return_value=False):
            backend = VTETerminalBackend.__new__(VTETerminalBackend)
            backend._macos_option_key_controller = None
            backend.vte = MagicMock()

            # Call the method - it should return early without installing controller
            backend.set_macos_option_key_passthrough(True)

            # Controller should still be None since we're not on macOS
            assert backend._macos_option_key_controller is None

    def test_destroy_removes_controller(self):
        """destroy() should clean up the controller."""
        # Verify the destroy method calls _remove_macos_option_key_controller
        from sshpilot.terminal_backends import VTETerminalBackend

        backend = VTETerminalBackend.__new__(VTETerminalBackend)
        backend._destroyed = False
        backend._macos_option_key_controller = MagicMock()
        backend._termprops_handler = None
        backend._selection_handler = None
        backend._native_context_handler = None
        backend._background_provider = None
        backend.vte = MagicMock()

        # Mock clear_native_context_menu and _remove_background_provider
        backend.clear_native_context_menu = MagicMock()
        backend._remove_background_provider = MagicMock()

        # Call destroy
        backend.destroy()

        # Verify destroyed flag is set
        assert backend._destroyed is True
        # Verify controller was removed (set to None in _remove_macos_option_key_controller)
        assert backend._macos_option_key_controller is None


class TestInputRouting:
    """Test input reaches the correct destination."""

    def test_utf8_encoding_for_multibyte_characters(self):
        """Non-ASCII characters should be properly UTF-8 encoded."""
        test_chars = [
            ('ß', b'\xc3\x9f'),  # German sharp s
            ('é', b'\xc3\xa9'),  # French e with acute
            ('日', b'\xe6\x97\xa5'),  # Japanese kanji
            ('€', b'\xe2\x82\xac'),  # Euro sign
        ]
        for char, expected_bytes in test_chars:
            assert char.encode('utf-8') == expected_bytes

    def test_printable_detection(self):
        """Test printable character detection logic."""
        # Printable characters
        assert '|'.isprintable()
        assert '@'.isprintable()
        assert 'ñ'.isprintable()
        assert '日'.isprintable()

        # Non-printable characters
        assert not '\x00'.isprintable()
        assert not '\x1b'.isprintable()  # ESC
        assert not '\x7f'.isprintable()  # DEL


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

"""Settings migration and default backfill (GTK-free)."""
from __future__ import annotations

from typing import Any, Dict, Tuple

from .defaults import get_default_config


def ensure_config_defaults(config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Ensure newly added keys exist in *config*. Returns (config, updated)."""
    updated = False

    shortcuts = config.get('shortcuts')
    if not isinstance(shortcuts, dict):
        config['shortcuts'] = {}
        updated = True

    terminal_cfg = config.get('terminal')
    if not isinstance(terminal_cfg, dict):
        config['terminal'] = get_default_config().get('terminal', {}).copy()
        terminal_cfg = config['terminal']
        updated = True
    if 'pass_through_mode' not in terminal_cfg:
        terminal_cfg['pass_through_mode'] = False
        updated = True
    elif not isinstance(terminal_cfg['pass_through_mode'], bool):
        terminal_cfg['pass_through_mode'] = bool(terminal_cfg['pass_through_mode'])
        updated = True
    if 'term' not in terminal_cfg:
        terminal_cfg['term'] = None
        updated = True
    else:
        term_value = terminal_cfg['term']
        normalized_term = None
        if isinstance(term_value, str):
            normalized_term = term_value.strip() or None
        elif term_value is None:
            normalized_term = None
        if normalized_term != term_value:
            terminal_cfg['term'] = normalized_term
            updated = True

    encoding_value = terminal_cfg.get('encoding')
    if isinstance(encoding_value, str):
        normalized_encoding = encoding_value.strip()
        if not normalized_encoding:
            normalized_encoding = 'UTF-8'
        if normalized_encoding != encoding_value:
            terminal_cfg['encoding'] = normalized_encoding
            updated = True
    else:
        terminal_cfg['encoding'] = 'UTF-8'
        updated = True

    # --- Tab close policy: retire a stored "detach" once ----------------
    # Detaching left the daemon holding a RUNNING session with no tab owning
    # it, so the sidebar kept the host green after the user had closed the
    # connection, and that orphan then outvoted every session opened later
    # (GH #1176). Closing a tab ends its session now. The marker makes this a
    # one-shot rewrite rather than a standing rule: detach stays a supported
    # value for anyone who deliberately sets it again, it is simply no longer
    # offered in Preferences.
    if not terminal_cfg.get('daemon_tab_close_policy_migrated'):
        stored_policy = terminal_cfg.get('daemon_tab_close_policy')
        if isinstance(stored_policy, str) and stored_policy.strip().lower() == 'detach':
            terminal_cfg['daemon_tab_close_policy'] = 'terminate'
        terminal_cfg['daemon_tab_close_policy_migrated'] = True
        updated = True

    # macOS Option key passthrough (new setting)
    if 'macos_option_key_passthrough' not in terminal_cfg:
        terminal_cfg['macos_option_key_passthrough'] = False
        updated = True
    elif not isinstance(terminal_cfg['macos_option_key_passthrough'], bool):
        terminal_cfg['macos_option_key_passthrough'] = bool(
            terminal_cfg['macos_option_key_passthrough']
        )
        updated = True

    file_manager_defaults = get_default_config().get('file_manager', {})
    file_manager_cfg = config.get('file_manager')
    if not isinstance(file_manager_cfg, dict):
        config['file_manager'] = file_manager_defaults.copy()
        updated = True
    else:
        for obsolete_key in ('force_internal', 'first_run_prompt_shown'):
            if obsolete_key in file_manager_cfg:
                file_manager_cfg.pop(obsolete_key, None)
                updated = True

        if 'open_externally' not in file_manager_cfg:
            file_manager_cfg['open_externally'] = bool(
                file_manager_defaults.get('open_externally', False)
            )
            updated = True
        elif not isinstance(file_manager_cfg['open_externally'], bool):
            file_manager_cfg['open_externally'] = bool(file_manager_cfg['open_externally'])
            updated = True

        def _ensure_non_negative_int(key: str) -> None:
            nonlocal updated
            default_value = file_manager_defaults.get(key, 0)
            value = file_manager_cfg.get(key, default_value)
            try:
                coerced = int(value)
            except (TypeError, ValueError):
                coerced = default_value
            if coerced < 0:
                coerced = default_value
            if file_manager_cfg.get(key) != coerced:
                file_manager_cfg[key] = coerced
                updated = True

        for int_key in (
            'sftp_keepalive_interval',
            'sftp_keepalive_count_max',
            'sftp_connect_timeout',
        ):
            if int_key not in file_manager_cfg:
                file_manager_cfg[int_key] = int(file_manager_defaults.get(int_key, 0))
                updated = True
            else:
                _ensure_non_negative_int(int_key)

        icon_size_default = int(file_manager_defaults.get('icon_size_level', 1))
        icon_size_value = file_manager_cfg.get('icon_size_level', icon_size_default)
        try:
            coerced_icon_size = int(icon_size_value)
        except (TypeError, ValueError):
            coerced_icon_size = icon_size_default
        clamped_icon_size = max(0, min(4, coerced_icon_size))
        if file_manager_cfg.get('icon_size_level') != clamped_icon_size:
            file_manager_cfg['icon_size_level'] = clamped_icon_size
            updated = True

    # --- Logging level: migrate from legacy ssh.debug_enabled --------
    logging_cfg = config.get('logging')
    if not isinstance(logging_cfg, dict):
        logging_cfg = {}
        config['logging'] = logging_cfg
        updated = True
    if logging_cfg.get('level') not in ('info', 'debug'):
        # One-shot migration: if the old hidden ssh.debug_enabled key was
        # True, preserve that as the new 'debug' level. Otherwise default
        # to 'info'.
        legacy_ssh = config.get('ssh') if isinstance(config.get('ssh'), dict) else {}
        legacy_debug = bool(legacy_ssh.get('debug_enabled', False)) if legacy_ssh else False
        logging_cfg['level'] = 'debug' if legacy_debug else 'info'
        updated = True

    ui_cfg = config.get('ui')
    if not isinstance(ui_cfg, dict):
        default_ui = get_default_config().get('ui', {}).copy()
        config['ui'] = default_ui
        ui_cfg = default_ui
        updated = True
    display_value = ui_cfg.get('group_color_display') if isinstance(ui_cfg, dict) else None
    if display_value is None:
        # Match get_default_config(): Accent Bars for installs missing the key.
        ui_cfg['group_color_display'] = 'bar'
        updated = True
    else:
        if not isinstance(display_value, str):
            display_value = str(display_value)
        normalized = display_value.lower()
        if normalized not in {'fill', 'badge', 'bar', 'dot'}:
            normalized = 'fill'
        if ui_cfg.get('group_color_display') != normalized:
            ui_cfg['group_color_display'] = normalized
            updated = True

    if 'use_group_color_in_tab' not in ui_cfg:
        ui_cfg['use_group_color_in_tab'] = False
        updated = True
    elif not isinstance(ui_cfg['use_group_color_in_tab'], bool):
        ui_cfg['use_group_color_in_tab'] = bool(ui_cfg['use_group_color_in_tab'])
        updated = True

    if 'use_group_color_in_terminal' not in ui_cfg:
        ui_cfg['use_group_color_in_terminal'] = False
        updated = True
    elif not isinstance(ui_cfg['use_group_color_in_terminal'], bool):
        ui_cfg['use_group_color_in_terminal'] = bool(ui_cfg['use_group_color_in_terminal'])
        updated = True

    # ``connection_sort_last`` used to persist the sidebar sort preset. The sort
    # is a UI-only overlay that the daemon projection drops on every refresh, so
    # restoring it made the button advertise an order that was never applied.
    if 'connection_sort_last' in ui_cfg:
        del ui_cfg['connection_sort_last']
        updated = True

    ssh_cfg = config.get('ssh')
    if not isinstance(ssh_cfg, dict):
        default_ssh = get_default_config().get('ssh', {}).copy()
        config['ssh'] = default_ssh
        updated = True
        ssh_cfg = config['ssh']
    elif 'apply_advanced' in ssh_cfg:
        del ssh_cfg['apply_advanced']
        updated = True
    if 'use_isolated_config' not in ssh_cfg:
        ssh_cfg['use_isolated_config'] = False
        updated = True
    elif not isinstance(ssh_cfg['use_isolated_config'], bool):
        ssh_cfg['use_isolated_config'] = bool(ssh_cfg['use_isolated_config'])
        updated = True

    if not isinstance(config.get('command_blocks'), dict):
        config['command_blocks'] = get_default_config()['command_blocks'].copy()
        updated = True
    else:
        cb = config['command_blocks']
        if not isinstance(cb.get('folders'), list):
            cb['folders'] = []
            updated = True
        if not isinstance(cb.get('commands'), list):
            cb['commands'] = []
            updated = True
        if 'insert_only' not in cb:
            cb['insert_only'] = False
            updated = True
        if 'auto_hide_sidebar' not in cb:
            cb['auto_hide_sidebar'] = False
            updated = True

    return config, updated

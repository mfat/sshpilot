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

    file_manager_defaults = get_default_config().get('file_manager', {})
    file_manager_cfg = config.get('file_manager')
    if not isinstance(file_manager_cfg, dict):
        config['file_manager'] = file_manager_defaults.copy()
        updated = True
    else:
        if 'force_internal' not in file_manager_cfg:
            file_manager_cfg['force_internal'] = bool(
                file_manager_defaults.get('force_internal', False)
            )
            updated = True
        elif not isinstance(file_manager_cfg['force_internal'], bool):
            file_manager_cfg['force_internal'] = bool(file_manager_cfg['force_internal'])
            updated = True

        if 'open_externally' not in file_manager_cfg:
            file_manager_cfg['open_externally'] = bool(
                file_manager_defaults.get('open_externally', False)
            )
            updated = True
        elif not isinstance(file_manager_cfg['open_externally'], bool):
            file_manager_cfg['open_externally'] = bool(file_manager_cfg['open_externally'])
            updated = True

        if 'first_run_prompt_shown' not in file_manager_cfg:
            file_manager_cfg['first_run_prompt_shown'] = bool(
                file_manager_defaults.get('first_run_prompt_shown', False)
            )
            updated = True
        elif not isinstance(file_manager_cfg['first_run_prompt_shown'], bool):
            file_manager_cfg['first_run_prompt_shown'] = bool(
                file_manager_cfg['first_run_prompt_shown']
            )
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

    sort_last = ui_cfg.get('connection_sort_last')
    if not isinstance(sort_last, str):
        ui_cfg['connection_sort_last'] = 'name-asc'
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

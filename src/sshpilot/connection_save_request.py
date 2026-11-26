"""The daemon request a connection editor save sends, built from its form.

One builder for the save itself and for the editor's question to OpenSSH
before the save (``connections.evaluate_connection_edit``): the question is
about exactly what the save would write only while both send the same
request. No GTK here.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from .api.models.connections import (
    EDITABLE_CONFIG_FIELDS,
    UNSET,
    CreateConnectionRequest,
    ForwardingRule,
    UpdateConnectionRequest,
    forwarding_rule_to_dict,
)

_CORE_FIELDS = ("nickname", "hostname", "username", "port", "protocol")


def config_patch(
    connection_data: Mapping[str, Any], changed_fields: Iterable[str] = ()
) -> dict:
    """The ``config_patch`` of the save: every editable field the form
    carries, or only the changed ones when *changed_fields* names any."""
    changed = set(changed_fields or ())
    patch: dict = {}
    for key in EDITABLE_CONFIG_FIELDS:
        if key in _CORE_FIELDS or key not in connection_data:
            continue
        if changed and key not in changed:
            continue
        if key == "forwarding_rules":
            raw = connection_data.get(key, ())
            patch[key] = [
                forwarding_rule_to_dict(r) if isinstance(r, ForwardingRule)
                else r if isinstance(r, dict) else {}
                for r in raw
            ] if raw else []
        elif key in ("identity_files", "certificate_files", "proxy_jump", "aliases"):
            val = connection_data.get(key, ())
            if isinstance(val, str):
                if key in ("proxy_jump", "aliases"):
                    patch[key] = [h.strip() for h in re.split(r'[\s,]+', val) if h.strip()]
                else:
                    patch[key] = [f.strip() for f in re.split(r'[\r\n,]+', val) if f.strip()]
            else:
                patch[key] = (
                    [str(item).strip() for item in val if str(item).strip()] if val else []
                )
        elif isinstance(connection_data.get(key), bool):
            patch[key] = connection_data[key]
        else:
            val = connection_data.get(key, '')
            if val is not None:
                patch[key] = val
    return patch


def update_request(
    connection_data: Mapping[str, Any],
    *,
    delta_available: bool,
    changed_fields: Iterable[str],
    expected_generation: Optional[int],
    shared_block: bool = False,
) -> UpdateConnectionRequest:
    """The update a save of an edited connection sends.

    With a delta (*delta_available*), only the changed fields are sent; the
    caller handles a delta that changed nothing (there is no update then).
    """
    changed = set(changed_fields or ())

    def core(name):
        if not delta_available or name in changed:
            return connection_data.get(name)
        return UNSET

    return UpdateConnectionRequest(
        nickname=core('nickname'),
        hostname=core('hostname'),
        username=core('username'),
        port=core('port'),
        display_name=core('display_name'),
        config_patch=config_patch(connection_data, changed),
        expected_generation=expected_generation,
        shared_block=shared_block,
    )


def create_request(connection_data: Mapping[str, Any]) -> CreateConnectionRequest:
    """The create a save of a new connection sends."""
    return CreateConnectionRequest(
        nickname=connection_data.get('nickname', ''),
        hostname=connection_data.get('hostname', ''),
        username=connection_data.get('username', ''),
        # A new connection needs a concrete port; an empty field means
        # "inherit", and 22 is what OpenSSH falls back to, so the formatter
        # still omits the Port line for it.
        port=connection_data.get('port') or 22,
        protocol='ssh',
        display_name=connection_data.get('display_name', ''),
        config_patch=config_patch(connection_data),
    )

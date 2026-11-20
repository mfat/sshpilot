"""Strict configuration manager wire contracts."""
import pytest

from sshpilot.api.models.connections import SshConfigManagerRequest, SshConfigText
from sshpilot.api.transport.config_manager import (
    config_preview_from_wire, config_preview_to_wire,
    manager_request_from_wire, manager_request_to_wire,
    manager_document_from_wire, manager_document_to_wire,
)
from sshpilot.core.ssh_config_manager import edit_document


def test_document_and_request_roundtrip():
    assert manager_request_from_wire({}) == SshConfigManagerRequest()
    request = SshConfigManagerRequest('Host prod\n User deploy\n', 'revision')
    assert manager_request_from_wire(manager_request_to_wire(request)) == request
    view = edit_document(SshConfigText(request.text, 'revision', '~/.ssh/config', True), request)
    assert manager_document_from_wire(manager_document_to_wire(view)) == view


@pytest.mark.parametrize('field,value', [('block', True), ('position', -1), ('directive', -2),
                                        ('value', 'one\nPort 99'), ('action', 'save'), ('text', 4)])
def test_request_rejects_invalid_fields(field, value):
    payload = manager_request_to_wire(SshConfigManagerRequest())
    payload[field] = value
    with pytest.raises((TypeError, ValueError)):
        manager_request_from_wire(payload)


def test_unknown_fields_never_select_a_filesystem_path():
    payload = manager_request_to_wire(SshConfigManagerRequest())
    payload['path'] = '/another/config'
    with pytest.raises(ValueError):
        manager_request_from_wire(payload)


def test_preview_roundtrip_and_strict_nested_types():
    payload = {'revision': 'revision', 'valid': True, 'diagnostics': [], 'hosts': [
        {'host': 'prod', 'hostname': 'example.com', 'matched': True,
         'before': ['user before'], 'after': ['user after'], 'contributions': ['user']}]}
    assert config_preview_to_wire(config_preview_from_wire(payload)) == payload
    payload['hosts'][0]['matched'] = 1
    with pytest.raises(ValueError):
        config_preview_from_wire(payload)


def test_document_rejects_invalid_directive_line_and_selection():
    view = edit_document(SshConfigText('Host prod\n User deploy\n', 'revision', '~/.ssh/config', True), SshConfigManagerRequest())
    payload = manager_document_to_wire(view)
    payload['blocks'][0]['directives'][0]['line'] = 0
    with pytest.raises(ValueError):
        manager_document_from_wire(payload)
    payload = manager_document_to_wire(view)
    payload['selected'] = 2
    with pytest.raises(ValueError):
        manager_document_from_wire(payload)

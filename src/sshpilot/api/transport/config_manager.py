"""Strict codecs for the advanced configuration manager's public DTOs."""
from dataclasses import asdict

from ..models.connections import (
    SshConfigBlock, SshConfigDirective, SshConfigHostPreview,
    SshConfigManagerDocument, SshConfigManagerRequest, SshConfigPreview,
)
from .codec import _strict_fields, ssh_config_text_from_wire, ssh_config_text_to_wire


_REQUEST_FIELDS = {'text', 'expected_revision', 'action', 'block', 'position', 'directive', 'keyword', 'value'}


def manager_request_to_wire(value: SshConfigManagerRequest) -> dict:
    if type(value) is not SshConfigManagerRequest:
        raise TypeError('a configuration manager request is required')
    return asdict(value)


def manager_request_from_wire(value) -> SshConfigManagerRequest:
    data = _strict_fields(value, required=set(), optional=_REQUEST_FIELDS, context='config manager request')
    return SshConfigManagerRequest(**data)


def _string(value):
    if type(value) is not str or '\x00' in value:
        raise ValueError('expected safe configuration text')
    return value


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('invalid configuration index')
    return value


def _strings(value):
    if type(value) is not list:
        raise ValueError('expected configuration string list')
    return tuple(_string(item) for item in value)


def manager_document_to_wire(value: SshConfigManagerDocument) -> dict:
    if type(value) is not SshConfigManagerDocument:
        raise TypeError('a configuration manager document is required')
    return {
        'document': ssh_config_text_to_wire(value.document),
        'selected': value.selected,
        'blocks': [dict(kind=b.kind, argument=b.argument, text=b.text, line=b.line,
                        directives=[asdict(d) for d in b.directives], included_files=list(b.included_files))
                   for b in value.blocks],
    }


def manager_document_from_wire(value) -> SshConfigManagerDocument:
    data = _strict_fields(value, required={'document', 'selected', 'blocks'}, context='config manager document')
    if type(data['blocks']) is not list:
        raise ValueError('expected configuration blocks')
    blocks = []
    for raw in data['blocks']:
        b = _strict_fields(raw, required={'kind', 'argument', 'text', 'line', 'directives', 'included_files'}, context='configuration block')
        if b['kind'] not in {'host', 'match', 'include', 'global'} or type(b['directives']) is not list:
            raise ValueError('invalid configuration block')
        directives = []
        for raw_directive in b['directives']:
            d = _strict_fields(raw_directive, required={'keyword', 'value', 'line'}, context='configuration directive')
            directives.append(SshConfigDirective(_string(d['keyword']), _string(d['value']), _integer(d['line'], 1)))
        blocks.append(SshConfigBlock(b['kind'], _string(b['argument']), _string(b['text']), _integer(b['line'], 1),
                                     tuple(directives), _strings(b['included_files'])))
    selected = _integer(data['selected'])
    if selected >= max(1, len(blocks)):
        raise ValueError('selected configuration block is out of range')
    return SshConfigManagerDocument(ssh_config_text_from_wire(data['document']), tuple(blocks), selected)


def config_preview_to_wire(value: SshConfigPreview) -> dict:
    if type(value) is not SshConfigPreview:
        raise TypeError('a configuration preview is required')
    return dict(revision=value.revision, valid=value.valid, diagnostics=list(value.diagnostics),
                hosts=[dict(host=h.host, hostname=h.hostname, matched=h.matched,
                            before=list(h.before), after=list(h.after), contributions=list(h.contributions))
                       for h in value.hosts])


def config_preview_from_wire(value) -> SshConfigPreview:
    data = _strict_fields(value, required={'revision', 'valid', 'diagnostics', 'hosts'}, context='configuration preview')
    if type(data['valid']) is not bool or type(data['hosts']) is not list:
        raise ValueError('invalid configuration preview')
    hosts = []
    for raw in data['hosts']:
        h = _strict_fields(raw, required={'host', 'hostname', 'matched', 'before', 'after', 'contributions'}, context='configuration host preview')
        if type(h['matched']) is not bool:
            raise ValueError('invalid configuration match')
        hosts.append(SshConfigHostPreview(_string(h['host']), _string(h['hostname']), h['matched'],
                                          _strings(h['before']), _strings(h['after']), _strings(h['contributions'])))
    return SshConfigPreview(_string(data['revision']), tuple(hosts), data['valid'], _strings(data['diagnostics']))

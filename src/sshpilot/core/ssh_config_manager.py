"""Daemon-owned structural editing and native OpenSSH draft evaluation.

Structural parsing never decides whether a Host/Match applies. A unique IdentityFile
probe in the selected stanza asks OpenSSH itself, including negation, Include,
canonical/final passes and Match predicates. Queries use private -F drafts and
-G, never connect. Removing one authored line identifies demonstrable directive
contributions without inventing provenance for equal-valued or default options.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile
import secrets
from dataclasses import replace

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.connections import (
    SshConfigBlock, SshConfigDirective, SshConfigHostPreview,
    SshConfigManagerDocument, SshConfigManagerRequest, SshConfigPreview,
)
from sshpilot.platform.paths import openssh_user_dir, is_openssh_user_config
from sshpilot.ssh_config_document import (
    SSHConfigDocument, _split_keyword, split_config_lines, split_include_args,
)
from .ssh_config_effective import expand_ssh_tokens, openssh_system_config, query_ssh_config


def _invalid(message):
    return SshPilotError(ErrorCode.VALIDATION_FAILED, message)


def blocks_for(text):
    # Reuse the lossless document lexer; boundaries are presentation structure,
    # not matching semantics. Include retains the following lexical scope.
    text = SSHConfigDocument.parse_text(text).text()
    lines = split_config_lines(text, keepends=True)
    starts = [i for i, line in enumerate(lines)
              if _split_keyword(line.strip())[0] in {'host', 'match', 'include'}]
    if lines and (not starts or starts[0] != 0):
        starts.insert(0, 0)
    blocks = []
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        keyword, argument = _split_keyword(lines[start].strip())
        kind = keyword if keyword in {'host', 'match', 'include'} else 'global'
        directives = []
        for index in range(start + (kind != 'global'), end):
            stripped = lines[index].strip()
            if stripped and not stripped.startswith('#'):
                key, value = _split_keyword(stripped)
                directives.append(SshConfigDirective(key, value, index + 1))
        included = []
        if kind == 'include':
            try:
                for pattern in split_include_args(argument):
                    pattern = os.path.expanduser(expand_ssh_tokens(pattern))
                    if not os.path.isabs(pattern):
                        pattern = str(openssh_user_dir() / pattern)
                    included.extend(p for p in sorted(glob.glob(pattern)) if os.path.isfile(p))
            except ValueError:
                pass  # OpenSSH validation reports the actual syntax error.
        blocks.append(SshConfigBlock(kind, argument, ''.join(lines[start:end]),
                                     start + 1, tuple(directives), tuple(included)))
    return tuple(blocks)


def edit_document(document, request: SshConfigManagerRequest):
    if request.text is not None and request.expected_revision != document.revision:
        raise SshPilotError(ErrorCode.STALE_EDITOR, 'The SSH configuration changed; reload before editing')
    text = document.text if request.text is None else request.text
    blocks = blocks_for(text)
    chunks = [b.text for b in blocks]
    index = request.block
    action = request.action
    newline = '\r\n' if '\r\n' in text else '\n'
    if action != 'inspect':
        if not document.writable:
            raise SshPilotError(ErrorCode.SSH_CONFIG_READ_ONLY, 'The SSH configuration is read-only')
        if action == 'add':
            if request.keyword.lower() not in {'host', 'match', 'include'} or not request.value.strip():
                raise _invalid('Choose Host, Match or Include and a nonempty argument')
            index = min(request.position, len(chunks))
            chunks.insert(index, f'{request.keyword} {request.value}{newline}')
        else:
            if index >= len(blocks):
                raise _invalid('The selected configuration block does not exist')
            block = blocks[index]
            lines = split_config_lines(chunks[index], keepends=True)
            if action == 'remove':
                chunks.pop(index)
            elif action == 'move':
                if request.position >= len(chunks):
                    raise _invalid('The destination position does not exist')
                chunks.insert(request.position, chunks.pop(index))
                index = request.position
            elif action == 'header':
                if block.kind == 'global' or not request.value.strip():
                    raise _invalid('This block requires a nonempty header')
                lines[0] = f'{block.kind.title()} {request.value}{newline}'
                chunks[index] = ''.join(lines)
            elif action in {'directive', 'remove_directive'}:
                if action == 'directive':
                    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', request.keyword) or request.keyword.lower() in {'host', 'match', 'include'}:
                        raise _invalid('Enter an SSH directive name')
                    if not request.value.strip():
                        raise _invalid('Enter a directive value')
                if request.directive == -1 and action == 'directive':
                    if lines and not lines[-1].endswith('\n'):
                        lines[-1] += newline
                    lines.append(f'    {request.keyword} {request.value}{newline}')
                else:
                    if not 0 <= request.directive < len(block.directives):
                        raise _invalid('The selected directive does not exist')
                    offset = block.directives[request.directive].line - block.line
                    if action == 'remove_directive':
                        lines.pop(offset)
                    else:
                        lines[offset] = f'    {request.keyword} {request.value}{newline}'
                chunks[index] = ''.join(lines)
        # A moved EOF stanza must not fuse with the following header.
        text = ''.join(chunk + (newline if n < len(chunks)-1 and not chunk.endswith('\n') else '')
                       for n, chunk in enumerate(chunks))
        blocks = blocks_for(text)
    return SshConfigManagerDocument(replace(document, text=text), blocks,
                                    min(index, max(0, len(blocks)-1)))


def preview_config(root, baseline, draft, hosts, selected, handle, *, isolated=False):
    """Resolve every saved SSH alias; cancellation supervises each ssh child."""
    blocks = blocks_for(draft.text)
    block = blocks[selected] if 0 <= selected < len(blocks) else None
    marker = 'SSHPILOT_PREVIEW_' + secrets.token_hex(16)
    system = openssh_system_config() if not isolated and is_openssh_user_config(str(root)) else None
    diagnostics = []

    with tempfile.TemporaryDirectory(prefix='sshpilot-config-preview-') as directory:
        if system:
            from .ssh_config_forward_strip import materialize_system_config_for_query
            try:
                system = materialize_system_config_for_query(system, directory)
            except (OSError, UnicodeError):
                return SshConfigPreview(draft.revision, (), False, ('The system SSH configuration could not be read',))
        def query(text, host, *, report_errors=True):
            handle.raise_if_cancelled()
            path = os.path.join(directory, 'config')
            # -F suppresses the system file. Restore that final stage only for
            # the ordinary user config, as the launch path does.
            if system:
                text += '\nHost *\nInclude "' + system.replace('"', '\\"') + '"\n'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(text)
            values, errors = query_ssh_config(host, path, handle=handle)
            if errors and report_errors:
                diagnostics.extend(error.replace(path, draft.display_name) for error in errors)
            return values

        # Even an empty saved-host list needs a real syntax check.
        query(draft.text, 'sshpilot-config-check')
        if diagnostics:
            return SshConfigPreview(draft.revision, (), False, tuple(dict.fromkeys(diagnostics)))
        probe = draft.text
        if block:
            lines = split_config_lines(probe, keepends=True)
            offset = block.line - 1 + (block.kind in {'host', 'match'})
            lines.insert(offset, f'    IdentityFile {marker}\n')
            probe = ''.join(lines)
        result = []
        for number, host in enumerate(hosts):
            handle.report('Resolving SSH configuration', number / max(1, len(hosts)))
            before = query(baseline.text, host, report_errors=False)
            after = query(draft.text, host)
            marked = query(probe, host) if block else {}
            matched = marker in marked.get('identityfile', ())
            contributions = []
            if matched:
                for directive in block.directives:
                    lines = split_config_lines(draft.text, keepends=True)
                    del lines[directive.line-1]
                    without = query(''.join(lines), host)
                    key = directive.keyword.lower()
                    if without.get(key) != after.get(key):
                        contributions.append(key)
            def render(values):
                return tuple(f'{key} {value}' for key, entries in values.items() for value in entries)
            result.append(SshConfigHostPreview(host, next(iter(after.get('hostname', (host,)))), matched,
                                               render(before), render(after), tuple(contributions)))
        return SshConfigPreview(draft.revision, tuple(result), not diagnostics,
                                tuple(dict.fromkeys(diagnostics)))

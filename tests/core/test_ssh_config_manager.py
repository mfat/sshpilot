"""Configuration manager contracts, using real native OpenSSH queries."""
import os
import shutil
import subprocess
import time
from dataclasses import replace

import pytest

from sshpilot.api import DaemonClient, SshPilotError
from sshpilot.api.models.connections import SshConfigManagerRequest, SshConfigText, SaveSshConfigTextRequest
from sshpilot.api.models.operations import OperationState
from sshpilot.api.transport.config_manager import config_preview_from_wire
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.ssh_config_manager import edit_document, preview_config
from sshpilot.daemon import DaemonServer


class Handle:
    def raise_if_cancelled(self):
        pass

    def report(self, *_args):
        pass

    def set_process(self, _process):
        pass

    def clear_process(self):
        pass


def document(text):
    return SshConfigText(text, 'revision', '~/.ssh/config', True)


def test_lossless_structural_edits_and_stale_revision():
    original = '# preamble\r\nHost=prod-*\r\n  User deploy\r\n# keep\r\nInclude "conf.d/*.conf"\r\nMatch host *.internal\r\n  Port 2222'
    doc = document(original)
    view = edit_document(doc, SshConfigManagerRequest())
    assert ''.join(b.text for b in view.blocks) == original
    assert [b.kind for b in view.blocks] == ['global', 'host', 'include', 'match']
    updated = edit_document(doc, SshConfigManagerRequest(original, 'revision', 'directive', block=1,
                                                        directive=0, keyword='User', value='other'))
    assert '# keep\r\n' in updated.document.text
    assert '    User other\r\n' in updated.document.text
    moved = edit_document(doc, SshConfigManagerRequest(original, 'revision', 'move', block=3, position=1))
    assert 'Port 2222\r\nHost=prod-*' in moved.document.text
    with pytest.raises(SshPilotError) as exc:
        edit_document(doc, SshConfigManagerRequest(original, 'old'))
    assert exc.value.code.value == 'stale_editor'


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_native_matching_first_value_and_accumulation(tmp_path):
    text = ('Host *\n  User first\n'
            'Host prod-* !prod-test\n  User ignored\n  IdentityFile ~/.ssh/one\n'
            'Host prod-db\n  HostName db.example.com\n  IdentityFile ~/.ssh/two\n'
            'Match host db.example.com\n  Port 2222\n')
    base = document(text)
    result = preview_config(tmp_path/'config', base, base, ('prod-db', 'prod-test', 'staging'), 1, Handle(), isolated=True)
    assert result.valid, result.diagnostics
    assert [h.matched for h in result.hosts] == [True, False, False]
    prod = result.hosts[0]
    assert 'user first' in prod.after
    assert 'port 2222' in prod.after
    assert 'identityfile ~/.ssh/one' in prod.after
    assert 'identityfile ~/.ssh/two' in prod.after
    assert prod.contributions == ('identityfile',)
    config = tmp_path/'config'
    config.write_text(text)
    native = subprocess.check_output(['ssh', '-F', str(config), '-G', 'prod-db'], text=True)
    assert prod.after == tuple(line.partition(' ')[0].lower() + ' ' + line.partition(' ')[2]
                               for line in native.splitlines())


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_include_match_final_and_change_preview(tmp_path):
    included = tmp_path/'included.conf'
    included.write_text('Host prod\n  HostName example.com\n  IdentityFile ~/.ssh/included\n')
    text = f'Include "{included}"\nMatch final host example.com\n  Port 2200\n'
    base = document(text)
    draft = replace(base, text=text.replace('2200', '2300'))
    result = preview_config(tmp_path/'config', base, draft, ('prod',), 1, Handle(), isolated=True)
    assert result.valid, result.diagnostics
    assert result.hosts[0].matched
    assert 'port 2200' in result.hosts[0].before
    assert 'port 2300' in result.hosts[0].after
    assert result.hosts[0].contributions == ('port',)
    assert included.read_text().endswith('~/.ssh/included\n')


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_native_validation_reports_invalid_directive_even_without_hosts(tmp_path):
    base = document('Host *\n  DefinitelyInvalid yes\n')
    result = preview_config(tmp_path/'config', base, base, (), 0, Handle(), isolated=True)
    assert not result.valid
    assert any('Bad configuration option' in d for d in result.diagnostics)
    assert not any('sshpilot-config-preview-' in d for d in result.diagnostics)


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_headless_daemon_round_trip_preview_then_save(tmp_path):
    root = tmp_path/'config'
    root.write_text('Host prod\n  HostName prod.example.com\n  User deploy\n')
    os.chmod(root, 0o600)
    state = tmp_path/'state'
    state.mkdir()
    repository = ConnectionRepository(ssh_store=SshConfigStore(root, isolated=True),
                                      state_path=state/'connections.json', legacy_config_path=state/'config.json', isolated=True)
    service = ConnectionApplicationService(repository)
    server = DaemonServer(lambda: service, socket_path=tmp_path/'daemon.sock')
    server.start_in_thread()
    client = DaemonClient(socket_path=tmp_path/'daemon.sock')
    try:
        loaded = client.edit_ssh_config(SshConfigManagerRequest())
        edited = client.edit_ssh_config(SshConfigManagerRequest(loaded.document.text, loaded.document.revision,
                                                                'directive', directive=1, keyword='User', value='release'))
        assert root.read_text() == loaded.document.text
        operation = client.start_ssh_config_preview(SshConfigManagerRequest(edited.document.text, edited.document.revision))
        other = DaemonClient(socket_path=tmp_path/'daemon.sock')
        try:
            with pytest.raises(SshPilotError) as exc:
                other.get_operation(operation.operation_id)
            assert exc.value.code.value == 'service_owner_required'
        finally:
            other.close()
        deadline = time.monotonic()+15
        while operation.state in {OperationState.QUEUED, OperationState.RUNNING} and time.monotonic() < deadline:
            time.sleep(.02)
            operation = client.get_operation(operation.operation_id)
        assert operation.state is OperationState.SUCCEEDED, operation
        preview = config_preview_from_wire(operation.result)
        assert preview.valid
        assert 'user release' in preview.hosts[0].after
        assert 'user deploy' in preview.hosts[0].before
        assert root.read_text() == loaded.document.text
        saved = client.save_ssh_config_text(SaveSshConfigTextRequest(edited.document.text, edited.document.revision))
        assert saved.text == root.read_text()
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_existing_setenv_does_not_hide_native_match_probe(tmp_path):
    base = document('Host *\n SetEnv EXISTING=yes\nHost prod\n User deploy\n')
    result = preview_config(tmp_path/'config', base, base, ('prod',), 1, Handle(), isolated=True)
    assert result.valid and result.hosts[0].matched
    assert not any('SSHPILOT_PREVIEW' in line for line in result.hosts[0].after)


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_invalid_saved_config_can_be_repaired(tmp_path):
    base = document('Host prod\n InvalidDirective yes\n')
    draft = replace(base, text='Host prod\n User deploy\n')
    result = preview_config(tmp_path/'config', base, draft, ('prod',), 0, Handle(), isolated=True)
    assert result.valid
    assert result.hosts[0].before == ()
    assert 'user deploy' in result.hosts[0].after


def test_readonly_and_invalid_edit_indexes_rejected():
    base = replace(document('Host prod\n User deploy\n'), writable=False)
    with pytest.raises(SshPilotError):
        edit_document(base, SshConfigManagerRequest(base.text, base.revision, 'remove'))
    for fields in [dict(action='move', position=4), dict(action='remove', block=2),
                   dict(action='remove_directive', directive=7), dict(action='directive', keyword='Match', value='all')]:
        with pytest.raises(SshPilotError):
            edit_document(replace(base, writable=True), SshConfigManagerRequest(base.text, base.revision, **fields))


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('ssh'), reason='POSIX OpenSSH process supervision')
def test_preview_cancellation_terminates_match_exec(tmp_path):
    from sshpilot.daemon.operation_runtime import OperationRuntime
    from sshpilot.api.models.operations import OperationKind
    started = tmp_path/'started'
    text = f'Match exec "touch {started}; sleep 30"\n User deploy\n'
    base = document(text)
    runtime = OperationRuntime()
    operation = runtime.start_operation(OperationKind.CONFIG_PREVIEW,
        lambda handle: (preview_config(tmp_path/'config', base, base, (), 0, handle, isolated=True), '')[1])
    try:
        deadline = time.monotonic()+5
        while not started.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert started.exists()
        runtime.cancel_operation(operation.operation_id)
        while time.monotonic()<deadline:
            current = runtime.get_operation(operation.operation_id)
            if current.state is OperationState.CANCELLED:
                break
            time.sleep(.01)
        assert current.state is OperationState.CANCELLED
    finally:
        runtime.shutdown()


def test_add_rename_remove_and_directive_edits_preserve_unrelated_bytes():
    base = document('# preserved\nHost old\n  User deploy\n  # note\n')
    def edit(current, **fields):
        return edit_document(base, SshConfigManagerRequest(current, base.revision, **fields)).document.text
    renamed = edit(base.text, action='header', block=1, value='prod-* !prod-test')
    added = edit(renamed, action='add', position=2, keyword='Match', value='all')
    directive = edit(added, action='directive', block=2, keyword='Port', value='2222')
    assert directive.startswith('# preserved\nHost prod-* !prod-test\n  User deploy\n  # note\n')
    assert directive.endswith('Match all\n    Port 2222\n')
    removed_directive = edit(directive, action='remove_directive', block=2, directive=0)
    removed_block = edit(removed_directive, action='remove', block=2)
    assert removed_block == renamed


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_relative_includes_resolve_from_native_user_directory(tmp_path, monkeypatch):
    from sshpilot.platform.paths import openssh_user_dir
    extra = tmp_path/'extra.conf'
    extra.write_text('Host prod\n  User included\n')
    # Ubuntu OpenSSH also consults HOME while globbing the tilde-prefixed
    # Include; align it with the native home without writing anything there.
    monkeypatch.setenv('HOME', str(openssh_user_dir().parent))
    relative = os.path.relpath(extra, openssh_user_dir())
    base = document(f'Include "{relative}"\n')
    view = edit_document(base, SshConfigManagerRequest())
    assert extra in [type(extra)(p).resolve() for p in view.blocks[0].included_files]
    result = preview_config(tmp_path/'config', base, base, ('prod',), 0, Handle(), isolated=True)
    assert result.valid and 'user included' in result.hosts[0].after


def test_query_failure_is_explicit_when_openssh_is_missing(monkeypatch):
    from sshpilot.core import ssh_config_effective
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(ssh_config_effective.subprocess, 'Popen', unavailable)
    values, diagnostics = ssh_config_effective.query_ssh_config('prod', '/config')
    assert not values
    assert diagnostics == ('OpenSSH could not be started',)


@pytest.mark.skipif(not shutil.which('ssh'), reason='requires OpenSSH')
def test_system_relative_includes_and_forwarding_options_are_preserved(tmp_path, monkeypatch):
    from sshpilot.core import ssh_config_manager
    system_dir = tmp_path/'system'
    system_dir.mkdir()
    included = system_dir/'defaults with spaces.conf'
    included.write_text('Host prod\n  Port 2223\n  User system-user\n  LocalForward 8080 localhost:80\n')
    system = system_dir/'ssh_config'
    system.write_text('Include "defaults with spaces.conf"\n')
    monkeypatch.setattr(ssh_config_manager, 'openssh_system_config', lambda: str(system))
    monkeypatch.setattr(ssh_config_manager, 'is_openssh_user_config', lambda _path: True)
    base = document('Host prod\n User user-file\n')
    result = preview_config(tmp_path/'config', base, base, ('prod',), 0, Handle())
    assert result.valid, result.diagnostics
    assert 'user user-file' in result.hosts[0].after
    assert 'port 2223' in result.hosts[0].after
    assert any(line.startswith('localforward ') for line in result.hosts[0].after)
    isolated = preview_config(tmp_path/'config', base, base, ('prod',), 0, Handle(), isolated=True)
    assert 'port 22' in isolated.hosts[0].after
    assert not any(line.startswith('localforward ') for line in isolated.hosts[0].after)

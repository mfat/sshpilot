import subprocess

import pytest

from sshpilot import scp_utils
from sshpilot.scp_utils import (
    SFTP_UNAVAILABLE_MESSAGE,
    assemble_scp_transfer_args,
    classify_sftp_error,
    download_file,
    insert_legacy_scp_flag,
    legacy_scp_flag_unsupported,
)
from sshpilot import ssh_password_exec


def test_insert_legacy_scp_flag_inserts_after_binary():
    assert insert_legacy_scp_flag(['scp', '-v', '-P', '22']) == ['scp', '-O', '-v', '-P', '22']


def test_insert_legacy_scp_flag_is_idempotent():
    argv = ['scp', '-O', '-v']
    assert insert_legacy_scp_flag(argv) == ['scp', '-O', '-v']


@pytest.mark.parametrize('text,expected', [
    ('unknown option -- O', True),
    ('scp: illegal option -- O', True),
    ('subsystem request failed', False),
    ('', False),
    (None, False),
])
def test_legacy_scp_flag_unsupported(text, expected):
    assert legacy_scp_flag_unsupported(text) is expected


def test_download_file_retries_with_legacy_flag_on_missing_sftp(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, check, text, capture_output, env):
        calls.append(list(argv))

        class _Result:
            def __init__(self, rc, err):
                self.returncode = rc
                self.stderr = err

        # First attempt (no -O) fails with a missing-SFTP error; the -O retry succeeds.
        if '-O' in argv:
            return _Result(0, '')
        return _Result(1, 'subsystem request failed on channel 0\r\n')

    monkeypatch.setattr(scp_utils.subprocess, 'run', fake_run)

    details = {}
    result = download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(tmp_path / 'dest'),
        result_details=details,
    )

    assert result is True
    assert len(calls) == 2
    assert '-O' not in calls[0]
    assert '-O' in calls[1]
    assert details == {}


def test_download_file_no_retry_on_unrelated_error(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, check, text, capture_output, env):
        calls.append(list(argv))

        class _Result:
            returncode = 1
            stderr = 'scp: /remote/file.txt: No such file or directory\r\n'

        return _Result()

    monkeypatch.setattr(scp_utils.subprocess, 'run', fake_run)

    details = {}
    result = download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(tmp_path / 'dest'),
        result_details=details,
    )

    assert result is False
    assert len(calls) == 1
    assert 'friendly' not in details


def test_download_file_legacy_retry_attempted_once(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, check, text, capture_output, env):
        calls.append(list(argv))

        class _Result:
            returncode = 1
            stderr = 'subsystem request failed on channel 0\r\n'

        return _Result()

    monkeypatch.setattr(scp_utils.subprocess, 'run', fake_run)

    details = {}
    result = download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(tmp_path / 'dest'),
        result_details=details,
    )

    assert result is False
    # One initial attempt + exactly one legacy (-O) retry.
    assert len(calls) == 2
    assert '-O' in calls[1]
    assert details['friendly'] == SFTP_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize('error_text', [
    'subsystem request failed on channel 0',
    'ash: /usr/lib/openssh/sftp-server: not found',
    'Connection closed by remote host',
    'received message too long 1450741611',
    'EOF during negotiation',
])
def test_classify_sftp_error_detects_missing_server(error_text):
    assert classify_sftp_error(error_text) == SFTP_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize('error_text', [
    None,
    '',
    'Permission denied (publickey,password).',
    'No such file or directory',
])
def test_classify_sftp_error_ignores_unrelated(error_text):
    assert classify_sftp_error(error_text) is None


def test_download_file_populates_friendly_details_on_subsystem_failure(monkeypatch, tmp_path):
    def fake_run(argv, check, text, capture_output, env):
        class _Result:
            returncode = 1
            stderr = 'subsystem request failed on channel 0\r\nlost connection\r\n'

        return _Result()

    monkeypatch.setattr(scp_utils.subprocess, 'run', fake_run)

    details = {}
    result = download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(tmp_path / 'dest'),
        result_details=details,
    )

    assert result is False
    assert details['friendly'] == SFTP_UNAVAILABLE_MESSAGE
    assert 'subsystem request failed' in details['stderr']


def test_assemble_scp_transfer_args_upload():
    sources, destination = assemble_scp_transfer_args(
        'alice@example.com',
        ['file1.txt', 'dir/archive.tar'],
        '/var/tmp',
        'upload',
    )
    assert sources == ['file1.txt', 'dir/archive.tar']
    assert destination == 'alice@example.com:/var/tmp'


def test_assemble_scp_transfer_args_upload_ipv6():
    sources, destination = assemble_scp_transfer_args(
        'alice@[2001:db8::1]',
        ['file1.txt'],
        '/var/tmp',
        'upload',
    )
    assert sources == ['file1.txt']
    assert destination == 'alice@[2001:db8::1]:/var/tmp'


def test_assemble_scp_transfer_args_download_normalizes_host():
    sources, destination = assemble_scp_transfer_args(
        'alice@example.com',
        ['~/logs/app.log', 'example.com:/opt/data.tar'],
        '/tmp/out',
        'download',
    )
    assert sources[0] == 'alice@example.com:~/logs/app.log'
    assert sources[1] == 'example.com:/opt/data.tar'
    assert destination == '/tmp/out'


def test_assemble_scp_transfer_args_download_ipv6():
    sources, destination = assemble_scp_transfer_args(
        'alice@[2001:db8::1]',
        ['~/logs/app.log', '[2001:db8::1]:/opt/data.tar', 'bob@[2001:db8::2]:/srv/backup'],
        '/tmp/out',
        'download',
    )
    assert sources[0] == 'alice@[2001:db8::1]:~/logs/app.log'
    assert sources[1] == '[2001:db8::1]:/opt/data.tar'
    assert sources[2] == 'bob@[2001:db8::2]:/srv/backup'
    assert destination == '/tmp/out'


@pytest.mark.parametrize('direction,expected_path', [
    ('upload', 'alice@example.com:/remote/dir'),
    ('download', '/local/dir'),
])
def test_run_scp_with_password_builds_command(monkeypatch, direction, expected_path):
    recorded = {}

    tmpdir = []

    original_mkdtemp = ssh_password_exec.tempfile.mkdtemp

    def fake_mkdtemp(prefix=None):
        path = original_mkdtemp(prefix=prefix)
        tmpdir.append(path)
        return path

    monkeypatch.setattr(ssh_password_exec.tempfile, 'mkdtemp', fake_mkdtemp)

    original_exists = ssh_password_exec.os.path.exists
    original_access = ssh_password_exec.os.access

    def fake_exists(path):
        if path == '/app/bin/sshpass':
            return False
        return original_exists(path)

    def fake_access(path, mode):
        if path == '/app/bin/sshpass':
            return False
        return original_access(path, mode)

    monkeypatch.setattr(ssh_password_exec.os.path, 'exists', fake_exists)
    monkeypatch.setattr(ssh_password_exec.os, 'access', fake_access)

    def fake_which(binary):
        if binary == 'sshpass':
            return '/usr/bin/sshpass'
        if binary == 'scp':
            return '/usr/bin/scp'
        return None

    monkeypatch.setattr(ssh_password_exec.shutil, 'which', fake_which)

    def fake_run(cmd, env, text, capture_output, check):
        recorded['cmd'] = cmd
        recorded['env'] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ssh_password_exec.subprocess, 'run', fake_run)

    result = ssh_password_exec.run_scp_with_password(
        'example.com',
        'alice',
        'topsecret',
        ['/local/file'] if direction == 'upload' else ['~/remote.dat'],
        '/remote/dir' if direction == 'upload' else '/local/dir',
        direction=direction,
        port=2222,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    cmd = recorded['cmd']
    assert cmd[0] == '/usr/bin/sshpass'
    assert cmd[1] == '-f'
    assert cmd[3] == '/usr/bin/scp'
    assert cmd[4:7] == ['-v', '-P', '2222']
    assert expected_path == cmd[-1]
    if direction == 'download':
        assert 'alice@example.com:~/remote.dat' in cmd
    else:
        assert 'alice@example.com:/remote/dir' == cmd[-1]

    # Cleanup temp directories created during the test
    for path in tmpdir:
        ssh_password_exec.shutil.rmtree(path, ignore_errors=True)

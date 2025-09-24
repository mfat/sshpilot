import subprocess

import pytest

from sshpilot.scp_utils import assemble_scp_transfer_args
from sshpilot import ssh_password_exec


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

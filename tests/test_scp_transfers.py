
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


def test_download_file_recursive_includes_r(monkeypatch, tmp_path):
    """Directory downloads must pass `-r` (regression for issue #1002: directory
    downloads ran without `-r` and failed with 'not a regular file')."""
    calls = []

    def fake_run(argv, check, text, capture_output, env):
        calls.append(list(argv))

        class _Result:
            returncode = 0
            stderr = ''

        return _Result()

    monkeypatch.setattr(scp_utils.subprocess, 'run', fake_run)

    result = download_file(
        'example.com',
        'alice',
        '/remote/dir',
        str(tmp_path / 'dest'),
        recursive=True,
    )

    assert result is True
    assert calls and '-r' in calls[0]


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

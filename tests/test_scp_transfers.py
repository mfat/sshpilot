
import pytest

from sshpilot.scp_utils import (
    SFTP_UNAVAILABLE_MESSAGE,
    assemble_scp_transfer_args,
    classify_sftp_error,
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

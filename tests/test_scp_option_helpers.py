"""Characterization tests for ScpWindowController's pure SCP-option helpers.

Pin the current behavior of _append_scp_option_pair and
_extend_scp_options_from_connection (both GTK-free logic) before any refactor.
_build_scp_argv is covered by test_window_scp_args.py; these fill the gaps in
the option-assembly logic underneath it.
"""

import types
import pytest

scp_window = pytest.importorskip("sshpilot.scp_window")
ScpWindowController = scp_window.ScpWindowController


def _ctrl(window=None):
    c = ScpWindowController.__new__(ScpWindowController)
    c.window = window if window is not None else types.SimpleNamespace()
    return c


# --- _append_scp_option_pair ---------------------------------------------


def test_append_skips_empty_value():
    c = _ctrl()
    opts = []
    c._append_scp_option_pair(opts, '-o', '')
    c._append_scp_option_pair(opts, '-o', None)
    assert opts == []


def test_append_adds_flag_value_pair():
    c = _ctrl()
    opts = []
    c._append_scp_option_pair(opts, '-o', 'ForwardAgent=yes')
    assert opts == ['-o', 'ForwardAgent=yes']


def test_append_dedups_identical_pair():
    c = _ctrl()
    opts = ['-o', 'ForwardAgent=yes']
    c._append_scp_option_pair(opts, '-o', 'ForwardAgent=yes')
    assert opts == ['-o', 'ForwardAgent=yes']  # not appended twice


def test_append_F_expands_and_skips_missing(tmp_path):
    c = _ctrl()
    opts = []
    # A -F path that does not exist is dropped entirely.
    c._append_scp_option_pair(opts, '-F', str(tmp_path / 'nope' / 'ssh_config'))
    assert opts == []
    # An existing -F path is stored absolute/expanded.
    real = tmp_path / 'ssh_config'
    real.write_text('')
    c._append_scp_option_pair(opts, '-F', str(real))
    assert opts == ['-F', str(real)]


# --- _extend_scp_options_from_connection ---------------------------------


def _conn(**kw):
    base = dict(config_root='', proxy_jump=[], proxy_command='',
                forward_agent=False, certificate='', extra_ssh_config='')
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_extend_proxy_jump_joined_with_commas():
    c = _ctrl()
    opts = []
    c._extend_scp_options_from_connection(_conn(proxy_jump=['a', ' b ', '']), opts)
    assert opts == ['-o', 'ProxyJump=a,b']


def test_extend_proxy_command_and_forward_agent():
    c = _ctrl()
    opts = []
    c._extend_scp_options_from_connection(
        _conn(proxy_command='corkscrew p 8080 %h %p', forward_agent=True), opts)
    assert opts == ['-o', 'ProxyCommand=corkscrew p 8080 %h %p',
                    '-o', 'ForwardAgent=yes']


def test_extend_certificate_only_when_file_exists(tmp_path):
    c = _ctrl()
    opts = []
    c._extend_scp_options_from_connection(_conn(certificate=str(tmp_path / 'missing')), opts)
    assert opts == []  # non-existent cert is skipped
    cert = tmp_path / 'id-cert.pub'
    cert.write_text('')
    c._extend_scp_options_from_connection(_conn(certificate=str(cert)), [])
    opts2 = []
    c._extend_scp_options_from_connection(_conn(certificate=str(cert)), opts2)
    assert opts2 == ['-o', f'CertificateFile={cert}']


def test_extend_extra_ssh_config_skips_blank_and_comments():
    c = _ctrl()
    opts = []
    extra = "# comment\n\n  Compression yes  \nServerAliveInterval 30\n"
    c._extend_scp_options_from_connection(_conn(extra_ssh_config=extra), opts)
    assert opts == ['-o', 'Compression yes', '-o', 'ServerAliveInterval 30']


def test_extend_uses_manager_ssh_config_path_when_no_config_root(tmp_path):
    cfg_file = tmp_path / 'ssh_config'
    cfg_file.write_text('')
    window = types.SimpleNamespace(
        connection_manager=types.SimpleNamespace(ssh_config_path=str(cfg_file)))
    c = _ctrl(window)
    opts = []
    c._extend_scp_options_from_connection(_conn(config_root=''), opts)
    assert opts == ['-F', str(cfg_file)]

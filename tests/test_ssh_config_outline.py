"""parse_ssh_config_outline extracts Host/Match headers (with line indices) for
the SSH config editor's navigation sidebar."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.text_editor import parse_ssh_config_outline


SAMPLE = """\
# my ssh config
Host web db
    HostName web.example
    Port 22

Host prod
    HostName prod.example

# Host commented-out
Match host *.internal
    ForwardAgent yes
"""


def test_extracts_hosts_and_match_with_line_indices():
    out = parse_ssh_config_outline(SAMPLE)
    assert out == [
        (1, "host", "web db"),
        (5, "host", "prod"),
        (9, "match", "host *.internal"),
    ]


def test_ignores_comments_and_value_keywords():
    # '# Host ...' is a comment; 'HostName' must not match as a Host header.
    out = parse_ssh_config_outline("# Host x\nHostName y\n    HostName z\n")
    assert out == []


def test_indented_host_is_matched():
    out = parse_ssh_config_outline("\tHost tabbed\n")
    assert out == [(0, "host", "tabbed")]


def test_host_without_pattern_falls_back_to_keyword():
    out = parse_ssh_config_outline("Host\n")
    assert out == [(0, "host", "Host")]


def test_empty_text():
    assert parse_ssh_config_outline("") == []
    assert parse_ssh_config_outline(None) == []

"""Which of a host's own values an earlier SSH config block keeps unused."""

from __future__ import annotations

from sshpilot.core.ssh_config_effective import (
    collect_host_block_lines,
    losing_directives,
    normalized_directive_value,
    own_block_values,
)


def test_own_block_values_keep_the_first_value_and_skip_collected_options():
    lines = [
        "Host web",
        "    User deploy",
        "    # User commented",
        "    IdentityFile ~/.ssh/a",
        "    User second",
        '    ProxyCommand "nc %h %p"',
    ]
    assert own_block_values(lines) == {"user": "deploy", "proxycommand": "nc %h %p"}


def test_normalized_values_ignore_case_where_openssh_does():
    assert normalized_directive_value("ForwardAgent", "NO") == "no"
    assert normalized_directive_value("HostName", "Web.Example") == "web.example"
    assert normalized_directive_value("ProxyJump", "a, b c") == "a,b,c"
    assert normalized_directive_value("User", "Admin") == "Admin"


def test_losing_directives_report_only_values_that_differ(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host *\n    User admin\n    Port 22\n\n"
        "Host web\n    User deploy\n    Port 22\n\n"
        "Host *\n    Compression yes\n",
        encoding="utf-8",
    )
    found = losing_directives("web", str(root), collect_host_block_lines("web", str(root)))
    assert [(item["key"], item["own"], item["value"], item["line"]) for item in found] == [
        ("user", "deploy", "admin", 2),
    ]


def test_losing_directives_follow_an_include_read_before_the_host(tmp_path):
    common = tmp_path / "common.conf"
    common.write_text("Host *\n    User shared\n", encoding="utf-8")
    root = tmp_path / "config"
    root.write_text(
        f"Include {common}\n\nHost web\n    User deploy\n", encoding="utf-8"
    )
    found = losing_directives("web", str(root), collect_host_block_lines("web", str(root)))
    assert [(item["file"], item["line"], item["block"]) for item in found] == [
        (str(common), 2, "Host *"),
    ]

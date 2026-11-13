"""Which of a host's own values an earlier SSH config block keeps unused."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from sshpilot.core.ssh_config_effective import (
    ConfigScanCache,
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


def test_a_later_block_of_the_same_host_loses_to_a_wildcard_between_them(tmp_path):
    """OpenSSH reads every block naming the host; a `Host *` between two of
    them decides an option the second sets (it uses `admin` here)."""
    root = tmp_path / "config"
    root.write_text(
        "Host web\n    HostName web.example.com\n\n"
        "Host *\n    User admin\n\n"
        "Host web\n    User deploy\n",
        encoding="utf-8",
    )
    found = losing_directives("web", str(root), collect_host_block_lines("web", str(root)))
    assert [(item["key"], item["own"], item["value"], item["line"]) for item in found] == [
        ("user", "deploy", "admin", 5),
    ]


def test_an_option_the_first_own_block_sets_keeps_its_value(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host web\n    User deploy\n\nHost *\n    User admin\n    Port 7\n\n"
        "Host web\n    Port 2200\n\nHost *\n    Compression yes\n",
        encoding="utf-8",
    )
    found = losing_directives("web", str(root), collect_host_block_lines("web", str(root)))
    # User: the host's own first. Port: `Host *` between the blocks wins.
    # Compression: the host never sets it.
    assert [(item["key"], item["value"]) for item in found] == [("port", "7")]


_OPENSSH_CASES = {
    "between": "Host web\n HostName a\n\nHost *\n User admin\n\nHost web\n User deploy\n",
    "own-first": "Host web\n User deploy\n\nHost *\n User admin\n\nHost web\n Port 2200\n",
    "own-twice": "Host web\n User one\n\nHost *\n User admin\n\nHost web\n User two\n",
    "pattern": "Host web\n Port 1\n\nHost w*\n User pat\n Port 9\n\nHost web\n User me\n",
    "star-top": "Host *\n User admin\n\nHost web\n User deploy\n\nHost web\n Port 2\n",
    "multi-name": "Host other web\n Port 3\n\nHost *\n Port 4\n User z\n\nHost web\n User y\n",
    "negated": "Host web\n Port 1\n\nHost * !web\n User no\n\nHost web\n User yes\n",
}


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
@pytest.mark.parametrize("name", sorted(_OPENSSH_CASES))
def test_losing_directives_agree_with_ssh_g(tmp_path, name):
    """A reported value is exactly one `ssh -G` does not use."""
    root = tmp_path / "config"
    root.write_text(_OPENSSH_CASES[name], encoding="utf-8")
    out = subprocess.run(
        ["ssh", "-G", "-F", str(root), "web"], capture_output=True, text=True, check=True
    ).stdout
    used = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        used.setdefault(key, value)
    own = own_block_values(collect_host_block_lines("web", str(root)))
    expected = {key for key, value in own.items() if used.get(key, "").lower() != value.lower()}
    found = losing_directives("web", str(root), collect_host_block_lines("web", str(root)))
    assert {item["key"] for item in found} == expected


def test_a_scan_cache_gives_the_same_answers(tmp_path):
    common = tmp_path / "conf.d"
    common.mkdir()
    (common / "a.conf").write_text(
        "Host a1\n    User mine\n\nHost a*\n    Port 9\n\nHost a2\n    Port 22\n",
        encoding="utf-8",
    )
    root = tmp_path / "config"
    root.write_text(
        f"Host *\n    User admin\n\nInclude {common}/*.conf\n\n"
        "Host b1\n    User admin\n\nHost b1\n    Port 3\n",
        encoding="utf-8",
    )
    cache = ConfigScanCache()
    for host in ("a1", "a2", "b1", "missing"):
        plain = losing_directives(host, str(root), collect_host_block_lines(host, str(root)))
        cached = losing_directives(
            host, str(root), collect_host_block_lines(host, str(root), cache=cache),
            cache=cache,
        )
        assert cached == plain, host


def test_launch_file_lookup_with_a_scan_cache_matches_the_plain_one(tmp_path):
    """A root outside ~/.ssh: a host in a reachable include resolves through
    the root, one OpenSSH cannot reach through its own file. The cached walk
    of what OpenSSH reaches must agree."""
    from sshpilot.core.ssh_config_effective import launch_config_file

    (tmp_path / "inc").mkdir()
    reachable = tmp_path / "inc" / "a.conf"
    reachable.write_text("Host a\n    Port 1\n", encoding="utf-8")
    stray = tmp_path / "stray.conf"
    stray.write_text("Host s\n    Port 2\n", encoding="utf-8")
    root = tmp_path / "config"
    root.write_text(f"Include {tmp_path}/inc/*.conf\n", encoding="utf-8")

    cache = ConfigScanCache()
    for source in (reachable, stray, root, None):
        plain = launch_config_file(str(root), str(source) if source else None)
        cached = launch_config_file(str(root), str(source) if source else None, cache=cache)
        assert cached == plain
    assert launch_config_file(str(root), str(reachable), cache=cache) == str(root)
    assert launch_config_file(str(root), str(stray), cache=cache) == str(stray)

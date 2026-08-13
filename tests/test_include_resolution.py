"""Include resolution: fragments are loaded, sources are attributed, and edits
target the fragment that actually defines the host while native SSH still
evaluates the root config so the full Include tree applies."""

import logging
import shutil
import subprocess

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.ssh_config_utils import resolve_ssh_config_files
from sshpilot.ssh_connection_builder import build_native_command


def test_include_directives_parsed(tmp_path):
    main_cfg = tmp_path / "config"
    inc_dir = tmp_path / "conf.d"
    inc_dir.mkdir()
    a_cfg = inc_dir / "a.conf"
    b_cfg = inc_dir / "b.conf"
    a_cfg.write_text("\n".join([
        "Host hosta",
        "    HostName hosta.example.com",
    ]))
    b_cfg.write_text("\n".join([
        "Host hostb",
        "    HostName hostb.example.com",
    ]))
    main_cfg.write_text("\n".join([
        "Host main",
        "    HostName main.example.com",
        "Include conf.d/*.conf",
    ]))

    loaded = load_ssh_configuration(main_cfg, isolated=False)
    connections = {c.id: c for c in loaded.connections}
    assert set(connections) == {"main", "hosta", "hostb"}
    sources = {cid: str(c.source) for cid, c in connections.items()}
    assert sources["main"] == str(main_cfg)
    assert sources["hosta"] == str(a_cfg)
    assert sources["hostb"] == str(b_cfg)
    from pathlib import Path
    assert set(loaded.source_paths) == {
        Path(main_cfg).resolve(),
        Path(a_cfg).resolve(),
        Path(b_cfg).resolve(),
    }

    hosta = connections["hosta"]
    new_data = dict(hosta.data)
    new_data["port"] = 2222
    SshConfigStore(a_cfg).update(
        "hosta", new_data, expected_generation=hosta.generation
    )
    assert "Port 2222" in a_cfg.read_text()
    assert "Port 2222" not in main_cfg.read_text()


def test_included_host_native_commands_use_root_config(tmp_path):
    """Native commands must start at the root config, not the host's fragment.

    ``source`` remains the fragment used for editing, while ``-F`` must name the
    root so OpenSSH follows the complete Include tree and applies Host * globals.
    """
    main_cfg = tmp_path / "config"
    inc_dir = tmp_path / "conf.d"
    inc_dir.mkdir()
    host_cfg = inc_dir / "production.conf"

    host_cfg.write_text("\n".join([
        "Host production",
        "    HostName 10.0.0.50",
        "    User deploy",
        "",
    ]))
    main_cfg.write_text("\n".join([
        "Include conf.d/*.conf",
        "",
        "Host *",
        "    ForwardAgent yes",
        "",
    ]))

    loaded = load_ssh_configuration(main_cfg, isolated=False)
    connection = next(c for c in loaded.connections if c.id == "production")
    assert str(connection.source) == str(host_cfg)

    command = build_native_command(connection, config_file=str(main_cfg))
    assert "-F" in command
    selected_config = command[command.index("-F") + 1]
    assert selected_config == str(main_cfg), (
        "Native SSH must evaluate the root config; selecting the included "
        "fragment drops root Host * directives and sibling includes"
    )

    if shutil.which("ssh") is not None:
        command = list(command)
        command.insert(command.index("production"), "-G")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        effective = dict(
            line.split(None, 1)
            for line in result.stdout.splitlines()
            if line.strip() and " " in line
        )
        assert effective["forwardagent"] == "yes"


def test_nested_include_sources(tmp_path):
    main_cfg = tmp_path / "config"
    level1_cfg = tmp_path / "level1.conf"
    level2_dir = tmp_path / "level2"
    level2_dir.mkdir()
    level2_cfg = level2_dir / "level2.conf"

    level2_cfg.write_text("\n".join([
        "Host nested",
        "    HostName nested.example.com",
    ]))

    level1_cfg.write_text("\n".join([
        "Host mid",
        "    HostName mid.example.com",
        f"Include {level2_dir}/*.conf",
    ]))

    main_cfg.write_text("\n".join([
        "Host top",
        "    HostName top.example.com",
        f"Include {level1_cfg}",
    ]))

    loaded = load_ssh_configuration(main_cfg, isolated=False)
    sources = {c.id: str(c.source) for c in loaded.connections}
    assert sources["top"] == str(main_cfg)
    assert sources["mid"] == str(level1_cfg)
    assert sources["nested"] == str(level2_cfg)


def test_include_cycle_detected(tmp_path, caplog):
    a_cfg = tmp_path / "a.conf"
    b_cfg = tmp_path / "b.conf"
    a_cfg.write_text("Include b.conf\n")
    b_cfg.write_text("Include a.conf\n")
    caplog.set_level(logging.WARNING)
    files = resolve_ssh_config_files(str(a_cfg))
    assert files == [str(a_cfg.resolve()), str(b_cfg.resolve())]
    assert any("cycle" in msg for msg in caplog.messages)


def test_directory_only_include(tmp_path):
    main_cfg = tmp_path / "config"
    inc_dir = tmp_path / "conf.d"
    inc_dir.mkdir()
    (inc_dir / "a").write_text("Host a\n    HostName a.example.com\n")
    (inc_dir / "b").write_text("Host b\n    HostName b.example.com\n")
    main_cfg.write_text("Include conf.d\n")
    files = resolve_ssh_config_files(str(main_cfg))
    assert files == [str(main_cfg.resolve()), str((inc_dir / 'a').resolve()), str((inc_dir / 'b').resolve())]


def test_env_var_expansion(tmp_path, monkeypatch):
    inc_dir = tmp_path / "envdir"
    inc_dir.mkdir()
    cfg = inc_dir / "c.conf"
    cfg.write_text("Host c\n    HostName c.example.com\n")
    main_cfg = tmp_path / "config"
    monkeypatch.setenv("EXTRA", str(inc_dir))
    main_cfg.write_text("Include $EXTRA/c.conf\n")
    files = resolve_ssh_config_files(str(main_cfg))
    assert files == [str(main_cfg.resolve()), str(cfg.resolve())]

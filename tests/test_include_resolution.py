import asyncio
import logging
from sshpilot.connection_manager import ConnectionManager
from sshpilot.ssh_config_utils import resolve_ssh_config_files

# Ensure an event loop for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())

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

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(main_cfg)
    cm.load_ssh_config()

    names = {c.nickname for c in cm.connections}
    assert names == {"main", "hosta", "hostb"}
    sources = {c.nickname: c.source for c in cm.connections}
    assert sources["main"] == str(main_cfg)
    assert sources["hosta"] == str(a_cfg)
    assert sources["hostb"] == str(b_cfg)

    hosta = next(c for c in cm.connections if c.nickname == "hosta")
    new_data = dict(hosta.data)
    new_data["port"] = 2222
    cm.update_ssh_config_file(hosta, new_data)
    assert "Port 2222" in a_cfg.read_text()
    assert "Port 2222" not in main_cfg.read_text()


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

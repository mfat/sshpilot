import asyncio
from sshpilot.connection_manager import ConnectionManager

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


from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration


def _load(tmp_path, text: str):
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def test_parse_and_load_proxy_directives(tmp_path):
    result = _load(
        tmp_path,
        "\n".join(
            [
                "Host proxycmd",
                "    HostName example.com",
                "    ProxyCommand ssh -W %h:%p bastion",
                "",
                "Host proxyjump",
                "    HostName example.com",
                "    ProxyJump bastion",
                "",
                "Host multijump",
                "    HostName example.com",
                "    ProxyJump bast1,bast2",
                "    ForwardAgent yes",
                "",
            ]
        )
    )

    by_id = {c.id: c for c in result.connections}
    assert set(by_id) == {"proxycmd", "proxyjump", "multijump"}

    assert by_id["proxycmd"].data["proxy_command"] == "ssh -W %h:%p bastion"
    assert by_id["proxyjump"].data["proxy_jump"] == ["bastion"]
    assert by_id["multijump"].data["proxy_jump"] == ["bast1", "bast2"]
    assert by_id["multijump"].data["forward_agent"] is True


from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration


def _load(tmp_path, text: str):
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def test_extra_config_contains_expected_directives(tmp_path):
    result = _load(
        tmp_path,
        "Host primary\n    HostName example.com\n    Compression yes\n",
    )
    record = result.connections[0]
    extra = record.data.get("extra_ssh_config", "") or ""
    assert "compression yes" in extra.lower()

import os

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration


def test_certificatefile_persistence(tmp_path):
    cert_path = os.path.expanduser("~/cert.pub")
    path = tmp_path / "config"
    path.write_text(
        "Host example\n    HostName example.com\n    CertificateFile ~/cert.pub\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(path, isolated=False)
    record = loaded.connections[0]
    assert record.data.get("certificate") == cert_path
    assert record.data.get("certificate_files") == [cert_path]

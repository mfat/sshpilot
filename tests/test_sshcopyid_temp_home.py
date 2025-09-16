from sshpilot.ssh_utils import ensure_writable_ssh_home


def test_ensure_writable_home_flatpak(monkeypatch, tmp_path):
    monkeypatch.setenv("FLATPAK_ID", "io.github.mfat.sshpilot")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    env = {}
    ensure_writable_ssh_home(env)
    expected = tmp_path / "sshcopyid-home"
    assert env["HOME"] == str(expected)
    assert (expected / ".ssh").is_dir()


def test_ensure_writable_home_non_flatpak(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    env = {}
    ensure_writable_ssh_home(env)
    assert "HOME" not in env

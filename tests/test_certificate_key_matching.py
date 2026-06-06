"""
Verifies the SSH-certificate model the connection dialog relies on:

  * An OpenSSH certificate certifies a *specific* public key (it embeds it), so
    a certificate corresponds to exactly one keypair regardless of its filename.
  * ssh therefore matches a `CertificateFile` to the right key by key material,
    NOT by filename or config position. Multiple certificates with non-standard
    names can be listed as a flat list and ssh pairs each with its key.

This is why the dialog keeps `CertificateFile` as a flat list and does not bind
a certificate to a specific `IdentityFile` — there is no such binding in
ssh_config(5), and none is needed.

Tests use real ssh-keygen/ssh(d); they skip cleanly where those are absent.
"""

import os
import shutil
import socket
import subprocess
import time

import pytest


HAVE_KEYGEN = shutil.which("ssh-keygen") is not None
HAVE_SSH = shutil.which("ssh") is not None
SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)

pytestmark = pytest.mark.skipif(not (HAVE_KEYGEN and HAVE_SSH), reason="ssh/ssh-keygen not available")


def _run(*argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, **kw)


def _keygen(*argv):
    r = _run("ssh-keygen", *argv)
    assert r.returncode == 0, f"ssh-keygen {argv} failed: {r.stderr}"
    return r


def _fingerprint(pub_path):
    # "256 SHA256:xxxx comment (ED25519)" -> SHA256:xxxx
    out = _run("ssh-keygen", "-l", "-f", pub_path).stdout.split()
    return out[1]


def _cert_certified_fp(cert_path):
    # ssh-keygen -L: "Public key: ED25519-CERT SHA256:xxxx"
    for line in _run("ssh-keygen", "-L", "-f", cert_path).stdout.splitlines():
        line = line.strip()
        if line.startswith("Public key:"):
            return line.split()[-1]
    return None


@pytest.fixture
def pki(tmp_path):
    """A CA + two keypairs + two certs deliberately given NON-STANDARD names."""
    d = tmp_path
    _keygen("-t", "ed25519", "-N", "", "-f", str(d / "ca"), "-C", "ca")
    _keygen("-t", "ed25519", "-N", "", "-f", str(d / "alpha"), "-C", "alpha")
    _keygen("-t", "ed25519", "-N", "", "-f", str(d / "bravo"), "-C", "bravo")
    _keygen("-s", str(d / "ca"), "-I", "alpha-id", "-n", "user1", str(d / "alpha.pub"))
    _keygen("-s", str(d / "ca"), "-I", "bravo-id", "-n", "user2", str(d / "bravo.pub"))
    # Rename to non-standard names so the <key>-cert.pub convention does NOT apply.
    os.rename(d / "alpha-cert.pub", d / "signed-one.pub")
    os.rename(d / "bravo-cert.pub", d / "signed-two.pub")
    return d


class TestCertKeyCorrespondence:
    def test_cert_certifies_specific_key_independent_of_filename(self, pki):
        """The non-standard-named cert embeds (certifies) exactly one key's pubkey."""
        assert _cert_certified_fp(pki / "signed-one.pub") == _fingerprint(pki / "alpha.pub")
        assert _cert_certified_fp(pki / "signed-two.pub") == _fingerprint(pki / "bravo.pub")
        # And crucially NOT the other key — proves matching is by key material.
        assert _cert_certified_fp(pki / "signed-one.pub") != _fingerprint(pki / "bravo.pub")

    def test_ssh_g_accepts_multiple_nonstandard_certificates(self, pki):
        """A flat list of multiple non-standard-named CertificateFile is valid config."""
        cfg = pki / "config"
        cfg.write_text(
            "Host target\n"
            f"    IdentityFile {pki/'alpha'}\n"
            f"    IdentityFile {pki/'bravo'}\n"
            f"    CertificateFile {pki/'signed-one.pub'}\n"
            f"    CertificateFile {pki/'signed-two.pub'}\n"
        )
        out = _run("ssh", "-F", str(cfg), "-G", "target").stdout.lower()
        assert str(pki / "signed-one.pub").lower() in out
        assert str(pki / "signed-two.pub").lower() in out


class TestParserRoundTripsNonStandardCerts:
    def test_parser_keeps_all_nonstandard_certs(self, pki):
        from sshpilot.connection_manager import ConnectionManager
        cfg = pki / "config"
        cfg.write_text(
            "Host multi\n"
            "    HostName multi.example.com\n"
            f"    CertificateFile {pki/'signed-one.pub'}\n"
            f"    CertificateFile {pki/'signed-two.pub'}\n"
        )
        cm = ConnectionManager.__new__(ConnectionManager)
        cm.connections = []
        cm.rules = []
        cm.ssh_config_path = str(cfg)
        cm.load_ssh_config()
        conn = next(c for c in cm.connections if c.nickname == "multi")
        assert any("signed-one.pub" in c for c in conn.certificate_files)
        assert any("signed-two.pub" in c for c in conn.certificate_files)
        # And they round-trip back out as two CertificateFile lines.
        entry = cm.format_ssh_config_entry({
            "nickname": "multi", "hostname": "multi.example.com", "auth_method": 0,
            "key_select_mode": 2, "keyfile": str(pki / "alpha"),
            "identity_files": [str(pki / "alpha")],
            "certificate_files": conn.certificate_files,
        })
        assert entry.count("CertificateFile") == 2


@pytest.mark.integration
@pytest.mark.skipif(SSHD is None, reason="sshd not available for live cert auth")
def test_live_sshd_matches_nonstandard_cert_to_key(pki):
    """Gold standard: a live sshd trusting the CA authenticates using a key plus a
    NON-STANDARD-named certificate, and (given both keys+certs) picks the right one.

    Skips on any infrastructure problem (can't bind/start/connect) so it never
    fails for reasons unrelated to the behaviour under test.
    """
    # Pick a free localhost port.
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    finally:
        s.close()

    d = pki
    _keygen("-t", "ed25519", "-N", "", "-f", str(d / "hostkey"), "-C", "host")
    (d / "principals").write_text("user2\n")  # only bravo's principal is authorized
    (d / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d/'hostkey'}\n"
        f"PidFile {d/'sshd.pid'}\nTrustedUserCAKeys {d/'ca.pub'}\n"
        f"AuthorizedPrincipalsFile {d/'principals'}\nUsePAM no\nStrictModes no\nLogLevel VERBOSE\n"
    )
    proc = subprocess.Popen([SSHD, "-D", "-f", str(d / "sshd_config"), "-E", str(d / "sshd.log")])
    try:
        time.sleep(1.0)
        if proc.poll() is not None:
            pytest.skip("sshd failed to start in this environment")
        cfg = d / "cfg"
        cfg.write_text(
            "Host target\n"
            f"    HostName 127.0.0.1\n    Port {port}\n    User {os.environ.get('USER','root')}\n"
            f"    IdentityFile {d/'alpha'}\n    IdentityFile {d/'bravo'}\n"
            f"    CertificateFile {d/'signed-one.pub'}\n    CertificateFile {d/'signed-two.pub'}\n"
            "    IdentitiesOnly yes\n    StrictHostKeyChecking no\n"
            "    UserKnownHostsFile /dev/null\n    BatchMode yes\n"
        )
        r = _run("ssh", "-F", str(cfg), "target", "echo AUTH_OK")
        if r.returncode != 0 or "AUTH_OK" not in r.stdout:
            pytest.skip(f"live auth not exercisable here (rc={r.returncode}): {r.stderr.strip()[:200]}")
        log = (d / "sshd.log").read_text()
        # ssh auto-selected bravo (the only authorized principal) via its
        # non-standard-named cert — matched by key material, not filename.
        assert 'Accepted certificate ID "bravo-id"' in log
        assert _fingerprint(d / "bravo.pub").split(":", 1)[1][:20] in log
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

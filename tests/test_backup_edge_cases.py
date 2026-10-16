"""Regression tests for edge cases in the backup import/export flow.

These started as an adversarial bug-hunt; each test pins the correct behavior for a bug that
was found and fixed (remote-path rebasing, scrypt cost bound, Replace orphan fragments, dangling
Includes, read-only config capture, cross-mode absolute Includes, negated Host patterns).
"""
import os
import pytest

import sshpilot.backup_manager as bm
from sshpilot.backup_archive import _validate_enc_params, SpbkFormatError


class Cfg:
    config_data = {"secrets": {}}
    def get_setting(self, k, d=None): return d
    def get_default_config(self): return {}


class CM:
    def __init__(self, path, isolated=False):
        self.ssh_config_path = path
        self.isolated_mode = isolated
        self.known_hosts_path = None
    def get_connections(self): return []
    def load_ssh_config(self): pass


def _mgr(main_path, isolated=False, cfgdir=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(bm, "get_config_dir", lambda: cfgdir or os.path.dirname(main_path))
    return bm.BackupManager(Cfg(), CM(main_path, isolated))


REPLACE_OPTS = {"app_settings": False, "ssh_config": True, "known_hosts": False,
                "secrets": False, "private_keys": False}


# ---------------------------------------------------------------------------
# BUG 1 (correctness, HIGH): home-rebase rewrites REMOTE-side paths & comments.
# `_rebase_home_in_text` blindly replaces the source-home prefix everywhere in
# the config text, including RemoteCommand / RemoteForward targets that live on
# the REMOTE host and free-text comments.
# ---------------------------------------------------------------------------
def test_bug1_remote_command_path_must_not_be_rebased():
    txt = ("Host jump\n"
           "    RemoteCommand tail -f /home/alice/prod.log\n")
    out = bm._rebase_home_in_text(txt, "/home/alice", "/home/bob")
    # RemoteCommand runs on the REMOTE machine; rebasing it to the local target
    # home silently points at the wrong path.
    assert "/home/alice/prod.log" in out, f"remote path got rewritten:\n{out}"


# ---------------------------------------------------------------------------
# BUG 2 (security/DoS, HIGH): scrypt clamp bounds n, r, p independently, so
# n=2**20 AND r=32 pass -> ~4 GiB working set (and p=16 = 16x time). The clamp
# was meant to bound cost; it does not bound the PRODUCT.
# ---------------------------------------------------------------------------
def test_bug2_scrypt_clamp_should_bound_total_memory():
    mem_gib = 128 * (2 ** 20) * 32 / 2 ** 30
    with pytest.raises(SpbkFormatError):
        _validate_enc_params(b"x" * 16, b"y" * 12, 2 ** 20, 32, 16)
    # If we reach here the params were accepted -> DoS surface of ~%.0f GiB.
    print(f"\naccepted a ~{mem_gib:.0f} GiB scrypt derivation")


# ---------------------------------------------------------------------------
# BUG 3 (data/behavior, HIGH): "Replace" does not remove the target's own
# pre-existing Include fragments. If the restored main config globs conf.d/*,
# leftover local fragments survive and are silently merged back in.
# ---------------------------------------------------------------------------
def test_bug3_replace_should_not_keep_preexisting_fragment_hosts(tmp_path, monkeypatch):
    root = tmp_path / ".ssh"
    (root / "conf.d").mkdir(parents=True)
    main = root / "config"
    main.write_text("Include conf.d/*.conf\nHost target-main\n", encoding="utf-8")
    (root / "conf.d" / "leftover.conf").write_text(
        "Host leftover-host\n    HostName old.example\n", encoding="utf-8")

    mgr = _mgr(str(main), cfgdir=str(tmp_path / "cfg"), monkeypatch=monkeypatch)
    import_data = {
        "source_home": "/home/src",
        "ssh_config_main_rel": "config",
        "ssh_config_files": {
            "config": "Include conf.d/*.conf\nHost from-backup\n",
            "conf.d/base.conf": "Host backup-host\n",
        },
    }
    ok, err = mgr._import_replace(import_data, REPLACE_OPTS)
    assert ok, err
    names = bm._existing_host_names(str(main))
    # A clean Replace should reflect only the backup. The target's leftover host
    # must be gone.
    assert "leftover-host" not in names, f"orphan fragment host survived Replace: {sorted(names)}"


# ---------------------------------------------------------------------------
# BUG 4 (correctness, MEDIUM): export drops out-of-root Include *files* (good)
# but keeps their Include *line* in the main config, so Replace restores a
# dangling / foreign-system Include that was never backed up.
# ---------------------------------------------------------------------------
def test_bug4_replace_should_not_restore_dangling_include(tmp_path, monkeypatch):
    root = tmp_path / ".ssh"
    root.mkdir(parents=True)
    main = root / "config"
    mgr = _mgr(str(main), cfgdir=str(tmp_path / "cfg"), monkeypatch=monkeypatch)
    # The source referenced a system include that export could not bundle.
    import_data = {
        "source_home": "/home/src",
        "ssh_config_root": "/home/src/.ssh",
        "ssh_config_main_rel": "config",
        "ssh_config_files": {
            "config": "Include /etc/ssh/ssh_config.d/*.conf\nHost h\n    HostName h.ex\n",
        },
    }
    ok, err = mgr._import_replace(import_data, REPLACE_OPTS)
    assert ok, err
    restored = main.read_text()
    # The Include for a file that was never in the backup must not be an ACTIVE directive
    # pointing at the target's system config (commented-out is fine).
    active = [l for l in restored.splitlines()
              if l.strip().lower().startswith("include ") and "/etc/ssh" in l]
    assert not active, f"restored a dangling/foreign system Include:\n{restored}"


# ---------------------------------------------------------------------------
# BUG 5 (data loss, MEDIUM): a concrete host defined with a negation/pattern
# list (`Host prod !prod-db`) is dropped entirely on merge -> the host is lost.
# ---------------------------------------------------------------------------
def test_bug5_concrete_host_with_negation_should_survive_merge():
    new, dropped, _coll = bm._select_new_host_blocks(
        ["Host prod !prod-db\n    HostName p.example\n"], existing_names=set())
    assert "Host prod" in new, f"host with a negation pattern was dropped (dropped={dropped})"


# ---------------------------------------------------------------------------
# Splitting where OpenSSH does not and re-joining with LF would turn the tail of
# a comment into a real directive in the file we write.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sep", ["\x0c", "\r", "\x85", "\u2028"])
def test_comment_tail_is_not_rewritten_as_a_directive(sep):
    comment = f"    # note{sep}ProxyCommand touch /tmp/x"
    text = f"Host a\n    HostName a.example\n{comment}\n"
    new, _dropped, _coll = bm._select_new_host_blocks([text], existing_names=set())
    assert comment in new.split("\n")
    normalized = bm.BackupManager._normalize_fragment_include(None, text, "f.conf", "f.conf")
    assert comment in normalized.split("\n")


# ---------------------------------------------------------------------------
# BUG 6 (data loss, MEDIUM): the export tree filter requires os.W_OK, so a
# user's OWN but read-only (chmod 400 — a common hardening) config file is
# silently excluded from the backup. The W_OK filter was meant to skip files we
# don't control, but it also drops files we own yet made read-only.
# ---------------------------------------------------------------------------
def test_bug6_readonly_owned_fragment_should_be_backed_up(tmp_path):
    root = tmp_path / ".ssh"
    (root / "conf.d").mkdir(parents=True)
    main = root / "config"
    main.write_text("Include conf.d/*.conf\nHost m\n", encoding="utf-8")
    frag = root / "conf.d" / "hardened.conf"
    frag.write_text("Host secure-box\n    HostName s.example\n", encoding="utf-8")
    os.chmod(frag, 0o400)  # user-owned, read-only (defensive)
    try:
        tree, skipped = bm._gather_ssh_config_tree(str(main))
        # A file the user owns must be captured even if it is read-only.
        assert any("hardened.conf" in k for k in tree), \
            f"read-only owned config was dropped from backup; skipped={skipped}"
    finally:
        os.chmod(frag, 0o600)


# ---------------------------------------------------------------------------
# BUG 7 (correctness, MEDIUM): cross-mode Replace with an ABSOLUTE Include. The
# fragment is restored under the target root (isolated config dir), but the
# absolute Include line is rebased to the target HOME, so they no longer point
# at the same place -> orphaned fragment + dangling include.
# ---------------------------------------------------------------------------
def test_bug7_crossmode_absolute_include_stays_consistent(tmp_path, monkeypatch):
    # Target is ISOLATED: config root is the app config dir, NOT the home dir.
    home = tmp_path / "home_bob"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    iso_root = tmp_path / "cfg" / "sshpilot"
    iso_root.mkdir(parents=True)
    main = iso_root / "ssh_config"
    mgr = _mgr(str(main), isolated=True, cfgdir=str(tmp_path / "cfg"), monkeypatch=monkeypatch)

    # Source (default mode) used an absolute Include under its home.
    import_data = {
        "source_home": "/home/alice",
        "ssh_config_root": "/home/alice/.ssh",
        "ssh_config_main_rel": "config",
        "ssh_config_files": {
            "config": "Include /home/alice/.ssh/conf.d/x.conf\nHost m\n",
            "conf.d/x.conf": "Host frag\n    HostName f.ex\n",
        },
    }
    ok, err = mgr._import_replace(import_data, REPLACE_OPTS)
    assert ok, err
    from sshpilot.ssh_config_utils import resolve_ssh_config_files
    resolved = resolve_ssh_config_files(str(main))
    # The bundled fragment we wrote under the config root must actually be
    # reachable from the restored main config.
    frag_written = os.path.join(str(iso_root), "conf.d", "x.conf")
    assert os.path.abspath(frag_written) in {os.path.abspath(p) for p in resolved}, \
        f"restored fragment is orphaned; main resolves to {resolved}"


# ---------------------------------------------------------------------------
# A wildcard next to a concrete name makes the stanza machine-wide. Merge keeps
# the concrete name and must leave the wildcard behind.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["Host web *", "Host * web", "Host web ?*", 'Host web "*"'])
def test_wildcard_next_to_a_concrete_name_is_not_imported(header):
    new, dropped, _coll = bm._select_new_host_blocks(
        [f"{header}\n    ProxyCommand touch /tmp/x\n"], existing_names=set())
    assert new.splitlines() == ["Host web", "    ProxyCommand touch /tmp/x"]
    assert dropped == 1                           # reported like any other global rule


def test_negation_next_to_a_concrete_name_is_kept():
    new, dropped, _coll = bm._select_new_host_blocks(
        ["Host prod !prod-db\n    HostName p.example\n"], existing_names=set())
    assert new.splitlines()[0] == "Host prod !prod-db"
    assert dropped == 0


# The header that is written is rebuilt from the names that were checked, so that OpenSSH cannot
# read a host out of it that the check did not see ("web" is the user's own host here).
@pytest.mark.parametrize("header, expected", [
    ("Host * # note", None),                      # a comment is not a concrete name
    ("Host new # web", "Host new"),
    ('Host web "x web"', 'Host "x web"'),         # one name with a space stays one name
    ("Host web =web", 'Host "=web"'),             # `Host =web` would be `Host web` for ssh
    ("Host new '\"web\"'", 'Host new "\\"web\\""'),   # literal quotes stay literal
    ("Host x\\\tweb", 'Host "x\tweb"'),           # shlex: one token; ssh: `x\` and `web`
    ("Host web\0x", None),                        # ssh stops reading the line at a NUL
    ('Host " web"', None),                        # the loader strips tokens
    ('Host new "" -oProxyCommand=x', "Host new"),     # never valid as an alias
    ("  Host new", "Host new"),
])
def test_merged_header_is_rebuilt_from_the_checked_names(header, expected):
    new, _dropped, _coll = bm._select_new_host_blocks(
        [f"{header}\n    ProxyCommand touch /tmp/x\n"], existing_names={"web"})
    assert (new.splitlines()[0] if new else None) == expected
    if new:                                       # and reads back as the same names (idempotent)
        again, _dropped, _coll = bm._select_new_host_blocks(
            [new], existing_names={"web", *bm._host_patterns(new.splitlines()[0])})
        assert again == ""


# Every spelling OpenSSH accepts for the keyword has to end the stanza before it, or a wildcard
# header rides along as an "option" of a concrete host.
@pytest.mark.parametrize("header", [
    '"Host" *', '"Host"*', 'Ho"st" *', '=Host *', ' = Host *', '""Host *', '"" Host *',
    '"Match" all', '=Match all',
])
def test_disguised_header_in_a_stanza_body_is_still_a_header(header):
    new, dropped, _coll = bm._select_new_host_blocks(
        [f"Host new\n    HostName n.example\n{header}\n    ProxyCommand touch /tmp/x\n"],
        existing_names=set())
    assert new == "Host new\n    HostName n.example"
    assert dropped == 1


def test_split_directive_variants():
    assert bm._split_directive("Host = a b") == ("host", "a b")
    assert bm._split_directive("Host ==a") == ("host", "=a")     # only ONE `=` is a separator
    assert bm._split_directive('"Include" x.conf') == ("include", "x.conf")
    assert bm._split_directive("==Host *") == ("", "")           # ssh ignores these lines
    assert bm._split_directive('"Host *') == ("", "")
    assert bm._split_directive('"#" Host *') == ("", "")

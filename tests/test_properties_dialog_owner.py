"""Owner/group name resolution for the SFTP properties dialog."""

from sshpilot.file_manager.properties_dialog import PropertiesDialog


PASSWD = """root:x:0:0:root:/root:/bin/bash
ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash
"""

GROUP = """root:x:0:
ubuntu:x:1000:
"""


def test_lookup_name_in_passwd():
    assert PropertiesDialog._lookup_name_in_passwd(PASSWD, 1000) == "ubuntu"
    assert PropertiesDialog._lookup_name_in_passwd(PASSWD, 0) == "root"
    assert PropertiesDialog._lookup_name_in_passwd(PASSWD, 999) is None


def test_lookup_name_in_group():
    assert PropertiesDialog._lookup_name_in_group(GROUP, 1000) == "ubuntu"
    assert PropertiesDialog._lookup_name_in_group(GROUP, 0) == "root"
    assert PropertiesDialog._lookup_name_in_group(GROUP, 999) is None


def test_format_remote_owner_uses_remote_passwd_not_local():
    class FakeSFTP:
        files = {
            "/etc/passwd": PASSWD.encode(),
            "/etc/group": GROUP.encode(),
        }

        def open(self, path, mode="r"):
            data = self.files[path]

            class _FH:
                def read(self_inner):
                    return data

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

            return _FH()

    text = PropertiesDialog._format_remote_owner(FakeSFTP(), 1000, 1000)
    assert text == "ubuntu : ubuntu"

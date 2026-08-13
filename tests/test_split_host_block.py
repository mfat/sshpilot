from pathlib import Path

from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _split(tmp_path, config_text: str, key_path: Path, key_select_mode: int):
    config_path = tmp_path / "ssh_config"
    config_path.write_text(config_text, encoding="utf-8")

    store = SshConfigStore(config_path)
    store.split(
        "hostA",
        "hostA",
        {
            "nickname": "hostA",
            "username": "testuser",
            "keyfile": str(key_path),
            "identity_files": [str(key_path)],
            "key_select_mode": key_select_mode,
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    return config_path.read_text()


def _dedicated_block(contents: str) -> str:
    host_blocks = [block for block in contents.strip().split("\n\n") if block.strip()]
    return next(block for block in host_blocks if block.startswith("Host hostA"))


def test_split_host_block_preserves_identityfile_without_identitiesonly(tmp_path):
    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    contents = _split(
        tmp_path,
        "\n".join([
            "Host shared hostA hostB",
            "    User testuser",
            f"    IdentityFile {key_path}",
            "",
        ]),
        key_path,
        key_select_mode=2,
    )

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    dedicated_block = _dedicated_block(contents)
    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" not in dedicated_block


def test_split_host_block_preserves_identitiesonly_directive(tmp_path):
    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    contents = _split(
        tmp_path,
        "\n".join([
            "Host shared hostA hostB",
            "    User testuser",
            f"    IdentityFile {key_path}",
            "    IdentitiesOnly yes",
            "",
        ]),
        key_path,
        key_select_mode=1,
    )

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    dedicated_block = _dedicated_block(contents)
    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" in dedicated_block


def test_split_host_block_respects_identitiesonly_no(tmp_path):
    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    contents = _split(
        tmp_path,
        "\n".join([
            "Host shared hostA hostB",
            "    User testuser",
            f"    IdentityFile {key_path}",
            "    IdentitiesOnly no",
            "",
        ]),
        key_path,
        key_select_mode=2,
    )

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    dedicated_block = _dedicated_block(contents)
    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" not in dedicated_block

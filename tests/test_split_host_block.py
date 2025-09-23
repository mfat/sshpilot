from sshpilot.connection_manager import ConnectionManager


def test_split_host_block_preserves_identityfile_without_identitiesonly(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)

    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    config_path = tmp_path / "ssh_config"
    config_path.write_text(
        "\n".join(
            [
                "Host shared hostA hostB",
                "    User testuser",
                f"    IdentityFile {key_path}",
                "",
            ]
        )
    )

    cm.ssh_config_path = str(config_path)

    parsed = ConnectionManager.parse_host_config(
        cm,
        {
            "host": "hostA",
            "user": "testuser",
            "identityfile": str(key_path),
        },
    )

    assert parsed["key_select_mode"] == 2

    parsed["source"] = str(config_path)

    assert cm._split_host_block("hostA", parsed, str(config_path))

    contents = config_path.read_text()

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    host_blocks = [block for block in contents.strip().split("\n\n") if block.strip()]
    dedicated_block = next(block for block in host_blocks if block.startswith("Host hostA"))

    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" not in dedicated_block


def test_split_host_block_preserves_identitiesonly_directive(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)

    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    config_path = tmp_path / "ssh_config"
    config_path.write_text(
        "\n".join(
            [
                "Host shared hostA hostB",
                "    User testuser",
                f"    IdentityFile {key_path}",
                "    IdentitiesOnly yes",
                "",
            ]
        )
    )

    cm.ssh_config_path = str(config_path)

    parsed = ConnectionManager.parse_host_config(
        cm,
        {
            "host": "hostA",
            "user": "testuser",
            "identityfile": str(key_path),
            "identitiesonly": "yes",
        },
    )

    assert parsed["key_select_mode"] == 1

    parsed["source"] = str(config_path)

    assert cm._split_host_block("hostA", parsed, str(config_path))

    contents = config_path.read_text()

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    host_blocks = [block for block in contents.strip().split("\n\n") if block.strip()]
    dedicated_block = next(block for block in host_blocks if block.startswith("Host hostA"))

    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" in dedicated_block


def test_split_host_block_respects_identitiesonly_no(tmp_path):

    cm = ConnectionManager.__new__(ConnectionManager)

    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    config_path = tmp_path / "ssh_config"
    config_path.write_text(
        "\n".join(
            [
                "Host shared hostA hostB",
                "    User testuser",
                f"    IdentityFile {key_path}",
                "    IdentitiesOnly no",
                "",
            ]
        )
    )

    cm.ssh_config_path = str(config_path)

    parsed = ConnectionManager.parse_host_config(
        cm,
        {
            "host": "hostA",
            "user": "testuser",
            "identityfile": str(key_path),
            "identitiesonly": "no",
        },
    )

    assert parsed["key_select_mode"] == 2

    parsed["source"] = str(config_path)

    assert cm._split_host_block("hostA", parsed, str(config_path))

    contents = config_path.read_text()

    assert "Host shared hostB" in contents
    assert "Host hostA" in contents

    host_blocks = [block for block in contents.strip().split("\n\n") if block.strip()]
    dedicated_block = next(block for block in host_blocks if block.startswith("Host hostA"))

    assert f"IdentityFile {key_path}" in dedicated_block
    assert "IdentitiesOnly yes" not in dedicated_block


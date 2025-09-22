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


def test_truthy_non_string_identitiesonly_parsing_and_format(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)

    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    base_config = {
        "host": "hostA",
        "user": "testuser",
        "identityfile": str(key_path),
    }

    parsed_bool = ConnectionManager.parse_host_config(
        cm,
        {**base_config, "identitiesonly": True},
    )
    assert parsed_bool["key_select_mode"] == 1

    formatted_bool = ConnectionManager.format_ssh_config_entry(cm, parsed_bool)
    assert "IdentitiesOnly yes" in formatted_bool

    parsed_str = ConnectionManager.parse_host_config(
        cm,
        {**base_config, "identitiesonly": "yes"},
    )
    assert parsed_str["key_select_mode"] == 1

    formatted_str = ConnectionManager.format_ssh_config_entry(cm, parsed_str)
    assert formatted_str.count("IdentitiesOnly yes") == 1

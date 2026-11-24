"""What a block is for, and how a list of blocks names it."""

import pytest

from sshpilot.gtk.advanced_config_messages import block_category_label, block_list_title
from sshpilot.ssh_config_blocks import (
    ALL_HOSTS, CONNECTION, GLOBAL, HOST, MATCH, MATCH_RULE, PATTERN, block_category,
)


@pytest.mark.parametrize("kind,patterns,category", [
    (GLOBAL, "", ALL_HOSTS),
    (HOST, "web1", CONNECTION),
    (HOST, "web1 web2", CONNECTION),
    (HOST, "*", ALL_HOSTS),
    (HOST, "* !bastion", PATTERN),          # everything but one: not everything
    (HOST, "*.prod", PATTERN),
    (HOST, "web?", PATTERN),
    (HOST, "!bad", PATTERN),
    (HOST, "edge *.edge", PATTERN),         # reaches more than the host it names
    (HOST, "", PATTERN),                    # a block not yet given a line
    (MATCH, "all", ALL_HOSTS),
    (MATCH, "All", ALL_HOSTS),
    (MATCH, "host *.corp", MATCH_RULE),
    (MATCH, "all exec true", MATCH_RULE),
])
def test_a_block_is_one_of_four_things(kind, patterns, category):
    assert block_category(kind, patterns) == category


def test_a_list_names_a_block_by_what_follows_its_keyword():
    assert block_list_title(GLOBAL, "") == "Global Options"
    assert block_list_title(HOST, " web1 web2 ") == "web1 web2"
    assert block_list_title(HOST, "*") == "Host *"
    assert block_list_title(HOST, "*.prod") == "*.prod"
    assert block_list_title(MATCH, "host *.corp") == "Match host *.corp"
    assert block_list_title(HOST, "") == "New Host Block"


def test_the_caption_says_what_the_block_is_for():
    assert block_category_label(CONNECTION) == "Connection"
    assert block_category_label(CONNECTION, 3) == "3 connections"
    assert block_category_label(PATTERN) == "Host pattern"
    assert block_category_label(MATCH_RULE) == "Match rule"
    assert block_category_label(ALL_HOSTS) == "All hosts"

"""What the sidebar's Rules view lists: everything that is not a connection."""

from sshpilot.sidebar_rules import Rule, matches, read_rules, summary
from sshpilot.ssh_config_blocks import GLOBAL, HOST, MATCH

CONFIG = """\
# shared defaults
ServerAliveInterval 30
Compression yes

Host web1
    HostName 10.0.0.1

Host web2 web3
    User deploy

Host *.prod !bastion.prod
    User ops
    IdentityFile ~/.ssh/prod
    IdentityFile ~/.ssh/prod2

Match host *.corp exec "true"
    ProxyJump gate

# [disabled] Host *.old
# [disabled]     User gone

Host edge *.edge
    Port 2222

Host *
    ForwardAgent no
"""


def test_rules_are_everything_but_connections_in_file_order():
    rules = read_rules(CONFIG)
    assert [(r.kind, r.patterns) for r in rules] == [
        (GLOBAL, ""),
        (HOST, "*.prod !bastion.prod"),
        (MATCH, 'host *.corp exec "true"'),
        (HOST, "edge *.edge"),          # a name beside a pattern is a rule too
        (HOST, "*"),
    ]


def test_a_rule_knows_where_it_is_and_what_it_sets():
    rules = read_rules(CONFIG, "inc-1", "conf.d/work")
    lines = CONFIG.splitlines()
    prod = rules[1]
    assert lines[prod.line - 1] == "Host *.prod !bastion.prod"
    assert prod.keywords == ("User", "IdentityFile")        # each once, as written
    assert (prod.file, prod.file_label) == ("inc-1", "conf.d/work")
    # The global options have no header: their first line stands for them.
    assert lines[rules[0].line - 1] == "ServerAliveInterval 30"


def test_a_file_of_connections_only_has_no_rules():
    assert read_rules("Host a\n    HostName h\n\nHost b c\n    User u\n") == []
    assert read_rules("") == []


def test_the_summary_trails_off_after_a_few_options():
    rule = Rule(HOST, 1, "*", ("A", "B", "C", "D", "E"))
    assert summary(rule) == "A, B, C, D, …"
    assert summary(Rule(HOST, 1, "*", ("User",))) == "User"
    assert summary(Rule(HOST, 1, "*", ())) == ""


def test_search_looks_at_the_line_the_options_and_the_file():
    rule = Rule(HOST, 1, "*.prod", ("User", "IdentityFile"), "inc", "conf.d/work")
    assert matches(rule, "")
    assert matches(rule, "PROD")
    assert matches(rule, "identity prod")
    assert matches(rule, "work")
    assert not matches(rule, "staging")

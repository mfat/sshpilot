"""The connection editor's sidebar search: what a query finds, and in what order."""

from sshpilot.connection_dialog_search import fold, rank_matches

PAGES = [
    ("connection", "Connection"),
    ("authentication", "Authentication"),
    ("forwarding", "Port Forwarding"),
    ("advanced", "Extra Options"),
]
ROWS = [
    ("connection", "Host", "Hostname / IP address", ""),
    ("connection", "Host", "Port", ""),
    ("connection", "Routing", "Forward agent", "Allow the remote host to use your local ssh-agent"),
    ("authentication", "Password", "Password (optional)", ""),
    ("forwarding", "X11 Forwarding", "X11 Forwarding", "Forward X11 connections for GUI applications"),
    ("forwarding", "Session", "Do not start a remote shell (ssh -N flag)", ""),
]


def _found(query):
    return [
        (page, None if index is None else ROWS[index][2])
        for page, index in rank_matches(query, PAGES, ROWS)
    ]


def test_an_empty_query_finds_nothing():
    assert rank_matches("   ", PAGES, ROWS) == []


def test_a_page_matched_by_name_comes_before_its_fields():
    assert _found("port")[:2] == [("forwarding", None), ("connection", "Port")]


def test_a_title_starting_with_the_query_beats_one_that_only_holds_it():
    found = _found("forward")
    assert found.index(("connection", "Forward agent")) < found.index(
        ("forwarding", "X11 Forwarding")
    )


def test_a_subtitle_match_is_found_and_ranked_last():
    assert _found("ssh-agent") == [("connection", "Forward agent")]
    found = _found("remote")
    assert found[-1] == ("connection", "Forward agent")


def test_every_word_must_match_and_may_match_group_or_page():
    assert _found("routing agent") == [("connection", "Forward agent")]
    assert _found("authentication password") == [("authentication", "Password (optional)")]
    assert _found("password port") == []


def test_matching_ignores_case_and_accents():
    assert fold("Señal ÉTÉ") == "senal ete"
    assert _found("HOSTNAME") == [("connection", "Hostname / IP address")]


def test_a_row_on_a_page_that_is_not_shown_is_left_out():
    assert rank_matches("port", [("connection", "Connection")], ROWS) == [("connection", 1)]


def test_the_extra_options_page_is_found_by_its_name():
    assert _found("extra") == [("advanced", None)]

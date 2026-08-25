"""Hypothesis strategies for sshPilot sidecar stress tests.

Aliases/hostnames/usernames are restricted to characters that are both valid
SSH config tokens and safely round-trip through the loader's parser (no
whitespace, ``#``, quoting, or glob metacharacters) -- wildcard/negated/
percent-token/``$``-expression inputs are adversarial cases the spec calls
for, and they are generated deliberately by dedicated strategies below rather
than accidentally by the "plain" ones.
"""

from __future__ import annotations

import string

from hypothesis import strategies as st

_ALIAS_ALPHABET = string.ascii_lowercase + string.digits + "-_"
_HOST_ALPHABET = string.ascii_lowercase + string.digits + "-."


def alias_suffixes() -> st.SearchStrategy[str]:
    """A short token safe to append to a generated ``host-<n>-<suffix>`` alias."""

    return st.text(alphabet=_ALIAS_ALPHABET, min_size=1, max_size=8).map(str.strip).filter(bool)


def hostnames() -> st.SearchStrategy[str]:
    body = st.text(alphabet=_HOST_ALPHABET, min_size=1, max_size=12).filter(
        lambda value: value.strip(".-") == value and value
    )
    return st.builds(lambda a, b: f"{a}.{b}.example", body, body)


def ports() -> st.SearchStrategy[int]:
    return st.integers(min_value=1, max_value=65535)


def usernames() -> st.SearchStrategy[str]:
    return st.one_of(
        st.just(""),
        st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=10),
    )


def identity_file_component() -> st.SearchStrategy[str]:
    name = st.text(alphabet=string.ascii_lowercase + string.digits + "_-", min_size=1, max_size=10)
    return st.builds(lambda n: f"~/.ssh/{n}", name)


def identity_file_lists() -> st.SearchStrategy[tuple]:
    return st.lists(identity_file_component(), max_size=3, unique=True).map(tuple)


def group_names() -> st.SearchStrategy[str]:
    return st.text(
        alphabet=string.ascii_letters + string.digits + " _-",
        min_size=1,
        max_size=16,
    ).map(str.strip).filter(bool)


def tag_names() -> st.SearchStrategy[str]:
    return st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=12).filter(bool)

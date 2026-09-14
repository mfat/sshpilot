"""Public keys from online identities: the ssh-import-id fetch step in core."""

from __future__ import annotations

import json
import urllib.error

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.identity import (
    PUBLIC_KEY_FETCH_FAILED,
    PUBLIC_KEY_SOURCE_EMPTY,
    PUBLIC_KEY_SOURCE_INVALID,
    PUBLIC_KEY_SOURCE_NOT_FOUND,
    PUBLIC_KEY_SOURCE_RATE_LIMITED,
)
from sshpilot.core.keys import public_key_import as mod
from sshpilot.core.keys.public_key_import import (
    PublicKeyHttpError,
    fetch_public_keys,
    parse_public_key_line,
    parse_public_key_source,
)

# Vectors shared with ssh-import-id's test_key_fingerprint.py (ssh-keygen -lf).
ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIMkGoTfVoNpsJrNxzq9WpRhlCp0qsPwsOHopWxNbIM8Z"
ED25519_FP = "SHA256:XCEFfvF2V6u0ESVb/GLp/HAHVCZMoi36uskzHxTBUk0"
ED25519_B = "AAAAC3NzaC1lZDI1NTE5AAAAIBZD0BJ4cbO7gdR0ncScv++/uuVhNyVZIchrfaM4qcVs"
ED25519_B_FP = "SHA256:KNENJxaBWf6prhHPSPcMharb4yJpo2MDW6zqiOt9xW4"
ECDSA256 = (
    "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBIuWFtqIoZ7OzXwFcNS3AR"
    "Y8qlE+QCC37MuzWLn7S7QNCsjeSFQj0fGtnfwwHXc+jOEzNPu49HCWZZ8XWUMrXGY="
)
ECDSA256_FP = "SHA256:dPhT6je9SI3Ix6ouENaT5KC65AR7JrayViEz4sKqE1Q"
SK_ED25519 = (
    "AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIDUoNG0ZFnEUpkmTfkUOrXqidO1bi6PJ"
    "AmHw9qnul9NuAAAABHNzaDo="
)
SK_ED25519_FP = "SHA256:ODe/XhzkNdD94qkx0Dvjv4BGwQ066GU9o6Qs8olG7Tw"


class _Fetch:
    def __init__(self, body=b"", error=None):
        self.body = body
        self.error = error
        self.calls = []

    def __call__(self, url, headers, timeout, *, follow_redirects):
        self.calls.append((url, dict(headers), timeout, follow_redirects))
        if self.error is not None:
            raise self.error
        return self.body


def _detail(exc_info):
    return exc_info.value.details.get("code")


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "provider", "url", "label"),
    (
        ("gh:mfat", "gh", "https://api.github.com/users/mfat/keys", "gh:mfat"),
        ("gl:someone", "gl", "https://gitlab.com/someone.keys", "gl:someone"),
        ("lp:kirkland", "lp", "https://launchpad.net/~kirkland/+sshkeys", "lp:kirkland"),
        # ssh-import-id's default protocol is Launchpad.
        ("kirkland", "lp", "https://launchpad.net/~kirkland/+sshkeys", "lp:kirkland"),
        ("  gh:mfat  ", "gh", "https://api.github.com/users/mfat/keys", "gh:mfat"),
        (
            "https://github.com/mfat.keys",
            "url",
            "https://github.com/mfat.keys",
            "https://github.com/mfat.keys",
        ),
        (
            "HTTPS://example.com/keys?user=a",
            "url",
            "HTTPS://example.com/keys?user=a",
            "HTTPS://example.com/keys?user=a",
        ),
    ),
)
def test_sources_resolve_like_ssh_import_id(text, provider, url, label):
    source = parse_public_key_source(text)
    assert (source.provider, source.url, source.label) == (provider, url, label)


def test_username_is_quoted_into_the_url():
    source = parse_public_key_source("gh:a/../b?x")
    assert source.url == "https://api.github.com/users/a%2F..%2Fb%3Fx/keys"


@pytest.mark.parametrize(
    "text",
    (
        "",
        "   ",
        "http://github.com/mfat.keys",
        "ftp://example.com/keys",
        "https://user:secret@example.com/keys",
        "https:///nohost",
        "xx:user",
        "gh:",
        "gh:a:b",
        "gh:two words",
        "gh:line\nbreak",
        "gh:" + "a" * 3000,
    ),
)
def test_invalid_sources_fail_validation(text):
    with pytest.raises(SshPilotError) as exc_info:
        parse_public_key_source(text)
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
    assert _detail(exc_info) == PUBLIC_KEY_SOURCE_INVALID


# ---------------------------------------------------------------------------
# Line validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "key_type", "fingerprint"),
    (
        (f"ssh-ed25519 {ED25519} smoser@frink", "ssh-ed25519", ED25519_FP),
        (f"ecdsa-sha2-nistp256 {ECDSA256} user@localhost", "ecdsa-sha2-nistp256", ECDSA256_FP),
        (f"sk-ssh-ed25519@openssh.com {SK_ED25519}", "sk-ssh-ed25519@openssh.com", SK_ED25519_FP),
    ),
)
def test_fingerprints_match_ssh_keygen(line, key_type, fingerprint):
    parsed = parse_public_key_line(line)
    assert parsed is not None
    assert parsed[0] == key_type
    assert parsed[3] == fingerprint


@pytest.mark.parametrize(
    "line",
    (
        "",
        "# comment",
        f'command="/bin/sh" ssh-ed25519 {ED25519} evil',
        f"cert-authority ssh-ed25519 {ED25519}",
        f"no-pty,from=\"*\" ssh-ed25519 {ED25519}",
        f"ssh-ed25519-cert-v01@openssh.com {ED25519}",
        # The blob says ed25519 but the line claims rsa.
        f"ssh-rsa {ED25519}",
        "ssh-ed25519 not*base64",
        "ssh-ed25519 AAAA",
        "ssh-ed25519",
        f"ssh-ed25519 {ED25519} bell\x07",
    ),
)
def test_non_bare_or_malformed_lines_are_rejected(line):
    assert parse_public_key_line(line) is None


# ---------------------------------------------------------------------------
# Fetch and labelling
# ---------------------------------------------------------------------------
def test_github_api_keys_get_ssh_import_id_comments():
    body = json.dumps(
        [
            {"id": 11, "key": f"ssh-ed25519 {ED25519}"},
            {"id": 12, "key": f"ssh-ed25519 {ED25519_B}"},
            {"id": "bad", "key": f"ssh-ed25519 {ED25519}"},
        ]
    ).encode()
    fetch = _Fetch(body)

    result = fetch_public_keys("gh:mfat", fetch=fetch)

    url, headers, timeout, follow_redirects = fetch.calls[0]
    assert url == "https://api.github.com/users/mfat/keys"
    assert headers["User-Agent"].startswith("sshPilot/")
    assert timeout == mod.DEFAULT_TIMEOUT
    assert follow_redirects is False
    assert result.source == "gh:mfat"
    assert [key.line for key in result.keys] == [
        f"ssh-ed25519 {ED25519} mfat@github/11 # ssh-import-id gh:mfat",
        f"ssh-ed25519 {ED25519_B} mfat@github/12 # ssh-import-id gh:mfat",
    ]
    assert [key.fingerprint for key in result.keys] == [ED25519_FP, ED25519_B_FP]


def test_gitlab_keys_get_gitlab_comment():
    fetch = _Fetch(f"ssh-ed25519 {ED25519} laptop\n\n# note\n".encode())

    result = fetch_public_keys("gl:dev", fetch=fetch)

    assert fetch.calls[0][0] == "https://gitlab.com/dev.keys"
    assert result.keys[0].comment == "laptop dev@gitlab # ssh-import-id gl:dev"


def test_url_source_filters_and_dedupes_lines():
    body = "\n".join(
        (
            f"ssh-ed25519 {ED25519}",
            f'command="/bin/sh" ssh-ed25519 {ED25519_B} injected',
            f"cert-authority ecdsa-sha2-nistp256 {ECDSA256}",
            f"ssh-ed25519 {ED25519} duplicate",
            "garbage",
            f"ecdsa-sha2-nistp256 {ECDSA256} work",
        )
    ).encode()

    fetch = _Fetch(body)
    result = fetch_public_keys("https://github.com/mfat.keys", fetch=fetch)

    assert fetch.calls[0][3] is True
    assert [key.line for key in result.keys] == [
        f"ssh-ed25519 {ED25519} # ssh-import-id https://github.com/mfat.keys",
        f"ecdsa-sha2-nistp256 {ECDSA256} work # ssh-import-id https://github.com/mfat.keys",
    ]


def test_source_without_usable_keys_is_not_found():
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("lp:nobody", fetch=_Fetch(b"<html>login</html>"))
    assert exc_info.value.code is ErrorCode.KEY_NOT_FOUND
    assert _detail(exc_info) == PUBLIC_KEY_SOURCE_EMPTY


def test_http_404_is_not_found():
    fetch = _Fetch(error=PublicKeyHttpError(404))
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("gh:ghost", fetch=fetch)
    assert exc_info.value.code is ErrorCode.KEY_NOT_FOUND
    assert _detail(exc_info) == PUBLIC_KEY_SOURCE_NOT_FOUND


def test_gitlab_sign_in_redirect_is_not_found():
    fetch = _Fetch(error=PublicKeyHttpError(302, {"Location": "/users/sign_in"}))
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("gl:ghost", fetch=fetch)
    assert exc_info.value.code is ErrorCode.KEY_NOT_FOUND
    assert _detail(exc_info) == PUBLIC_KEY_SOURCE_NOT_FOUND


def test_github_rate_limit_is_reported_and_retryable():
    fetch = _Fetch(error=PublicKeyHttpError(403, {"X-RateLimit-Remaining": "0"}))
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("gh:mfat", fetch=fetch)
    assert exc_info.value.code is ErrorCode.KEY_PUBLIC_UNAVAILABLE
    assert _detail(exc_info) == PUBLIC_KEY_SOURCE_RATE_LIMITED
    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    "error",
    (
        PublicKeyHttpError(500),
        urllib.error.URLError("no route"),
        TimeoutError("timed out"),
    ),
)
def test_transport_and_server_failures_are_retryable(error):
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("https://example.com/keys", fetch=_Fetch(error=error))
    assert exc_info.value.code is ErrorCode.KEY_PUBLIC_UNAVAILABLE
    assert _detail(exc_info) == PUBLIC_KEY_FETCH_FAILED
    assert exc_info.value.retryable is True


def test_unexpected_github_payload_fails_cleanly():
    with pytest.raises(SshPilotError) as exc_info:
        fetch_public_keys("gh:mfat", fetch=_Fetch(b'{"message": "nope"}'))
    assert _detail(exc_info) == PUBLIC_KEY_FETCH_FAILED


def test_redirects_to_non_https_are_refused():
    handler = mod._RedirectPolicy(True)
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://evil.example/keys"
        )


def test_unfollowed_redirect_surfaces_its_status():
    handler = mod._RedirectPolicy(False)
    with pytest.raises(PublicKeyHttpError) as exc_info:
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://gitlab.com/users/sign_in"
        )
    assert exc_info.value.status == 302

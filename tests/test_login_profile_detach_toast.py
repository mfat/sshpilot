"""Login-profile detach notices use runtime catalogue plural rules."""

import gettext
import io
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from sshpilot import window_dialogs
from sshpilot.api.models.login_profiles import (
    DetachedConnectionInfo,
    LoginProfileDetachReason,
    LoginProfilesChangedEvent,
)
from sshpilot.api.transport.login_profile_codec import (
    login_profiles_changed_event_from_wire,
    login_profiles_changed_event_to_wire,
)
from sshpilot.gtk import login_profile_messages as messages


SINGULAR = (
    "{count} connection was detached from its login profile because "
    "its SSH config was edited."
)
PLURAL = (
    "{count} connections were detached from their login profile because "
    "their SSH config was edited."
)
NAMED = (
    "{connection} was detached from login profile “{profile}” because "
    "its SSH config was edited."
)


@pytest.mark.parametrize("count", [1, 2, 5, 21])
def test_detach_count_translates_before_formatting(monkeypatch, count):
    calls = []

    def translate(singular, plural, n):
        calls.append((singular, plural, n))
        return "translated:{count}:one" if n == 1 else "translated:{count}:many"

    monkeypatch.setattr(messages, "ngettext", translate)

    text = messages.format_detached_connections(count)

    assert calls == [(SINGULAR, PLURAL, count)]
    assert text == f"translated:{count}:{'one' if count == 1 else 'many'}"


@pytest.mark.skipif(shutil.which("msgfmt") is None, reason="GNU gettext unavailable")
@pytest.mark.parametrize(
    "language, rule, forms, count, expected",
    [
        ("fr", "nplurals=2; plural=(n > 1);", (
            "{count} connexion détachée.", "{count} connexions détachées."
        ), count, f"{count} {'connexion détachée.' if count < 2 else 'connexions détachées.'}")
        for count in (0, 1, 2, 5, 21)
    ] + [
        ("ru", "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : "
         "n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
         ("one:{count}", "few:{count}", "many:{count}"), count, expected)
        for count, expected in ((2, "few:2"), (5, "many:5"), (21, "one:21"))
    ],
)
def test_detach_count_uses_runtime_catalogue(
    monkeypatch, language, rule, forms, count, expected
):
    # Compile in memory: no installed or committed catalogue is written.
    header = (
        "Content-Type: text/plain; charset=UTF-8\n"
        f"Language: {language}\nPlural-Forms: {rule}\n"
    )
    po = "\n".join([
        'msgid ""', f"msgstr {json.dumps(header)}", "",
        "#, python-brace-format",
        f"msgid {json.dumps(SINGULAR)}",
        f"msgid_plural {json.dumps(PLURAL)}",
        *(f"msgstr[{index}] {json.dumps(form, ensure_ascii=False)}"
          for index, form in enumerate(forms)),
        "",
    ])
    compiled = subprocess.run(
        ["msgfmt", "--check-format", "-o", "-", "-"],
        input=po.encode(), capture_output=True, check=True,
    )
    catalogue = gettext.GNUTranslations(io.BytesIO(compiled.stdout))
    monkeypatch.setattr(messages, "ngettext", catalogue.ngettext)

    assert messages.format_detached_connections(count) == expected


@pytest.fixture
def toast_window(monkeypatch):
    toasts = []

    def new_toast(message):
        toast = SimpleNamespace(message=message, timeout=None)
        toast.set_timeout = lambda seconds: setattr(toast, "timeout", seconds)
        return toast

    monkeypatch.setattr(window_dialogs.Adw, "Toast", SimpleNamespace(new=new_toast))
    window = SimpleNamespace(
        toast_overlay=SimpleNamespace(add_toast=toasts.append),
    )
    return window, toasts


def _round_trip_event(count, *, other_reason=None):
    detached = tuple(
        DetachedConnectionInfo(f"web{index}", "Deploy", LoginProfileDetachReason.DRIFT)
        for index in range(count)
    )
    if other_reason is not None:
        detached += (DetachedConnectionInfo("other", "Deploy", other_reason),)
    event = LoginProfilesChangedEvent(detached=detached)
    return login_profiles_changed_event_from_wire(login_profiles_changed_event_to_wire(event))


@pytest.mark.parametrize("count", [2, 5, 21])
def test_grouped_api_event_counts_only_drifted_connections(monkeypatch, toast_window, count):
    calls = []

    def translate(singular, plural, n):
        calls.append((singular, plural, n))
        # A catalogue can select a singular form at 21 even in a grouped event.
        return "catalogue:{count}:one" if n == 21 else "catalogue:{count}:many"

    monkeypatch.setattr(messages, "ngettext", translate)
    window, toasts = toast_window
    payload = _round_trip_event(count, other_reason=LoginProfileDetachReason.PROFILE_DELETED)

    window_dialogs.WindowConfigDialogsMixin.notify_login_profiles_changed(window, payload)

    assert calls == [(SINGULAR, PLURAL, count)]
    assert len(toasts) == 1
    assert toasts[0].message == f"catalogue:{count}:{'one' if count == 21 else 'many'}"
    assert toasts[0].timeout == 6


def test_unit_event_preserves_named_notice(monkeypatch, toast_window):
    calls = []
    monkeypatch.setattr(window_dialogs, "_", lambda text: calls.append(text) or text)
    window, toasts = toast_window

    window_dialogs.WindowConfigDialogsMixin.notify_login_profiles_changed(
        window, _round_trip_event(1)
    )

    assert calls == [NAMED]
    assert len(toasts) == 1
    assert toasts[0].message == (
        "web0 was detached from login profile “Deploy” because its SSH config was edited."
    )
    assert toasts[0].timeout == 6


def test_event_without_drift_does_not_show_a_detach_notice(toast_window):
    window, toasts = toast_window

    window_dialogs.WindowConfigDialogsMixin.notify_login_profiles_changed(
        window, _round_trip_event(0, other_reason=LoginProfileDetachReason.PROFILE_MISSING)
    )

    assert toasts == []

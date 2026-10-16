"""The interface-language preference (Preferences ▸ Interface ▸ Language).

The whole feature rests on two things being true: the picker lists exactly the
catalogues that are installed, and the saved code reaches gettext through
LANGUAGE *before* the first lookup. Both are checked here.
"""

import json
import os

import pytest

from sshpilot import i18n


@pytest.fixture(autouse=True)
def _isolate_language_env():
    """Restore the language variables around every test.

    ``apply_language`` writes ``os.environ`` directly -- both ``LANGUAGE`` and
    the variable it parks the original in -- so without this a test that sets a
    language leaks it into the ones that follow.
    """
    saved = {k: os.environ.get(k)
             for k in ("LANGUAGE", i18n._ORIGINAL_LANGUAGE_VAR)}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _install_catalogue(root, code):
    d = root / code / "LC_MESSAGES"
    d.mkdir(parents=True)
    (d / "sshpilot.mo").write_bytes(b"")
    return d


def test_english_is_always_offered(tmp_path):
    """The picker is always shown, so it must never come back empty."""
    assert i18n.available_languages(str(tmp_path)) == [("en", "English")]
    assert i18n.available_languages("/nonexistent/localedir") == [("en", "English")]


def test_catalogues_bundled_in_the_package_are_found(monkeypatch, tmp_path):
    """The checkout/pip case: no Meson LOCALEDIR, catalogue inside the package.

    This is what made the setting invisible when it was first written.
    """
    monkeypatch.setattr(i18n, "_BUILD_LOCALEDIR", None)
    monkeypatch.setattr(i18n, "_PACKAGE_LOCALEDIR", str(tmp_path))
    _install_catalogue(tmp_path, "de")

    assert i18n.get_localedir() == str(tmp_path)
    assert ("de", "Deutsch") in i18n.available_languages()


def test_empty_meson_localedir_falls_through_to_the_bundled_copy(monkeypatch, tmp_path):
    """Meson installed the directory but no catalogue into it.

    Binding there would leave the UI English while the picker still listed the
    language, so an existing-but-empty tree must not win.
    """
    empty, bundled = tmp_path / "installed", tmp_path / "bundled"
    empty.mkdir()
    _install_catalogue(bundled, "de")
    monkeypatch.setattr(i18n, "_BUILD_LOCALEDIR", str(empty))
    monkeypatch.setattr(i18n, "_PACKAGE_LOCALEDIR", str(bundled))

    assert i18n.get_localedir() == str(bundled)
    assert ("de", "Deutsch") in i18n.available_languages()


def test_picker_is_scoped_to_the_directory_that_gets_bound(monkeypatch, tmp_path):
    """Never offer a language from a tree the text domain will not be bound to."""
    installed, bundled = tmp_path / "installed", tmp_path / "bundled"
    _install_catalogue(installed, "fr")
    _install_catalogue(bundled, "de")
    monkeypatch.setattr(i18n, "_BUILD_LOCALEDIR", str(installed))
    monkeypatch.setattr(i18n, "_PACKAGE_LOCALEDIR", str(bundled))

    codes = [c for c, _name in i18n.available_languages()]

    assert codes == ["en", "fr"]  # de lives only in the tree that loses


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"LANGUAGE": "de"}, ["de"]),
        ({"LANGUAGE": "pt_BR:pt"}, ["pt_BR", "pt"]),
        ({"LANGUAGE": "", "LC_ALL": "de_DE.UTF-8"}, ["de_DE", "de"]),
        ({"LANGUAGE": "", "LC_ALL": "C"}, []),
        ({"LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}, []),
    ],
)
def test_ui_language_codes(monkeypatch, env, expected):
    """Drives the tips.<lang>.md lookup, which is translated as data."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    assert i18n.ui_language_codes() == expected


def test_meson_localedir_wins_over_the_bundled_copy(monkeypatch, tmp_path):
    installed, bundled = tmp_path / "installed", tmp_path / "bundled"
    _install_catalogue(installed, "de")
    _install_catalogue(bundled, "de")
    monkeypatch.setattr(i18n, "_BUILD_LOCALEDIR", str(installed))
    monkeypatch.setattr(i18n, "_PACKAGE_LOCALEDIR", str(bundled))

    assert i18n.get_localedir() == str(installed)


def test_lists_installed_catalogues_with_endonyms(tmp_path):
    _install_catalogue(tmp_path, "de")
    _install_catalogue(tmp_path, "fr")

    langs = i18n.available_languages(str(tmp_path))

    assert ("de", "Deutsch") in langs
    assert ("fr", "Français") in langs
    # English is the source language and ships no .mo, but a user on a German
    # system still needs a way back to it.
    assert ("en", "English") in langs


def test_directory_without_a_catalogue_is_ignored(tmp_path):
    (tmp_path / "de" / "LC_MESSAGES").mkdir(parents=True)  # no .mo inside
    _install_catalogue(tmp_path, "es")

    codes = [c for c, _name in i18n.available_languages(str(tmp_path))]

    assert "de" not in codes
    assert "es" in codes


def test_unknown_code_falls_back_to_the_code_itself(tmp_path):
    _install_catalogue(tmp_path, "qq")
    assert ("qq", "qq") in i18n.available_languages(str(tmp_path))


def test_apply_language_exports_language(monkeypatch):
    # setenv, not delenv: monkeypatch only restores keys it has recorded, and
    # apply_language writes os.environ directly — a delenv on an unset variable
    # records nothing, so "de" would leak into the rest of the session and
    # translate the strings other tests assert on.
    monkeypatch.setenv("LANGUAGE", "")

    assert i18n.apply_language("de") == "de"
    assert os.environ["LANGUAGE"] == "de"


def test_system_default_leaves_the_environment_alone(monkeypatch):
    """Empty setting must not pin a language — that is the whole point of it."""
    monkeypatch.setenv("LANGUAGE", "fr")
    os.environ.pop(i18n._ORIGINAL_LANGUAGE_VAR, None)

    assert i18n.apply_language("") == ""

    assert os.environ["LANGUAGE"] == "fr"


def test_system_default_undoes_an_override_inherited_from_a_restart(monkeypatch):
    """"Restart Now" re-execs with os.execv, which keeps the environment.

    So the next process starts with the *previous* choice still exported. An
    empty setting has to clear it, or going back to "System Default" does
    nothing and the old language survives every restart.
    """
    monkeypatch.setenv("LANGUAGE", "")
    os.environ.pop("LANGUAGE", None)
    os.environ.pop(i18n._ORIGINAL_LANGUAGE_VAR, None)

    i18n.apply_language("fa")                      # the instance being replaced
    assert os.environ["LANGUAGE"] == "fa"

    assert i18n.apply_language("") == ""           # the instance replacing it

    assert "LANGUAGE" not in os.environ
    assert i18n._ORIGINAL_LANGUAGE_VAR not in os.environ


def test_system_default_restores_the_sessions_own_language(monkeypatch):
    """Undoing our override must not swallow what the session set itself."""
    monkeypatch.setenv("LANGUAGE", "de:en")
    os.environ.pop(i18n._ORIGINAL_LANGUAGE_VAR, None)

    i18n.apply_language("fa")
    assert os.environ["LANGUAGE"] == "fa"

    i18n.apply_language("")

    assert os.environ["LANGUAGE"] == "de:en"


def test_only_the_first_override_records_the_original(monkeypatch):
    """Switching between languages must not overwrite the stashed original."""
    monkeypatch.setenv("LANGUAGE", "de:en")
    os.environ.pop(i18n._ORIGINAL_LANGUAGE_VAR, None)

    i18n.apply_language("fa")
    i18n.apply_language("ru")
    assert os.environ[i18n._ORIGINAL_LANGUAGE_VAR] == "de:en"

    i18n.apply_language("")

    assert os.environ["LANGUAGE"] == "de:en"


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"ui": {"language": "de"}}, "de"),
        ({"ui": {"language": ""}}, ""),
        ({"ui": {}}, ""),
        ({}, ""),
        ({"ui": {"language": None}}, ""),
    ],
)
def test_configured_language_reads_the_config_file(tmp_path, monkeypatch, config, expected):
    monkeypatch.setattr(i18n, "get_config_dir", lambda: str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    assert i18n.configured_language() == expected


def test_missing_or_broken_config_is_not_fatal(tmp_path, monkeypatch):
    """Startup must never fail over this; the app repairs the config later."""
    monkeypatch.setattr(i18n, "get_config_dir", lambda: str(tmp_path))
    assert i18n.configured_language() == ""

    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    assert i18n.configured_language() == ""


class _Catalogue:
    def gettext(self, message):
        return "bound:" + message

    def ngettext(self, singular, plural, n):
        return "bound:" + (singular if n == 1 else plural)


def test_lookups_go_through_the_bound_catalogue(monkeypatch):
    monkeypatch.setattr(i18n, "_translation", _Catalogue())

    assert i18n.gettext("Connect") == "bound:Connect"
    assert i18n.ngettext("%d host", "%d hosts", 2) == "bound:%d hosts"


def test_lookups_before_binding_are_not_kept(monkeypatch):
    """An early lookup must not stay stuck once the catalogue is bound."""
    monkeypatch.setattr(i18n, "_translation", None)
    monkeypatch.setattr(i18n._stdlib_gettext, "gettext", lambda m: "early:" + m)
    assert i18n.gettext("Connect") == "early:Connect"

    monkeypatch.setattr(i18n, "_translation", _Catalogue())
    assert i18n.gettext("Connect") == "bound:Connect"


def test_bind_translation_falls_back_without_a_catalogue(monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "_translation", None)

    i18n.bind_translation(str(tmp_path))

    assert i18n.gettext("Connect") == "Connect"
    assert i18n.ngettext("%d host", "%d hosts", 1) == "%d host"

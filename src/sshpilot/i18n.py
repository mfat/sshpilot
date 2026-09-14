"""Interface-language preference.

gettext picks its catalogue from the ``LANGUAGE`` environment variable the
first time a string is looked up, and caches it for the life of the process.
So the preference is applied by exporting ``LANGUAGE`` before anything calls
``_()`` -- see :func:`sshpilot.main._init_gettext`. Nothing here retranslates
widgets that already exist, which is why the preference asks for a restart
rather than pretending to switch live.

An empty setting means "system default": leave the environment alone and let
gettext read the user's locale, which is the behaviour every other GTK app has.
"""

import json
import logging
import os
import sys
from typing import List, Optional, Tuple

from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)

# Written by Meson at install time; absent in a setuptools/editable checkout,
# where no catalogue is installed either.
try:
    from .build_config import LOCALEDIR as _BUILD_LOCALEDIR
except Exception:  # pragma: no cover - only present in a Meson install
    _BUILD_LOCALEDIR = None

# Endonyms: a language picker is only useful in the language it names, so each
# entry reads the way a speaker of that language expects to find it.
LANGUAGE_NAMES = {
    'de': 'Deutsch',
    'en': 'English',
    'es': 'Español',
    'fa': 'فارسی',
    'fr': 'Français',
    'it': 'Italiano',
    'nl': 'Nederlands',
    'pl': 'Polski',
    'pt': 'Português',
    'pt_BR': 'Português (Brasil)',
    'ru': 'Русский',
    'tr': 'Türkçe',
    'zh_CN': '简体中文',
}


# Catalogues bundled with the Python package, for runs that never went through
# Meson: a source checkout (run.py) and the pip/setuptools install. Built from
# po/*.po by scripts/build_gresource.sh and committed, like the .ui files and
# the gresource bundle.
_PACKAGE_LOCALEDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locale')


def candidate_localedirs() -> List[str]:
    """Every place a sshpilot catalogue may live, best first.

    A Meson install has exactly one and it is authoritative. Everything else --
    running from the checkout, a pip install, an app bundle -- has to fall back
    to the copy inside the package, or the picker would be empty and the
    setting would silently do nothing.
    """
    dirs = [_BUILD_LOCALEDIR, _PACKAGE_LOCALEDIR, os.path.join(sys.prefix, 'share', 'locale')]
    return [d for d in dirs if d and os.path.isdir(d)]


def N_(message: str) -> str:
    """Mark a string for extraction without translating it here.

    For strings that must stay English in the code -- because something derives
    an identifier from them -- while still being translated where they are
    displayed. xgettext collects them via ``--keyword=N_``; the call site does
    the actual ``_()``.
    """
    return message


def get_localedir() -> Optional[str]:
    """The directory to bind the text domain to, or None if none has a catalogue."""
    for d in candidate_localedirs():
        if _catalogue_codes(d):
            return d
    return None


def _catalogue_codes(localedir: str) -> List[str]:
    """Language codes with a compiled sshpilot catalogue under ``localedir``."""
    codes = []
    try:
        for code in sorted(os.listdir(localedir)):
            if os.path.isfile(os.path.join(localedir, code, 'LC_MESSAGES', 'sshpilot.mo')):
                codes.append(code)
    except OSError as exc:
        logger.debug("Could not scan %s for translations: %s", localedir, exc)
    return codes


def available_languages(localedir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``(code, display name)`` for every catalogue that can actually be loaded.

    English is always included: it is the source language and ships no .mo of
    its own, but a user who has switched to German needs a way back.
    """
    # Exactly the directory the text domain will be bound to, never a union of
    # the candidates: a language found only under a lower-priority tree would be
    # offered in the picker and then silently fail to load after the restart.
    d = localedir if localedir is not None else get_localedir()

    codes = _catalogue_codes(d) if d else []
    if 'en' not in codes:
        codes.insert(0, 'en')
    return [(c, LANGUAGE_NAMES.get(c, c)) for c in sorted(codes)]


def configured_language() -> str:
    """The saved ``ui.language``, read straight from the config file.

    Deliberately not via :class:`~sshpilot.config.Config`: this runs during
    module import of ``main``, before the application object exists, and
    instantiating the config there would invert the startup order for the sake
    of one string.
    """
    try:
        with open(os.path.join(get_config_dir(), 'config.json'), encoding='utf-8') as f:
            return str(json.load(f).get('ui', {}).get('language', '') or '')
    except FileNotFoundError:
        return ''
    except Exception as exc:  # malformed config — the app repairs it later
        logger.debug("Could not read the language preference: %s", exc)
        return ''


def ui_language_codes() -> List[str]:
    """Language codes for the running UI, most specific first (e.g. pt_BR, pt).

    For resources translated as data rather than through gettext -- the tips
    file -- which still need to follow whatever language the UI ended up in.
    """
    for var in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = os.environ.get(var)
        if not value:
            continue
        # LANGUAGE is a colon-separated preference list; the others are a single
        # locale like de_DE.UTF-8. Either way the first entry is what applies.
        tag = value.split(':')[0].split('.')[0].split('@')[0]
        if not tag or tag in ('C', 'POSIX'):
            return []
        codes = [tag]
        if '_' in tag:
            codes.append(tag.split('_')[0])
        return codes
    return []


# Scripts written right to left. GTK works out its default text direction by
# translating a marker string in its *own* catalogue, which follows the C
# library's LC_MESSAGES -- not ``LANGUAGE``. So picking a language in
# Preferences translates every string while leaving the interface laid out left
# to right: Persian text, but the search placeholder, entry alignment and
# margins all still mirrored the wrong way. The direction has to be set
# explicitly for the in-app preference to mean anything.
RTL_LANGUAGES = frozenset({
    'ar', 'ckb', 'dv', 'fa', 'he', 'iw', 'ji', 'ps', 'sd', 'ug', 'ur', 'yi',
})


def is_rtl_language(code: Optional[str]) -> bool:
    """Whether *code* names a right-to-left language.

    Accepts anything the environment may hold -- ``fa``, ``pt_BR``,
    ``ar_EG.UTF-8``, ``sr-Latn`` -- and looks only at the language subtag.
    """
    tag = str(code or '').split(':')[0].split('.')[0].split('@')[0]
    tag = tag.replace('-', '_').split('_')[0].lower()
    return bool(tag) and tag in RTL_LANGUAGES


def apply_text_direction() -> Optional[str]:
    """Point GTK's text direction at the language the UI actually runs in.

    Returns the language code the direction was taken from, or ``None`` when
    there was nothing to go on and GTK's own default was left alone. Must run
    before the first widget exists -- the default direction is read at
    construction time, so a later change would leave built widgets behind.

    This deliberately follows :func:`ui_language_codes`, which puts ``LANGUAGE``
    first: when the two disagree, the messages the user sees come from
    ``LANGUAGE``, so that is the one the layout has to match.
    """
    codes = ui_language_codes()
    if not codes:
        return None
    code = codes[0]
    try:
        from gi.repository import Gtk

        Gtk.Widget.set_default_direction(
            Gtk.TextDirection.RTL if is_rtl_language(code)
            else Gtk.TextDirection.LTR
        )
    except Exception as exc:  # pragma: no cover - GTK missing or too old
        logger.debug("Could not set the interface text direction: %s", exc)
        return None
    return code


# Where the session's own ``LANGUAGE`` is parked while an explicit choice
# overrides it. "Restart Now" re-execs through ``os.execv``, which keeps the
# environment, so an override outlives the preference that set it: choosing a
# language and then going back to "System Default" left ``LANGUAGE`` behind and
# the interface came back up in the old language, which only picking a language
# explicitly could clear. Stashing the original is what makes the way back work.
_ORIGINAL_LANGUAGE_VAR = 'SSHPILOT_ORIGINAL_LANGUAGE'


def apply_language(code: Optional[str] = None) -> str:
    """Export ``LANGUAGE`` for the chosen code. Returns what was applied.

    Must be called before the first ``_()`` in the process.

    An empty code means "system default", which has to *undo* any override this
    application put in the environment -- not merely skip setting one -- because
    the process may have inherited that override from the instance it replaced.
    Whatever ``LANGUAGE`` the session itself provided is restored; a session that
    set none gets none back. An empty code in a process that never overrode
    anything leaves the environment alone, so the user's own setting stands.
    """
    if code is None:
        code = configured_language()
    if code:
        # Only the first override records the original: later ones are replacing
        # our own value, not the session's.
        os.environ.setdefault(_ORIGINAL_LANGUAGE_VAR, os.environ.get('LANGUAGE', ''))
        os.environ['LANGUAGE'] = code
        return code

    original = os.environ.pop(_ORIGINAL_LANGUAGE_VAR, None)
    if original is None:
        return code
    if original:
        os.environ['LANGUAGE'] = original
    else:
        os.environ.pop('LANGUAGE', None)
    return code

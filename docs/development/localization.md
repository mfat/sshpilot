# Localization

Every change that adds or rewords user-visible text ships translated. A string
that is `_()`-wrapped but never reaches the catalogues still renders in English
in every locale, so "wrapped" is not "done".

Catalogues: `de es fa fr pt pt_BR ru zh_CN` (`po/LINGUAS`). French is also
maintained by an outside contributor; update it anyway, they refine it later.

## What counts as user-visible

- GTK widgets, dialogs, toasts, tooltips, menu items, accessible labels.
- Blueprint (`.blp`) text: mark it `_("...")`; the compiled `.ui` is what
  POTFILES lists.
- Error and status text shown to the user, including anything that started life
  in the daemon, core, or a built-in plugin.
- CLI output meant for people, `.desktop` and metainfo text.

Not translated: log lines, exception messages that only reach logs, SSH
directive names and other ssh_config keywords, identifiers, wire values.

## The daemon/frontend boundary

The daemon, `core/`, the API models and built-in plugins are locale-neutral.
They never call gettext and never send rendered English sentences across RPC.
Instead:

1. The backend raises or returns a **stable code** (a `str` `Enum` in
   `api/models/…`, e.g. `PluginSessionFailureCode`, `LoginProfileErrorReason`,
   `SecretMessageCode`) plus **strict, validated parameters** (program name,
   field, keyword…) and, optionally, an **opaque diagnostic** (tool/OpenSSH
   output) that is never used as a msgid.
2. The frontend owns wording in a presenter module,
   `src/sshpilot/gtk/<feature>_messages.py`: a code → `N_("template")` table,
   translated with `_()` at display time and formatted **after** translation.
   The opaque diagnostic is appended unchanged.
3. Adding a code changes the public API: bump
   `API_IMPLEMENTATION_VERSION`, add a `docs/api/CHANGELOG.md` entry and
   regenerate the API snapshots/schema (see de74bc92 for a minimal example,
   02cf7375 for a full one).
4. An architecture test pins the boundary
   (`tests/architecture/test_*_i18n_boundary.py`): the backend contains no
   `gettext` and no display strings, the presenter's sources are in POTFILES,
   and the dialog goes through the presenter. Extend the existing test for the
   feature or add one.

Do not "fix" an untranslated daemon message by wrapping it in `_()` on the
daemon side or by translating the received English on the frontend.

## Writing strings

- `from gettext import gettext as _, ngettext`; `N_` comes from
  `sshpilot.i18n` and only marks a string for extraction.
- Translate first, format second: `_("Copied {count} files").format(...)`,
  never `_(f"...")` or `_("..." + x)`. Use named `{placeholders}` so
  translators can reorder them.
- Counts use `ngettext`, not `"file(s)"` or an `if n == 1`.
- Whole sentences, not fragments glued together; word order differs per
  language (and `fa` is RTL).
- Watch msgid collisions: the same English word in two contexts gets one
  translation ("Interface" in Preferences vs a network table column). Reword
  one of them if the meanings differ.
- A new Python or `.ui` file with strings goes into `po/POTFILES`.

## Updating the catalogues

```bash
meson compile -C _build sshpilot-pot            # po/sshpilot.pot (gitignored)
for l in $(cat po/LINGUAS); do
  msgmerge --backup=none -U po/$l.po po/sshpilot.pot
done
# fill every new/fuzzy msgstr in each .po; remove "#, fuzzy" once correct
bash scripts/build_gresource.sh                 # po -> committed .mo (and .blp -> .ui)
for l in $(cat po/LINGUAS); do msgfmt --check --statistics po/$l.po -o /dev/null; done
```

Commit the `.po` files and `src/sshpilot/locale/*/LC_MESSAGES/sshpilot.mo`
together with the code change (or as the immediately following commit).

## Verifying

- `msgfmt --check` passes and reports no new untranslated or fuzzy entries for
  the strings you added.
- Render for real, e.g. `LANGUAGE=de` against
  `gettext.translation('sshpilot', 'src/sshpilot/locale')`, or run the app with
  `LANGUAGE=de`, and confirm the new text is not English.
- Run the relevant `tests/architecture/test_*_i18n_boundary.py` and
  `tests/test_*_i18n*.py`.

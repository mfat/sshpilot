# Status bar and notification center

The main window has a full-width status bar along its bottom edge. Its left side
shows one short message at a time. On the right it has two buttons:

- **Logs** opens the log viewer (`win.view-logs`).
- **Notifications** (a bell) opens the notification center (`win.show-notifications`).
  The bell carries a red badge counting unread entries across both tabs.

The notification center is an `Adw.Dialog` with two tabs:

- **Activity** holds entries sent by the app or by plugins.
- **News** holds announcements fetched from `data/news.json`.

Entries are grouped by day. Each one expands to show its details, and offers
Copy, Dismiss and at most one action button. The history survives restarts: it
is saved in `notifications.json` in the state directory, keeping the last 200
activity entries plus the ids of dismissed news.

Visibility is controlled from:

- Settings ▸ Interface ▸ Status Bar (`ui.show_status_bar`), or View ▸ Status Bar.
- Settings ▸ Interface ▸ Show News and Announcements (`ui.show_news`), which
  turns the news fetch on or off.

Terminal fullscreen always hides the bar.

Everything uses GTK 4.0 and libadwaita 1.5 widgets, which is the oldest
supported stack. `Gtk.Statusbar` is deprecated and is not used.

## Nothing is shown by default

Both catalogues ship empty. A message or notification appears only after it has
been explicitly added to one of these two files:

| What | Curation file |
|---|---|
| Status bar messages | `src/sshpilot/gtk/status_bar_messages.py` |
| Notification center entries | `src/sshpilot/gtk/notification_messages.py` |

Codes are stable strings such as `"transfer.failed"`. Wording lives only in
these two files, marked with `N_()` and translated when shown. Code that sends
a message or notification passes a code and its parameters, never a sentence.

### Show a message in the status bar

1. Add the message to `STATUS_MESSAGES`:

   ```python
   STATUS_MESSAGES["config.reloaded"] = StatusMessageSpec(
       template=N_("Reloaded “{name}”"),
       level=StatusLevel.SUCCESS,   # info | success | warning | error
       timeout_s=5,                 # None: stays until the next message
   )
   ```

2. Post it from anywhere. Posting is safe from any thread:

   ```python
   from sshpilot.status_bar import post_status
   post_status("config.reloaded", name=path)
   ```

For a count, set `plural=N_("...{count}...")` and pass `count=`. The right form
is then picked with `ngettext`.

### Send an entry to the notification center

1. Add the entry to `NOTIFICATIONS`:

   ```python
   NOTIFICATIONS["transfer.failed"] = NotificationSpec(
       title=N_("Transfer to “{name}” failed"),
       details=N_("{error}"),
       severity=NotificationSeverity.ERROR,   # info success important warning error critical
       action=ActionSpec(N_("Open Log"), "win.view-logs"),
   )
   ```

2. Send it:

   ```python
   from sshpilot.notification_store import notify
   notify("transfer.failed", name=nickname, error=str(exc))
   ```

The action button runs an existing `Gio` action. `ActionSpec(..., target_param="name")`
passes that parameter's value as the action's string target.

Parameters are written to disk, after passing through the log redactor. Never
pass secrets, and keep parameters short.

### Route a daemon event

Add a row to `EVENT_ROUTES` (status bar) or `NOTIFY_EVENT_ROUTES`
(notification center). A route returns `(code, params)`, or `None` to skip the
event:

```python
def _route_forward_failed(event, lookup):
    return "forward.failed", {"name": lookup(event.connection_id)}

NOTIFY_EVENT_ROUTES[EventType.FORWARD_FAILED] = _route_forward_failed
```

`lookup` turns a connection id into the connection's display name.

The status bar subscribes to the daemon only for event types that appear in
these two tables. While both are empty, it does not subscribe at all.

Every new string must ship translated in all `po/LINGUAS` catalogues. See
`docs/development/localization.md`.

## News feed

`data/news.json` on `main` is fetched once at startup, from
`https://raw.githubusercontent.com/mfat/sshpilot/main/data/news.json`. It is not
fetched when "Show News and Announcements" is off. To publish an announcement,
edit the file on GitHub and commit it. A failed fetch is silent and leaves the
current news in place.

```json
{"version": 1, "items": [{
  "id": "2026-11-relay",
  "date": "2026-11-20",
  "severity": "info",
  "title": {"en": "Title", "fa": "عنوان"},
  "body": {"en": "Plain text, no markup."},
  "link": {"url": "https://…", "label": {"en": "Read more"}},
  "min_version": "5.0.0",
  "max_version": "5.2.0",
  "platforms": ["linux", "macos", "windows", "bsd"],
  "packages": ["flatpak", "appimage", "native", "macos-dmg", "windows"],
  "expires": "2026-12-31"
}]}
```

Field rules:

- **Required fields:** `id`, `date` and `title.en`. Everything else is optional.
- **`id`:** must stay stable. It is how a dismissed item stays dismissed. Editing
  an item's text under the same id updates it in place.
- **`severity`:** `info`, `important` or `critical`. `important` shows in the
  accent color and `critical` in red.
- **Text fields** (`title`, `body`, link `label`): per-language maps. The app
  picks the UI language, falling back to English.
- **`link.url`:** must be `https://`.
- **Version range:** `min_version` and `max_version` are inclusive.
- **`expires`:** the last day the item is shown.
- **Removing an item:** an item deleted from the feed disappears from the app on
  the next fetch.

An item that fails validation is skipped; the rest of the feed still loads.

## Plugins (API ≥ 1.17)

```python
ctx.ui.show_status(_("Deployed"), level="success", timeout=5)
ctx.ui.add_notification(_("Deploy finished"), details=log_tail, severity="success")
```

Plugins pass their own, already-translated text. See `docs/PLUGIN_SDK.md`.

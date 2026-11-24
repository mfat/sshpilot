# Opening Advanced Configuration at a place

Advanced Configuration shows the SSH configuration as blocks, with what
OpenSSH resolves for a host. Any part of the app can open it at a block, an
option or a line, in the root file or in a file the root reads through
`Include`. There is one way in; do not construct `AdvancedConfigWindow`
yourself and do not reach into its selection.

## The call

```python
window.open_advanced_config(
    host="edge",                  # the block that declares this host
    line=0,                       # or: a line of the file, as saved
    option="user",                # within the block: the option to show
    file=connection.source,       # the file it is in (a connection's source)
    pin=False,                    # make `host` the host OpenSSH is asked about
    over=None,                    # a modal window to open above
    on_saved=None,                # called after each save made there
)
```

`MainWindow.open_advanced_config` returns the window, or `None` when it could
not be opened (no daemon, or one without draft evaluation: a toast says so).
With no target it opens as from the menu.

Code that holds no window uses the app action, with the same fields as a JSON
object (unknown fields and wrong types are dropped):

```python
app.activate_action(
    "advanced-ssh-config-at",
    GLib.Variant("s", json.dumps({"host": "edge", "file": source})),
)
```

`app.advanced-ssh-config` (no parameter) is the menu and shortcut action.

## What each field means

| Field | Meaning |
| --- | --- |
| `host` | The block whose `Host` line declares the host (`ssh_config_document.declared_hosts`: a plain name no wildcard on the same line already matches). Used when no `line` is given or the line finds nothing. Not found: a toast, nothing moves. |
| `line` | A 1-based line of `file` **as saved**. The block or option it belongs to is shown wherever unsaved edits have moved it (the window keeps the line map of the text it loaded). A line no block owns (a comment, a blank line) is shown selected on the Text page. |
| `option` | A keyword. The first option of the block with that keyword is shown instead of the block's header. |
| `file` | A path as a connection reports its source. Empty, the root's own path, or a path the configuration does not read all mean the root. |
| `pin` | `host` is typed into the sidebar's field: it stays the host being checked whichever block is selected afterwards. |

The call may be made before the window has loaded, and for a file other than
the one on screen: the window loads what it needs and goes there. If the file
on screen has unsaved changes, the user is asked before another file replaces
it; keeping them cancels the navigation.

## Rules

- **Included files.** The window shows one file at a time; the sidebar's
  drop-down switches between the files of the configuration and is hidden
  while there is only one. A file is named to the daemon by the name the
  daemon gave out (`SshConfigText.files`), never by a path the frontend
  chose: `file=` above is looked up among those names and falls back to the
  root.
- **Above a modal window.** Nothing else can be used while the connection
  editor is up, so pass `over=dialog`: the window is then a second, modal one
  above it, and is not the app's single Advanced Configuration window.
- **Another editor on the same file.** A save in Advanced Configuration does
  not make an open connection editor stale (connection generations are not
  touched by a raw save). Pass `on_saved` to refresh what the caller shows;
  the connection editor passes the hook it already uses for the SSH config
  editor, and still saves only the fields changed in it.
- **A configuration OpenSSH rejects** opens like any other: the rejected line
  is marked on its card, and `line=` reaches it.

## Callers

- Sidebar, connection context menu: **Advanced Configuration** (`host`,
  `file`).
- Connection editor, foot of the dialog: **Advanced Configuration** (`host`,
  `file`, `over`, `on_saved`).

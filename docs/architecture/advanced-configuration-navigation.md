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
    over=None,                    # the modal dialog calling, if any
    on_saved=None,                # called after each save made there
    new_block=None,               # or: (Host line, [(keyword, value), ...])
)
```

`new_block` begins a block instead of showing a place: a new `Host` block with
that line and those options is added to the draft, selected and left unsaved
(`AdvancedConfigWindow.start_block`). The connection editor passes a pattern,
or several names, typed where a connection's name goes, with the host, user
and port typed beside it: a rule for several hosts and a Host line of several
names are authored here, not saved from the connection editor.

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
- **From a modal dialog.** A modal dialog (the connection editor) stops
  input to the other windows of its window group. Advanced Configuration is
  therefore an ordinary window in a group of its own: it stays usable while
  the dialog is up, there is still only one of it, and it can be maximized
  (a modal child of the dialog could not be). `over=` names the dialog the
  call comes from and changes nothing else.
- **Another editor on the same file.** A save in Advanced Configuration does
  not make an open connection editor stale (connection generations are not
  touched by a raw save). Pass `on_saved` to refresh what the caller shows;
  the connection editor passes the hook that re-marks its fields, and still
  saves only the fields changed in it.
- **A configuration OpenSSH rejects** opens like any other: the rejected line
  is marked on its card, and `line=` reaches it.

## Callers

- Sidebar, connection context menu: **Advanced Configuration** (`host`,
  `file`).
- Connection editor, foot of the dialog: **Advanced Configuration** (`host`,
  `file`, `over`, `on_saved`). It is the dialog's one way into the
  configuration: the "Saved Effective Config" and "Edit SSH Config" buttons
  that stood beside it are gone, since it shows both, and with them the
  effective-config viewer, which only the first of them opened.

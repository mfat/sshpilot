# Settings architecture

Core owns the settings **model**:

* defaults — `sshpilot.core.settings.get_default_config`
* migration / backfill — `ensure_config_defaults`
* serialization — `load_settings` / `save_settings`
* SSH override composition — `compose_ssh_overrides`

GTK owns the **adapter**:

* `sshpilot.config.Config` (GObject signals, path via GLib XDG helpers)
* Preferences widgets read/write through `Config`, then call
  `compose_ssh_overrides` for the flat `ssh.ssh_overrides` list

No `Gio.Settings` dependency and no widget state inside core models.
Domain objects must not call gettext; UI layers translate messages.

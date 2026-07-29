# Known hosts

`sshpilot.core.known_hosts` owns:

* parsing / filtering
* host match helpers (including hashed-line substring search)
* removal planning
* atomic writes with mode preservation and symlink refusal

`KnownHostsEditorWindow` is a GTK view that loads/saves through the core
service and maps failures to UI feedback.

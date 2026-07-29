# Key management

`sshpilot.core.keys.KeyService` provides:

* key discovery (header sniff, no private material in results)
* generation specs / `ssh-keygen` planning and execution
* public-key line parsing
* fingerprinting via `ssh-keygen -lf`

`sshpilot.key_manager.KeyManager` is a thin GObject adapter that emits
`key-generated` for GTK listeners. File pickers and confirmation dialogs stay in
the UI.

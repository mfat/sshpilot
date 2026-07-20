# sshPilot documentation

User-facing documentation (guides, FAQ) lives on the
[wiki](https://github.com/mfat/sshpilot/wiki). This folder holds developer and
platform documentation that versions with the code:

| Document | Contents |
| --- | --- |
| [running-from-source.md](running-from-source.md) | Running from source on Linux: the hybrid (system PyGObject + venv) and pure-venv (pip-built PyGObject) approaches, plus dev/test setup and troubleshooting. |
| [INSTALL-macos.md](INSTALL-macos.md) | Installing and running from source on macOS (Homebrew GTK stack). |
| [keyboard-shortcuts.md](keyboard-shortcuts.md) | Full shortcut reference for Linux and macOS. |
| [agent-architecture.md](agent-architecture.md) | The Ptyxis-style agent that provides host shells with job control under Flatpak. |
| [SSH-config-parsing.md](SSH-config-parsing.md) | Design rationale for host discovery: display-only parsing, `ssh -G` as the source of truth. |
| [command-converter.md](command-converter.md) | The `ssh ...` command-line → connection-data parser (not yet wired to UI). |
| [PLUGIN_SDK.md](PLUGIN_SDK.md) | The plugin API reference: context objects, protocol backends, UI pages, credential dialogs. |
| [IDENTITY_PROVIDERS.md](IDENTITY_PROVIDERS.md) | The identity-provider contract (which key authenticates a connection, and who supplies it). |
| [CREDENTIAL_MANAGER.md](CREDENTIAL_MANAGER.md) | The credential export/backup layer and its secret backends. |
| [plugins/](plugins/) | Writing, packaging and publishing plugins, plus copyable templates. |

## Askpass debug log

The askpass helper doesn't log through the main logger. It writes debug
information to `sshpilot-askpass.log` in the first available of:

1. `$SSHPILOT_ASKPASS_LOG_DIR`, if set
2. `$XDG_RUNTIME_DIR`
3. the system temporary directory

No askpass messages appear in the normal console or application log.

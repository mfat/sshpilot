# sshPilot documentation

User-facing documentation (guides, FAQ) lives on the
[wiki](https://github.com/mfat/sshpilot/wiki). This folder holds developer and
platform documentation that versions with the code:

| Document | Contents |
| --- | --- |
| [INSTALL-macos.md](INSTALL-macos.md) | Installing and running from source on macOS (Homebrew GTK stack). |
| [keyboard-shortcuts.md](keyboard-shortcuts.md) | Full shortcut reference for Linux and macOS. |
| [agent-architecture.md](agent-architecture.md) | The Ptyxis-style agent that provides host shells with job control under Flatpak. |
| [SSH-config-parsing.md](SSH-config-parsing.md) | Design rationale for host discovery: display-only parsing, `ssh -G` as the source of truth. |
| [command-converter.md](command-converter.md) | The `ssh ...` command-line → connection-data parser (not yet wired to UI). |

## Askpass debug log

The askpass helper doesn't log through the main logger. It writes debug
information to `sshpilot-askpass.log` in the first available of:

1. `$SSHPILOT_ASKPASS_LOG_DIR`, if set
2. `$XDG_RUNTIME_DIR`
3. the system temporary directory

No askpass messages appear in the normal console or application log.

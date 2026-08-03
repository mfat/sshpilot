# Daemon-only production architecture

```text
GTK UI / future Web UI / future CLI or TUI
                    │
             versioned daemon API
                    │
              DaemonServer
       dispatch, events, lifecycle
                    │
       application and core services
                    │
 config, secrets, sessions, PTYs, SFTP,
 transfers, interactions and forwarding
```

The GTK process calls and subscribes to the daemon API, renders either VTE or
PyXtermJS, collects input, and answers interaction requests. It owns no backend
process. Failure to locate, launch, negotiate with, or validate the daemon is
shown as a recoverable startup error and never selects another backend.

The daemon composition root constructs `CoreServices`. `DaemonServer` injects
the connection application service directly into API dispatch and composes
session, interaction, SFTP, transfer, and forwarding services around it. Core
and daemon modules import no GTK, VTE, WebKit, or graphical-session code.

Both renderers use `DaemonTerminalSessionController`: daemon bytes flow to the
active renderer backend, while renderer input and dimensions flow back through
the same controller. Attach/replay synchronization uses sequence identifiers.
Renderer preference remains the existing `terminal.backend` value.

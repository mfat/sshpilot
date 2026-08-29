# Secrets architecture

## Split

| Concern | Owner |
| --- | --- |
| Backend selection / fallthrough / unlock decisions | `sshpilot.core.secrets` |
| Store / lookup / delete implementations | `sshpilot.secret_storage` |
| libsecret `gi` load | `sshpilot.platform.linux.libsecret` |
| Unlock / password dialogs | GTK (`secret_unlock_dialog`, window dialogs) |
| Status/result message translation | GTK (`gtk.secret_status_messages`) |

Core policy returns structured decisions (`READY`, `UNLOCK_REQUIRED`,
`BACKEND_UNAVAILABLE`, …) and never carries secret values into logs, events, or
diagnostics payloads.

`SecretManager` consults core helpers for name normalization and platform
default order so CLI/tests can reason about policy without GTK.

Public unlock, operation, Bitwarden, and rbw results use stable
`SecretMessageCode` values with validated non-secret parameters. Backend/tool
diagnostics are carried separately and remain opaque. The daemon never invokes
gettext; GTK translates the code-owned template immediately before display and
formats parameters only after translation.

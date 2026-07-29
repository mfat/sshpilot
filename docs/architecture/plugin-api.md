# Plugin API split

## Packages

| Package | Contents |
| --- | --- |
| `sshpilot.core.plugins` | Headless contracts: `Capability`, `SpawnSpec`, `FieldSpec`, `Events`, `EventBus`, `ConnectionInfo`, `SessionInfo`, `API_VERSION` |
| `sshpilot.gtk.plugins` | GTK contribution surface (`UiHost` re-export) |
| `sshpilot.plugins.api` | **Compatibility shim** — still the supported import for existing plugins |
| `sshpilot.plugins.host` | Runtime host; event types re-exported from core; `UiHost` / `PluginHost` stay here |

## Deprecation

New headless or multi-frontend code should import contracts from
`sshpilot.core.plugins`. Existing plugins may keep:

```python
from sshpilot.plugins.api import Capability, SpawnSpec, PluginContext, ...
```

`PluginContext` and `ProtocolBackend` remain in `plugins.api` because they bridge
application services. UI-only helpers should move toward `sshpilot.gtk.plugins`
over time.

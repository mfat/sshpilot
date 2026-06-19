# Plugin discovery registry

Third-party plugins are hosted in their authors' own repositories. To make them
discoverable, sshPilot uses a curated index — a single `plugins.json` — at
**[`mfat/sshpilot-plugins`](https://github.com/mfat/sshpilot-plugins)** (the
sshPilot equivalent of jottr's `Jottrhq/plugins`). Authors open a PR adding an
entry; users browse/install from it.

> **In-app:** sshPilot auto-indexes this registry — **Preferences ▸ Plugins**
> lists not-yet-installed plugins under *Available Plugins*. Toggling one on
> downloads it, **verifies its SHA-256** against the `checksumUrl`, shows a
> permissions/trust prompt, then installs and enables it (restart to load).
> Manual install (**Install plugin… ▸ folder/`.zip`**) is still available. The
> index URL is configurable via the `plugins.registry_url` setting.

## `plugins.json` format

```json
{
  "schemaVersion": 1,
  "checksumAlgorithm": "sha256",
  "plugins": [
    {
      "id": "example-plugin",
      "name": "Example Plugin",
      "description": "One-line summary shown in the browser.",
      "author": "yourname",
      "homepage": "https://github.com/yourname/example-plugin",
      "latestVersion": "1.0.0",
      "versions": [
        {
          "version": "1.0.0",
          "api_version": 1,
          "permissions": ["process"],
          "package": {
            "downloadUrl": "https://github.com/yourname/example-plugin/releases/download/v1.0.0/example-plugin.zip",
            "checksumUrl": "https://github.com/yourname/example-plugin/releases/download/v1.0.0/example-plugin.zip.sha256"
          }
        }
      ]
    }
  ]
}
```

## Publishing a plugin

1. Build from the [template](template/); keep `plugin.json` accurate, including
   [`permissions`](writing-plugins.md#permissions).
2. Create a GitHub **release** and upload two assets: `your-plugin.zip` and a
   sibling `your-plugin.zip.sha256` (the archive's SHA-256). sshPilot verifies the
   checksum **before** extracting.
3. PR an entry into `mfat/sshpilot-plugins`' `plugins.json` (and optionally add a
   row to [community.md](community.md)).

## Integrity & trust

- The `.zip` is verified against its `checksumUrl` before extraction.
- The index itself can be checksum-verified (`checksumAlgorithm`) when fetched.
- Plugins are unsandboxed; the installer shows declared permissions + the archive
  SHA-256 and requires explicit consent before enabling. See
  [writing-plugins.md ▸ Security](writing-plugins.md#security--trust).

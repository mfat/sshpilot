# Community plugins

Third-party sshPilot plugins, maintained outside the core repo. Install them into
the user plugin dir (`~/.local/share/sshpilot/plugins/<id>/`, Flatpak:
`~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/<id>/`) or via
**Preferences ▸ Plugins ▸ Install plugin…**, then enable and restart.

> These are not maintained or vetted by the sshPilot maintainers. Plugins run
> in-process with full privileges — **install only what you trust.**

To list yours, scaffold from the
[**sshpilot-plugin-template**](https://github.com/mfat/sshpilot-plugin-template)
("Use this template"), publish a release, then add an entry to the discovery
index at [**mfat/sshpilot-plugins**](https://github.com/mfat/sshpilot-plugins)
(see the [registry format](registry.md)) — and optionally add a row below
(keep it alphabetical).

| Plugin | Author | Repository | Description |
|--------|--------|------------|-------------|
| _Your plugin here_ | | | |

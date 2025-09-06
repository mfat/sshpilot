# sshPilot Parsing Method

This document explains how sshPilot interprets OpenSSH configuration files to build a user-friendly list of connections while preserving all SSH behavior.

## 1. Connectable Hosts vs Configuration Rules

- **Connectable hosts**: Entries that represent a single server alias that can be shown in the UI and selected for connection.
- **Configuration rules**: Patterns, conditional blocks, or directives that modify SSH behavior but are not themselves connectable. These are hidden from the main UI, but their settings still apply when relevant.

## 2. Determining Connection Blocks

A `Host` block is treated as a connectable host only when it meets all of the following:

1. The block contains a `HostName` directive.
2. The `Host` line does not contain wildcard characters (`*`, `?`) or negations (`!`).
3. The `Host` line resolves to a single nickname; additional tokens are recorded as aliases but are not shown in the sidebar.

Blocks that lack a `HostName`, contain wildcards, or include negated patterns are treated as **rules** and are not displayed in the connection list. The raw text of these blocks is preserved so that OpenSSH still applies them.

## 3. Handling `Host` Line Tokens

- The first token after `Host` is considered the **nickname** used in the UI.
- Additional tokens are stored as **aliases/patterns** and are written back on save.
- Quoted nicknames are supported; quotes are stripped when storing but restored when saving.

## 4. Proxy Settings

`ProxyJump` and `ProxyCommand` do not change a block's classification. If a block meets the criteria for a connectable host, these directives are preserved and passed through when connecting.

## 5. Match and Include Directives

- `Match` blocks are always treated as rules.
- `Include` directives are processed recursively so that all referenced files are parsed in the same manner as the main config.

## 6. Case-Insensitive Keywords

All keywords (`Host`, `HostName`, `User`, etc.) are matched case-insensitively. Original casing is preserved when writing back unchanged lines.

## 7. UI Considerations

- Only connectable hosts are shown in the sidebar.
- Connection names in the sidebar are ellipsized to prevent layout issues when nicknames are very long.
- A future advanced view may expose rule blocks for inspection.

## 8. Effective Configuration Resolution

Before establishing a connection, sshPilot runs `ssh -G <nickname>` to obtain the final configuration for the chosen host. This ensures that all rules, matches, and included files are honored exactly as the user's SSH client would.


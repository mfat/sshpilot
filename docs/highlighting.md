# Terminal output highlighting

Output highlighting is opt-in under **Settings → Terminal → Highlighting**.
It adds foreground colour and authentication-success emphasis without changing
visible output bytes.

Highlight colours come from the active terminal theme. Colours that would
blend into the background are lightened or darkened to reach a contrast ratio
of at least 4.5:1 against its configured opaque background. This applies to
built-in and custom themes and backend group-background overrides. Adjusted
RGB colours prevent bold text from unexpectedly switching to a less readable
bright palette colour. Theme changes affect new output; existing scrollback
keeps its previously rendered highlight colours. Remote colour and intensity
choices are still respected, so remote faint text can reduce apparent contrast.

The built-in profiles are:

- **Generic** for common IP addresses, MAC addresses, VLANs, interfaces, and status words.
- **Cisco IOS** for IOS interface and command-output conventions.
- **Junos** for Junos interface and command-output conventions.
- **Huawei VRP** for VRP interface and command-output conventions.

All profiles also colour log severity labels:

- Red: `EMERGENCY`, `EMERG`, `ALERT`, `CRITICAL`, `CRIT`, `FATAL`, `ERROR`, `ERR`.
- Yellow: `WARNING`, `WARN`.
- Cyan: `NOTICE`, `INFO`.
- Magenta: `DEBUG`, `TRACE`.

Uppercase labels, bracketed labels such as `[warn]`, and `level=error` fields
are recognised. Bracketed labels and `level=` values accept mixed case.
Only the severity label is coloured, leaving network tokens in the message
available for their usual highlighting. Existing command-error rules still
colour messages such as `Error: ...` and `% Invalid input ...` in red.
Matching is heuristic; ordinary lowercase words and labels embedded in
filenames or identifiers are left alone. As with network tokens, a label split
across output chunks can only be coloured from the chunk boundary onward.

Authentication-success messages (`Authenticated to ...`, `Authentication
succeeded ...`, and `Authentication successful ...`, optionally prefixed with
`debug1:`, `debug2:`, or `debug3:`) are highlighted as a whole line in **bold
green**. `Server accepts key: ...` and `Using private key: ...` lines are cyan,
including the displayed key filename and fingerprint. These rules only format
existing terminal output; they do not expose the daemon's private diagnostics
or private-key contents. Emphasis continues across chunks once the prefix is
recognised and stops at the next line break. Text already displayed before a
split prefix is recognised cannot be retroactively styled.

Highlighting is suspended while a remote application controls foreground or
background colour, reverse or concealed text, or the alternate screen. Parser
state is reset when a terminal reconnects.

The feature is disabled by default. Select a profile and enable **Highlight
network output** to apply it to new terminal output.

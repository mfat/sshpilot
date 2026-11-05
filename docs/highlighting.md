# Terminal output highlighting

Output highlighting is opt-in under **Settings → Terminal → Highlighting**.
It adds foreground colour without changing visible output bytes.

The built-in profiles are:

- **Generic** for common IP addresses, MAC addresses, VLANs, interfaces, and status words.
- **Cisco IOS** for IOS interface and command-output conventions.
- **Junos** for Junos interface and command-output conventions.
- **Huawei VRP** for VRP interface and command-output conventions.

Highlighting is suspended while a remote application controls foreground or
background colour, reverse or concealed text, or the alternate screen. Parser
state is reset when a terminal reconnects.

The feature is disabled by default. Select a profile and enable **Highlight
network output** to apply it to new terminal output.

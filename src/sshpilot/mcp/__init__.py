"""SSH Pilot MCP servers.

Two separate servers with different trust boundaries:

* ``sshpilot.mcp.dev`` — contributor-safe repository intelligence.
* ``sshpilot.mcp.runtime`` — opt-in privileged daemon/API integration.

See ``docs/mcp/`` for the governing rules.
"""

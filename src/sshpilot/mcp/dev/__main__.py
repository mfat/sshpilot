"""Run the dev MCP server: ``python -m sshpilot.mcp.dev``."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
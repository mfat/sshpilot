"""Run the runtime MCP server: ``python -m sshpilot.mcp.runtime``."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
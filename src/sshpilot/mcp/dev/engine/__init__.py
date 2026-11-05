"""Development-intelligence engine for the dev MCP server.

Pure analysis over the checkout (stdlib ``ast`` + ``sqlite3``); no MCP SDK
dependency. The MCP server (``sshpilot.mcp.dev.server``) and the CLI
(``python -m sshpilot.mcp.dev.cli``) are thin adapters over
:class:`~sshpilot.mcp.dev.engine.service.DevIntelligence`.
"""

from .models import RESULT_SCHEMA, EngineError
from .service import DevIntelligence

__all__ = ["RESULT_SCHEMA", "DevIntelligence", "EngineError"]

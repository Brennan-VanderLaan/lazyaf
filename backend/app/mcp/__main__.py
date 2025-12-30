"""
Entry point for running LazyAF MCP server with stdio transport.

This is used by Claude Desktop which spawns the server as a subprocess.

Usage:
    python -m app.mcp

Claude Desktop config (claude_desktop_config.json):
{
    "mcpServers": {
        "lazyaf": {
            "command": "uv",
            "args": ["run", "--project", "C:\\projects\\lazyaf\\backend", "python", "-m", "app.mcp"]
        }
    }
}
"""
import asyncio
import sys

# Ensure we can import app modules
sys.path.insert(0, str(__file__).rsplit("app", 1)[0])

from app.mcp.server import mcp


def main():
    """Run the MCP server with stdio transport for Claude Desktop."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Hosted MCP (06 "/mcp/{slug}", 09): a published site's documentation as read-only MCP tools.

`tokens` manages personal access tokens (private sites); `router` serves the token API and the MCP
endpoint (JSON-RPC over Streamable HTTP, stateless, JSON responses); `tools` reads a site's build
through the same service functions as the viewer.
"""

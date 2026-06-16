"""Tool injection for the proteus_retrieve function.

Injects a function tool definition into the tools array so the LLM
can call it to retrieve original uncompressed content.
"""

from __future__ import annotations

from typing import Any

RETRIEVE_TOOL_NAME = "proteus_retrieve"


def create_retrieve_tool_definition() -> dict[str, Any]:
    """Create the proteus_retrieve tool definition (OpenAI format).

    Returns:
        Tool definition dict suitable for the tools array in
        OpenAI-compatible /v1/chat/completions requests.
    """
    return {
        "type": "function",
        "function": {
            "name": RETRIEVE_TOOL_NAME,
            "description": (
                "Retrieve original uncompressed content that was compressed by Proteus to save tokens. "
                "Use this when you need more data than what's shown in the compressed tool results. "
                "The hash is provided in compression markers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "Hash key from the compression marker (e.g., 'abc123' from hash=abc123)",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional search query to filter results. "
                            "If provided, only returns items matching the query. "
                            "If omitted, returns all original content."
                        ),
                    },
                },
                "required": ["hash"],
            },
        },
    }


def inject_retrieve_tool(tools: list[dict] | None) -> list[dict]:
    """Inject the proteus_retrieve tool if not already present.

    Args:
        tools: Existing tools array from the request.

    Returns:
        Updated tools array with proteus_retrieve appended.
    """
    if tools is None:
        tools = []

    # Check if already injected
    for tool in tools:
        name = tool.get("function", {}).get("name", "") if tool.get("type") == "function" else tool.get("name", "")
        if name == RETRIEVE_TOOL_NAME:
            return tools

    tools = list(tools)
    tools.append(create_retrieve_tool_definition())
    return tools
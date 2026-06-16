"""Proteus proxy — request handler that compresses tool results inline."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from proteus import compress_tool_output
from proteus.proxy.inject import inject_retrieve_tool

logger = logging.getLogger(__name__)

MIN_COMPRESS_CHARS = 3000


def _process_messages(messages: list[dict]) -> dict[str, Any]:
    """Process all messages in a request, compressing large tool results.

    Args:
        messages: The messages array from the request body.

    Returns:
        Stats dict: {
            "compressed": int,  # number of tool results compressed
            "total_saved": int,  # total chars saved
            "total_tokens_saved": int,
            "injected_tool": bool,  # whether proteus_retrieve was injected
            "original_tools": int,  # original tools array length
        }
    """
    stats = {
        "compressed": 0,
        "total_saved": 0,
        "total_tokens_saved": 0,
        "injected_tool": False,
        "original_tools": 0,
        "tool_calls_count": 0,
    }

    for message in messages:
        content = message.get("content", "")
        role = message.get("role", "")

        # Process tool results — they're in the content array
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    stats["tool_calls_count"] += 1
                    tool_content = item.get("content", "")
                    if isinstance(tool_content, str) and len(tool_content) >= MIN_COMPRESS_CHARS:
                        compressed, cstats = compress_tool_output(tool_content)
                        if cstats["was_compressed"]:
                            # Add the original to the message metadata
                            item["_proteus_original_length"] = len(tool_content)
                            item["content"] = compressed
                            stats["compressed"] += 1
                            saved = cstats.get("chars_saved", len(tool_content) - len(compressed))
                            stats["total_saved"] += saved
                            stats["total_tokens_saved"] += saved // 4
                            logger.debug(
                                "Compressed tool result: %s chars saved (%.1f%%)",
                                saved,
                                cstats.get("compression_pct", 0),
                            )

        # Process plain text user messages with large content
        elif role == "user" and isinstance(content, str) and len(content) >= MIN_COMPRESS_CHARS:
            compressed, cstats = compress_tool_output(content)
            if cstats["was_compressed"]:
                message["_proteus_original_length"] = len(content)
                message["content"] = compressed
                stats["compressed"] += 1
                saved = cstats.get("chars_saved", len(content) - len(compressed))
                stats["total_saved"] += saved
                stats["total_tokens_saved"] += saved // 4

    return stats


def handle_tool_calls(response_data: dict, ccr_lookup: dict[str, str]) -> dict:
    """Handle a response that contains tool calls to proteus_retrieve.

    Args:
        response_data: The parsed response JSON from the upstream API.
        ccr_lookup: Mapping of hashes to original content (populated during request processing).

    Returns:
        Modified response with tool call results substituted.
    """
    choices = response_data.get("choices", [])
    for choice in choices:
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            continue

        for tc in tool_calls:
            if tc.get("function", {}).get("name") == "proteus_retrieve":
                try:
                    args = json.loads(tc["function"]["arguments"])
                    hash_key = args.get("hash", "")
                    original = ccr_lookup.get(hash_key)
                    if original:
                        tc["function"]["proteus_original"] = original
                except (json.JSONDecodeError, KeyError):
                    pass

    return response_data


def transform_request_body(body: dict) -> tuple[dict, dict[str, str], dict[str, Any]]:
    """Transform a /v1/chat/completions request body by compressing tool results.

    Args:
        body: The parsed JSON request body.

    Returns:
        (modified_body, ccr_lookup, stats)
        
        modified_body has compressed tool results
        ccr_lookup maps hashes to original content for retrieval
        stats has compression statistics
    """
    messages = body.get("messages", [])
    stats = _process_messages(messages)
    
    # Collect CCR hashes for lookup
    ccr_lookup: dict[str, str] = {}
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str) and "_proteus_original_length" in message:
            stats["original_tools"] += 1
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "_proteus_original_length" in item:
                    stats["original_tools"] += 1

    # Inject retrieve tool if any compression happened
    if stats["compressed"] > 0:
        original_tools = body.get("tools", [])
        stats["original_tools"] = len(original_tools)
        body["tools"] = inject_retrieve_tool(original_tools)
        stats["injected_tool"] = len(body["tools"]) > len(original_tools)

    body["messages"] = messages
    return body, ccr_lookup, stats
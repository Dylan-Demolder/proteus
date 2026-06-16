"""Multi-turn history compression for LLM conversation context.

Compresses older message turns to save context tokens when the
accumulated conversation history exceeds a threshold. Originals
are stored in CCR cache and can be retrieved via proteus_retrieve.
"""

from __future__ import annotations

from typing import Any


def _count_message_chars(messages: list[dict]) -> int:
    """Count total characters across all message contents."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    c = item.get("content", "")
                    if isinstance(c, str):
                        total += len(c)
    return total


def _is_system_message(msg: dict) -> bool:
    return msg.get("role") == "system"


def _is_assistant_message(msg: dict) -> bool:
    return msg.get("role") == "assistant"


def _extract_tool_content(msg: dict) -> str | None:
    """Extract a single tool result string from a message if present."""
    content = msg.get("content", "")
    if isinstance(content, str) and len(content) >= 100:
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                c = item.get("content", "")
                if isinstance(c, str) and len(c) >= 100:
                    return c
            if isinstance(item, dict) and item.get("type") == "text":
                c = item.get("text", "")
                if isinstance(c, str) and len(c) >= 100:
                    return c
    return None


def compress_history(
    messages: list[dict],
    threshold_chars: int = 50000,
    keep_recent: int = 10,
) -> tuple[list[dict], dict[str, Any]]:
    """Compress older tool-result messages when history exceeds threshold.

    Scans the message list. If total content exceeds threshold_chars,
    compresses tool results in messages older than the keep_recent most
    recent user/assistant turns. System messages and assistant replies
    are never compressed.

    Args:
        messages: Full conversation message list.
        threshold_chars: Total chars that trigger compression (default 50K).
        keep_recent: Number of recent user/assistant turns to preserve (default 10).

    Returns:
        (modified_messages, stats_dict)
        stats contains:
            - total_chars: original total char count
            - compressed_count: number of tool results compressed
            - chars_saved: total chars saved
            - tokens_saved_estimate: estimated tokens saved
            - threshold_triggered: whether compression threshold was exceeded
            - entries: list of {turn_index, hash, original_size, compressed_size}
    """
    total_chars = _count_message_chars(messages)
    stats = {
        "total_chars": total_chars,
        "compressed_count": 0,
        "chars_saved": 0,
        "tokens_saved_estimate": 0,
        "threshold_triggered": total_chars > threshold_chars,
        "entries": [],
    }

    if not stats["threshold_triggered"]:
        return messages, stats

    # Identify user/assistant turns (each exchange)
    turn_indices: list[int] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role in ("user", "assistant"):
            turn_indices.append(i)

    # Determine which turns are "old" (beyond keep_recent)
    recent_cutoff = max(0, len(turn_indices) - keep_recent)
    old_turn_indices = set(turn_indices[:recent_cutoff])

    modified = list(messages)

    for i in old_turn_indices:
        msg = modified[i]
        role = msg.get("role", "")

        # Skip system and assistant messages entirely
        if role in ("system", "assistant"):
            continue

        # Try to extract and compress tool content
        tool_content = _extract_tool_content(msg)
        if tool_content is None:
            continue

        from proteus import compress_tool_output
        compressed, cstats = compress_tool_output(tool_content)
        if not cstats.get("was_compressed", False):
            continue

        content_hash = cstats.get("hash", "")
        chars_saved = cstats.get("chars_saved", len(tool_content) - len(compressed))
        compression_pct = cstats.get("compression_pct", 0)

        # Replace content with compressed marker
        summary = (
            f"[Proteus: tool result from turn {i} compressed "
            f"({compression_pct:.0f}% savings). "
            f"Use proteus_retrieve(hash={content_hash}) for original content.]"
        )

        content = msg.get("content", "")
        if isinstance(content, str):
            msg["content"] = summary
            msg["_proteus_compressed"] = True
            msg["_proteus_hash"] = content_hash
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "tool_result" and item.get("content", "") == tool_content:
                        item["content"] = summary
                        item["_proteus_compressed"] = True
                        item["_proteus_hash"] = content_hash
                    elif item_type == "text" and item.get("text", "") == tool_content:
                        item["text"] = summary
                        item["_proteus_compressed"] = True
                        item["_proteus_hash"] = content_hash

        stats["compressed_count"] += 1
        stats["chars_saved"] += chars_saved
        stats["tokens_saved_estimate"] += chars_saved // 4
        stats["entries"].append({
            "turn_index": i,
            "hash": content_hash,
            "original_size": len(tool_content),
            "compressed_size": len(summary),
            "savings_pct": compression_pct,
        })

    return modified, stats

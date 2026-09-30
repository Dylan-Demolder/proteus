"""Multi-turn history compression for LLM conversation context.

Compresses older message turns to save context tokens when the
accumulated conversation history exceeds a threshold. Originals
are stored in CCR cache and can be retrieved via proteus_retrieve.
"""

from __future__ import annotations

import re
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


# Roles whose content is a tool's output in the OpenAI chat format.
_TOOL_ROLES = ("tool", "function")

# Already-compressed content carries one of these markers (history's own, or
# the proxy's). Re-compressing it on every turn would stack markers and
# summarise summaries.
_MARKER = re.compile(r"\[[Pp]roteus: ")


def _retrieve_call_ids(messages: list[dict]) -> set[str]:
    """IDs of proteus_retrieve calls: their results are originals the model asked for."""
    ids: set[str] = set()
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            function = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(function, dict) and function.get("name") == "proteus_retrieve":
                ids.add(tc.get("id", ""))
    return ids


def _compress_text(text: str, turn: int) -> tuple[str, dict] | None:
    """Compress one old tool output; None if it isn't worth it or was done already."""
    if len(text) < 100 or _MARKER.search(text):
        return None
    from proteus import compress_tool_output

    compressed, cstats = compress_tool_output(text)
    if not cstats.get("was_compressed", False):
        return None
    marker = (
        f"\n[Proteus: tool result from turn {turn} compressed "
        f"({cstats.get('compression_pct', 0):.0f}% smaller). "
        f'Full original: proteus_retrieve(hash="{cstats["hash"]}")]'
    )
    replacement = compressed + marker
    if len(replacement) >= len(text):
        return None
    return replacement, cstats


def compress_history(
    messages: list[dict],
    threshold_chars: int = 50000,
    keep_recent: int = 10,
) -> tuple[list[dict], dict[str, Any]]:
    """Compress older tool results when a conversation exceeds a size threshold.

    If total content exceeds threshold_chars, tool results that come before
    the keep_recent most recent user/assistant messages are compressed in
    place of the original: the compressed text plus a marker naming the CCR
    hash of the full original. Handled forms: OpenAI ``role: "tool"``
    messages, ``tool_result`` and ``text`` parts in a content list, and large
    plain-text user messages. System and assistant messages are never
    compressed, nor are results of proteus_retrieve calls, nor content that
    is already compressed (so calling this every turn is safe).

    The input list and its messages are not modified.

    Args:
        messages: Full conversation message list.
        threshold_chars: Total chars that trigger compression (default 50K).
        keep_recent: Number of recent user/assistant messages to leave untouched (default 10).

    Returns:
        (new_messages, stats_dict)
        stats contains:
            - total_chars: original total char count
            - compressed_count: number of tool results compressed
            - chars_saved: total chars saved
            - tokens_saved_estimate: estimated tokens saved
            - threshold_triggered: whether compression threshold was exceeded
            - entries: list of {turn_index, hash, original_size, compressed_size, savings_pct}
    """
    total_chars = _count_message_chars(messages)
    stats: dict[str, Any] = {
        "total_chars": total_chars,
        "compressed_count": 0,
        "chars_saved": 0,
        "tokens_saved_estimate": 0,
        "threshold_triggered": total_chars > threshold_chars,
        "entries": [],
    }

    if not stats["threshold_triggered"]:
        return messages, stats

    # Everything before the first of the keep_recent most recent
    # user/assistant messages is "old".
    turn_indices = [i for i, m in enumerate(messages) if m.get("role") in ("user", "assistant")]
    if keep_recent <= 0:
        cutoff = len(messages)
    elif len(turn_indices) > keep_recent:
        cutoff = turn_indices[-keep_recent]
    else:
        cutoff = 0

    skip_ids = _retrieve_call_ids(messages)
    result = list(messages)

    def record(i: int, original: str, replacement: str, cstats: dict) -> None:
        saved = len(original) - len(replacement)
        stats["compressed_count"] += 1
        stats["chars_saved"] += saved
        stats["tokens_saved_estimate"] += saved // 4
        stats["entries"].append({
            "turn_index": i,
            "hash": cstats["hash"],
            "original_size": len(original),
            "compressed_size": len(replacement),
            "savings_pct": cstats.get("compression_pct", 0),
        })

    for i in range(cutoff):
        msg = messages[i]
        role = msg.get("role", "")
        if role not in _TOOL_ROLES and role != "user":
            continue
        if role in _TOOL_ROLES and msg.get("tool_call_id") in skip_ids:
            continue

        content = msg.get("content", "")
        if isinstance(content, str):
            done = _compress_text(content, i)
            if done:
                result[i] = {**msg, "content": done[0]}
                record(i, content, *done)
        elif isinstance(content, list):
            new_parts = []
            changed = False
            for part in content:
                key = None
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    key = "content"
                elif isinstance(part, dict) and part.get("type") == "text":
                    key = "text"
                text = part.get(key) if key else None
                if key and isinstance(text, str):
                    done = _compress_text(text, i)
                    if done:
                        new_parts.append({**part, key: done[0]})
                        record(i, text, *done)
                        changed = True
                        continue
                new_parts.append(part)
            if changed:
                result[i] = {**msg, "content": new_parts}

    return result, stats

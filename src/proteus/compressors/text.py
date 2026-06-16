"""TextSummarizer — compress long plain text output.

Strategy:
1. For text >10K chars, keep head + tail with a summary marker
2. Drop middle section entirely
3. Always preserve content at natural boundaries (paragraphs, sections)
"""

from __future__ import annotations

import re

from .. import config

# ── Section boundary patterns ──
_SECTION_HEADING = re.compile(r"^#{1,6}\s|\n#{1,6}\s", re.MULTILINE)
_PARAGRAPH_BREAK = re.compile(r"\n\n+")


def summarize_text(
    content: str,
    max_chars: int | None = None,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> tuple[str, dict]:
    """Summarize long plain text by keeping head + tail.

    Args:
        content: Long text content.
        max_chars: Threshold above which to summarize (default: config).
        head_chars: Chars to keep from start (default: config).
        tail_chars: Chars to keep from end (default: config).

    Returns:
        (summarized_text, stats_dict)
    """
    if max_chars is None:
        max_chars = config.TEXT_MAX_CHARS
    if head_chars is None:
        head_chars = config.TEXT_HEAD_CHARS
    if tail_chars is None:
        tail_chars = config.TEXT_TAIL_CHARS

    stats = {
        "original_chars": len(content),
        "mode": "text_summary",
    }

    if len(content) <= max_chars:
        stats["compressed_chars"] = len(content)
        stats["was_summarized"] = False
        return content, stats

    # Find natural break points near head_chars
    head_end = _find_boundary(content, head_chars, direction="forward")
    tail_start = _find_boundary(content, len(content) - tail_chars, direction="backward")

    head = content[:head_end]
    tail = content[tail_start:]
    middle_dropped = len(content) - len(head) - len(tail)

    word_count_estimate = middle_dropped // 5  # Rough estimate

    compressed = (
        f"{head}\n"
        f"... [TEXT COMPRESSED: ~{middle_dropped:,} chars / ~{word_count_estimate:,} words dropped — "
        f"use proteus_retrieve to get full content] ...\n"
        f"{tail}"
    )

    stats["compressed_chars"] = len(compressed)
    stats["was_summarized"] = True
    stats["dropped_chars"] = middle_dropped

    return compressed, stats


def _find_boundary(text: str, position: int, direction: str = "forward") -> int:
    """Find the nearest natural boundary (paragraph break or newline) near position."""
    if direction == "forward":
        search_region = text[position:position + 200]
        # Look for paragraph break first
        m = _PARAGRAPH_BREAK.search(search_region)
        if m:
            return position + m.start() + 1
        # Then single newline
        nl = search_region.find("\n")
        if nl != -1:
            return position + nl + 1
        return position + len(search_region)
    else:  # backward
        search_start = max(0, position - 200)
        search_region = text[search_start:position]
        # Look for paragraph break from the end
        matches = list(_PARAGRAPH_BREAK.finditer(search_region))
        if matches:
            return search_start + matches[-1].start() + 1
        # Then single newline from the end
        nl = search_region.rfind("\n")
        if nl != -1:
            return search_start + nl + 1
        return position
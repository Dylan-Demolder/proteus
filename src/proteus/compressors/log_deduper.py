"""LogDeduper — compress repetitive log and build output.

Strategy:
1. Detect duplicate lines (exact or pattern-matched)
2. Count occurrences instead of repeating
3. Keep stack traces intact (not repetitive)
4. Preserve first and last occurrence of each pattern
5. Always keep ERROR/FATAL lines
"""

import re
import hashlib
from collections import Counter
from typing import Optional

from .. import config


# ── Stack trace detection ──
_STACK_TRACE_LINES = re.compile(
    r"^\s*(File \".*?\", line \d+|Traceback|  File |^  \w+|During handling|^    \w+\.\w+)"
)
_ERROR_PREFIX = re.compile(r"(ERROR|FATAL|CRITICAL|FAILED|Error:|FATAL:)", re.IGNORECASE)
_WARN_PREFIX = re.compile(r"(WARN|WARNING)", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]?\d{2}:\d{2}(:\d{2}([.,]\d+)?)?")
_PATH_LIKE = re.compile(r"/[\w./-]+(?:\s|:|$)")


def _normalize_for_dedup(line: str) -> str:
    """Normalize a line by removing timestamps and path-specific values
    so that semantically identical log lines with different timestamps/paths
    can be grouped.
    """
    normalized = _TIMESTAMP.sub("<TS>", line)
    normalized = _PATH_LIKE.sub("<PATH>", normalized)
    return normalized.strip()


def _score_line(line: str) -> float:
    """Score a log line by importance (higher = more important to keep)."""
    score = 0.0
    if _ERROR_PREFIX.search(line):
        score += 10.0
    if _STACK_TRACE_LINES.match(line):
        score += 8.0
    if _WARN_PREFIX.search(line):
        score += 3.0
    if "FAIL" in line:
        score += 5.0
    return score


def dedup_logs(content: str) -> tuple[str, dict]:
    """Compress repetitive log output.

    Args:
        content: Raw log text.

    Returns:
        (compressed_text, stats_dict)
    """
    stats = {
        "original_chars": len(content),
        "original_lines": 0,
        "unique_patterns": 0,
        "compressed_lines": 0,
    }

    lines = content.split("\n")
    stats["original_lines"] = len(lines)

    # First pass: classify lines
    classified: list[tuple[str, float, bool, bool]] = []  # (normalized, score, is_error, is_stack)
    raw_by_norm: dict[str, list[tuple[int, str]]] = {}  # normalized → [(line_idx, raw_line)]

    for i, line in enumerate(lines):
        if not line.strip():
            classified.append(("", 0.0, False, False))
            continue
        norm = _normalize_for_dedup(line)
        score = _score_line(line)
        is_error = bool(_ERROR_PREFIX.search(line))
        is_stack = bool(_STACK_TRACE_LINES.match(line))
        classified.append((norm, score, is_error, is_stack))
        raw_by_norm.setdefault(norm, []).append((i, line))

    # Compute repetition counts
    pattern_counts = Counter()
    for norm, _, _, _ in classified:
        if norm:
            pattern_counts[norm] += 1

    stats["unique_patterns"] = len(pattern_counts)

    # Build compressed output
    output_lines: list[str] = []
    seen_patterns: set[str] = set()
    total_repetitions_saved = 0

    # Headline stats first
    if stats["original_lines"] > 20:
        output_lines.append(f"# {stats['original_lines']} lines, {stats['unique_patterns']} unique patterns")

    for i, (norm, score, is_error, is_stack) in enumerate(classified):
        raw_line = lines[i] if i < len(lines) else ""

        if not raw_line.strip():
            if output_lines and output_lines[-1] != "":
                output_lines.append("")
            continue

        if is_stack:
            # Stack traces always kept
            output_lines.append(raw_line)
            continue

        count = pattern_counts.get(norm, 1)
        if count >= config.LOG_MIN_REPETITIONS and norm not in seen_patterns:
            # Show first occurrence + count
            seen_patterns.add(norm)
            lines_for_norm = raw_by_norm.get(norm, [])
            first_raw = lines_for_norm[0][1] if lines_for_norm else raw_line
            last_raw = lines_for_norm[-1][1] if len(lines_for_norm) > 1 and config.LOG_KEEP_FIRST_LAST else None

            if last_raw and last_raw != first_raw:
                output_lines.append(f"[x{count}] {first_raw}")
                output_lines.append(f"[last]  {last_raw}")
            else:
                output_lines.append(f"[x{count}] {first_raw}")

            total_repetitions_saved += count - 1

        elif norm not in seen_patterns:
            # Unique or low-repeat pattern — show it
            seen_patterns.add(norm)
            output_lines.append(raw_line)

    # If output is still too long, take a summary
    if len(output_lines) > config.LOG_MAX_LINES_TOTAL:
        head = output_lines[:config.LOG_MAX_LINES_TOTAL // 2]
        tail = output_lines[-(config.LOG_MAX_LINES_TOTAL // 2):]
        dropped = len(output_lines) - config.LOG_MAX_LINES_TOTAL
        output_lines = head + [
            f"... {dropped} more lines truncated ..."
        ] + tail

    compressed = "\n".join(output_lines)

    # If output still large, try block-level dedup (multi-line log entries)
    if len(output_lines) > config.LOG_MAX_LINES_TOTAL * 2:
        block_compressed = _dedup_blocks(output_lines)
        if len(block_compressed) < len(compressed):
            output_lines = block_compressed
            compressed = "\n".join(output_lines)
            stats["block_dedup_applied"] = True

    stats["compressed_lines"] = len(output_lines)
    stats["repetitions_saved"] = total_repetitions_saved
    stats["compressed_chars"] = len(compressed)

    return compressed, stats


def _dedup_blocks(lines: list[str]) -> list[str]:
    """Dedup repeating multi-line blocks (e.g., repeated multi-line log entries).

    Groups consecutive non-empty lines by their first line's normalized form.
    If a block repeats >= 3 times, replaces with a single occurrence + count.
    """
    from collections import Counter
    output: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        # Find block boundaries (groups separated by blank lines or stack traces)
        block: list[str] = [lines[i]]
        j = i + 1
        while j < n and lines[j].strip() and not _STACK_TRACE_LINES.match(lines[j]):
            block.append(lines[j])
            j += 1

        if len(block) <= 1:
            output.append(lines[i])
            i += 1
            continue

        # Check if this block repeats
        block_key = tuple(_normalize_for_dedup(l) for l in block)
        count = 1
        k = j
        while k < n:
            next_block = []
            for offset in range(len(block)):
                if k + offset < n and lines[k + offset].strip():
                    next_block.append(lines[k + offset])
                else:
                    break
            if len(next_block) == len(block):
                next_key = tuple(_normalize_for_dedup(l) for l in next_block)
                if next_key == block_key:
                    count += 1
                    k += len(block)
                    # Skip blank lines between blocks
                    while k < n and not lines[k].strip():
                        k += 1
                else:
                    break
            else:
                break

        if count >= 3:
            output.append(f"[x{count}] {lines[i]}")
            for l in block[1:]:
                output.append(f" .  {l}")
            i = k
        else:
            output.extend(block)
            i = j

    return output
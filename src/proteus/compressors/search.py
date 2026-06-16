"""SearchResultCompressor — compress grep/ripgrep search results.

Strategy:
1. Parse file:line:content format
2. Group matches by file
3. Score each match by content (errors > keywords > rest)
4. Keep top N per file, always keep first and last
5. Cap total matches
"""

from __future__ import annotations

import re
from collections import defaultdict

from .. import config

# ── Search result patterns ──
_SEARCH_LINE = re.compile(r"^([^:]+):(\d+):(.+)$")
_SEARCH_SEP = re.compile(r"^--$")  # ripgrep file separator

# ── Importance keywords ──
_HIGH_SIGNAL = re.compile(
    r"(error|exception|fail|traceback|crash|fatal|timeout)", re.IGNORECASE
)
_MEDIUM_SIGNAL = re.compile(
    r"(warning|deprecated|TODO|FIXME|HACK|BUG|WORKAROUND)", re.IGNORECASE
)
_LOW_SIGNAL = re.compile(
    r"(info|debug|log|print|console|note)", re.IGNORECASE
)


def _score_match(content: str) -> float:
    """Score a search match by importance (higher = more important)."""
    if _HIGH_SIGNAL.search(content):
        return 10.0
    if _MEDIUM_SIGNAL.search(content):
        return 5.0
    if _LOW_SIGNAL.search(content):
        return 1.0
    return 2.0  # Default: slightly above lowest


def compress_search(
    content: str,
    max_per_file: int = 5,
    max_total: int = 30,
    max_files: int = 15,
) -> tuple[str, dict]:
    """Compress search/grep results.

    Args:
        content: Raw grep/ripgrep output.
        max_per_file: Max matches to show per file.
        max_total: Max total matches across all files.
        max_files: Max files to show.

    Returns:
        (compressed_text, stats_dict)
    """
    stats = {
        "original_chars": len(content),
        "mode": "search",
    }

    lines = content.split("\n")
    file_matches: dict[str, list[tuple[int, str, float]]] = defaultdict(list)
    current_file = "unknown"

    # Parse matches
    for i, line in enumerate(lines):
        if _SEARCH_SEP.match(line.strip()):
            continue
        m = _SEARCH_LINE.match(line)
        if m:
            fpath, lnum, match_text = m.group(1), int(m.group(2)), m.group(3)
            current_file = fpath
            score = _score_match(match_text)
            file_matches[current_file].append((lnum, match_text, score))
        elif line.strip() and line.startswith("   ") or line.startswith("\t"):
            # Context line
            if file_matches[current_file]:
                last = file_matches[current_file][-1]
                file_matches[current_file][-1] = (
                    last[0],
                    last[1] + "\n" + line.strip(),
                    last[2],
                )

    stats["original_files"] = len(file_matches)
    stats["original_matches"] = sum(len(v) for v in file_matches.values())

    # Select files (by highest total score)
    file_scores = {
        f: sum(s for _, _, s in matches) for f, matches in file_matches.items()
    }
    top_files = sorted(file_scores, key=file_scores.get, reverse=True)[:max_files]

    # Select matches per file
    result: list[str] = []
    total_selected = 0

    for fname in top_files:
        matches = file_matches[fname]
        # Sort by line number, then by score descending
        matches.sort(key=lambda m: (-m[2], m[0]))

        selected: list[tuple[int, str, float]] = []
        # Always keep first and last
        if len(matches) > max_per_file:
            # Keep highest-scored + first + last
            scored = sorted(matches, key=lambda m: -m[2])[:max_per_file - 2]
            selected = scored[:]
            # Ensure first and last are included
            first = matches[0]
            last = matches[-1]
            if first not in selected:
                selected.append(first)
            if last not in selected:
                selected.append(last)
        else:
            selected = matches

        # Sort back by line number for readability
        selected.sort(key=lambda m: m[0])

        # Add to output
        total_in_file = len(matches)
        result.append(f"# {fname} ({len(selected)}/{total_in_file} matches)")
        for lnum, text, _ in selected:
            result.append(f"  {lnum}:{text}")

        total_selected += len(selected)
        if total_selected >= max_total:
            remaining_files = len(top_files) - (top_files.index(fname) + 1)
            if remaining_files > 0:
                result.append(f"... {remaining_files} more files with matches ...")
            break

    compressed = "\n".join(result)

    stats["compressed_chars"] = len(compressed)
    stats["compressed_files"] = len(top_files)
    stats["compressed_matches"] = total_selected

    return compressed, stats
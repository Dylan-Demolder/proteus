"""SearchResultCompressor — compress grep/ripgrep search results.

Strategy:
1. Parse file:line:content format
2. Group matches by file
3. Score each match by content (errors > keywords > rest)
4. Boost matches whose shape is rare: in a search, every hit contains the
   search term, so what stands out is the odd one (`timeout = 4711` among
   300 `timeout = get_setting(...)`)
5. Keep top N per file, always keep first and last
6. Cap total matches, and summarize the hidden ones by shape so the model
   can see nothing unusual was left out
"""

from __future__ import annotations

import re
from collections import defaultdict

from .. import config

# ── Search result patterns ──
# The leading field must look like a file path: it contains a "/" or ends in
# an extension. Without that, "key: value" lines (YAML, `kubectl describe`,
# Markdown "Note: ..."), ISO timestamps ("2026-09-30T10:00:05" reads as
# file "2026-09-30T10", line 00) and log dates ("2026-09-30-...") all parse
# as grep hits, and the search compressor keeps only ~30 of them.
FILE_PATH = r"(?:[^\s:]*/[^\s:]*|[^\s:/]*\.[A-Za-z]\w{0,7})"
_SEARCH_LINE = re.compile(rf"^({FILE_PATH}):(\d+):(.*)$")  # file:line:content
_SEARCH_CONTEXT = re.compile(rf"^({FILE_PATH})-(\d+)-(.*)$")  # file-line-content (rg context)
_SEARCH_BINARY = re.compile(rf"^({FILE_PATH}):\s*(.+)$")  # file: content (no line number)
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


_QUOTED = re.compile(r"""(["'`]).*?\1""")
_NUMBER = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")

# A shape seen at most this many times counts as unusual.
RARE_SHAPE_MAX = 2
RARE_BOOST = 20.0


def _shape(text: str) -> str:
    """A match with its literals blanked, so near-identical hits group together."""
    first = text.split("\n", 1)[0]
    return _SPACE.sub(" ", _NUMBER.sub("N", _QUOTED.sub(r"\1…\1", first))).strip()


def compress_search(
    content: str,
    max_per_file: int | None = None,
    max_total: int | None = None,
    max_files: int | None = None,
) -> tuple[str, dict]:
    """Compress search/grep results.

    Args:
        content: Raw grep/ripgrep output.
        max_per_file: Max matches to show per file (default: config).
        max_total: Max total matches across all files (default: config).
        max_files: Max files to show (default: config).

    Returns:
        (compressed_text, stats_dict)
    """
    if max_per_file is None:
        max_per_file = config.SEARCH_MAX_PER_FILE
    if max_total is None:
        max_total = config.SEARCH_MAX_TOTAL
    if max_files is None:
        max_files = config.SEARCH_MAX_FILES

    stats = {
        "original_chars": len(content),
        "mode": "search",
    }

    lines = content.split("\n")
    file_matches: dict[str, list[tuple[int, str, float]]] = defaultdict(list)
    current_file = "unknown"

    # Parse matches
    for line in lines:
        if _SEARCH_SEP.match(line.strip()):
            continue
        m = _SEARCH_LINE.match(line)
        if m:
            fpath, lnum, match_text = m.group(1), int(m.group(2)), m.group(3)
            current_file = fpath
            score = _score_match(match_text)
            file_matches[current_file].append((lnum, match_text, score))
            continue

        # Try rg context-line format: file-N-content
        m = _SEARCH_CONTEXT.match(line)
        if m:
            fpath, lnum, match_text = m.group(1), int(m.group(2)), m.group(3)
            current_file = fpath
            score = _score_match(match_text) * 0.3  # Context lines: lower priority
            file_matches[current_file].append((lnum, match_text, score))
            continue

        # Try binary/header format: file: text
        m = _SEARCH_BINARY.match(line)
        if m and not m.group(1).endswith(".") and len(m.group(1)) > 2:
            fpath, match_text = m.group(1), m.group(2)
            current_file = fpath
            score = _score_match(match_text)
            file_matches[current_file].append((0, match_text, score))
            continue

        is_context_line = (line.strip() and line.startswith("   ")) or line.startswith("\t")
        if is_context_line and file_matches[current_file]:
            # Context line
            last = file_matches[current_file][-1]
            file_matches[current_file][-1] = (
                last[0],
                last[1] + "\n" + line.strip(),
                last[2],
            )

    original_files = len(file_matches)
    stats["original_files"] = original_files
    stats["original_matches"] = sum(len(v) for v in file_matches.values())

    if not stats["original_matches"]:
        # Nothing parsed as a search hit (wrong content type, e.g. forced by a
        # caller's hint). Selecting "top matches" from nothing would return an
        # empty string, so hand the input back unchanged instead.
        stats["compressed_chars"] = len(content)
        stats["compressed_files"] = 0
        stats["compressed_matches"] = 0
        return content, stats

    # Rare shapes are what a search result is usually looked at for.
    shapes: dict[str, str] = {}  # each distinct match's shape, computed once

    def shape_of(text: str) -> str:
        if text not in shapes:
            shapes[text] = _shape(text)
        return shapes[text]

    shape_counts: dict[str, int] = defaultdict(int)
    for matches in file_matches.values():
        for _, text, _ in matches:
            shape_counts[shape_of(text)] += 1
    for fname, matches in file_matches.items():
        file_matches[fname] = [
            (lnum, text, score + (RARE_BOOST if shape_counts[shape_of(text)] <= RARE_SHAPE_MAX else 0.0))
            for lnum, text, score in matches
        ]

    # Select files (by highest total score)
    # A file holding an unusual match outranks files full of ordinary ones.
    file_scores = {
        f: (max(s for _, _, s in matches), sum(s for _, _, s in matches)) for f, matches in file_matches.items()
    }
    top_files = sorted(file_scores, key=lambda f: file_scores[f], reverse=True)[:max_files]

    # Select matches per file
    result: list[str] = []
    total_selected = 0
    shown: set[tuple[str, int, str]] = set()
    files_shown = 0

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
            shown.add((fname, lnum, text))

        files_shown += 1
        total_selected += len(selected)
        if total_selected >= max_total:
            break

    hidden = [
        (fname, lnum, text)
        for fname, matches in file_matches.items()
        for lnum, text, _ in matches
        if (fname, lnum, text) not in shown
    ]
    rare_hidden = sum(shape_counts[shape_of(text)] <= RARE_SHAPE_MAX for _, _, text in hidden)
    if hidden:
        remaining_files = original_files - files_shown
        where = f", {remaining_files} more files with matches" if remaining_files > 0 else ""
        result.append(f"... {len(hidden)} more matches not shown{where} ...")
        hidden_shapes: dict[str, int] = defaultdict(int)
        for _, _, text in hidden:
            hidden_shapes[shape_of(text)] += 1
        common = sorted(hidden_shapes.items(), key=lambda kv: -kv[1])[:3]
        listed = sum(n for _, n in common)
        summary = "; ".join(f"{n}× `{shape[:100]}`" for shape, n in common)
        if listed < len(hidden):
            summary += f"; {len(hidden) - listed} others"
        result.append(f"[not shown, by shape (numbers as N, strings as '…'): {summary}]")

    compressed = "\n".join(result)

    stats["compressed_chars"] = len(compressed)
    stats["compressed_files"] = files_shown
    stats["compressed_matches"] = total_selected
    stats["rare_hidden"] = rare_hidden

    return compressed, stats

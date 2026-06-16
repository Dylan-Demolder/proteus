"""DiffCompressor — compress git diff output.

Strategy:
1. Parse unified diff format
2. Keep additions and deletions, compress context
3. Cap per-file hunks and total files
"""

from __future__ import annotations

import re

from .. import config

# ── Diff patterns ──
_DIFF_FILE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_HUNK = re.compile(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.+)$")
_DIFF_ADD = re.compile(r"^\+")
_DIFF_DEL = re.compile(r"^-")
_DIFF_CONTEXT = re.compile(r"^ ")
_DIFF_NO_NEWLINE = re.compile(r"^\\ No newline at end of file$")


def compress_diff(
    content: str,
    max_context_lines: int = 2,
    max_hunks_per_file: int = 10,
    max_files: int = 20,
) -> tuple[str, dict]:
    """Compress git diff output.

    Args:
        content: Unified diff output.
        max_context_lines: Max context lines to keep around changes.
        max_hunks_per_file: Max hunks to show per file.
        max_files: Max files to show.

    Returns:
        (compressed_diff, stats_dict)
    """
    stats = {
        "original_chars": len(content),
        "mode": "diff",
    }

    lines = content.split("\n")
    result: list[str] = []
    file_count = 0
    total_hunks = 0
    total_additions = 0
    total_deletions = 0

    in_hunk = False
    context_count = 0
    hunk_count = 0
    current_file = ""
    hunks_for_file = 0
    skip_file = False
    additions = 0
    deletions = 0

    for line in lines:
        dm = _DIFF_FILE.match(line)
        if dm:
            # New file
            if skip_file and hunks_for_file > 0:
                pass
            file_count += 1
            skip_file = file_count > max_files
            current_file = dm.group(2)
            hunks_for_file = 0
            additions = 0
            deletions = 0
            in_hunk = False
            result.append(f"diff --git a/{dm.group(1)} b/{dm.group(2)}")
            continue

        if skip_file:
            continue

        hm = _DIFF_HUNK.match(line)
        if hm:
            hunks_for_file += 1
            in_hunk = True
            context_count = 0
            if hunks_for_file <= max_hunks_per_file:
                total_hunks += 1
                result.append(line)
            continue

        if _DIFF_NO_NEWLINE.match(line):
            if hunks_for_file <= max_hunks_per_file:
                result.append(line)
            continue

        if in_hunk and hunks_for_file > max_hunks_per_file:
            # Still track stats but don't output
            if _DIFF_ADD.match(line):
                additions += 1
                total_additions += 1
            elif _DIFF_DEL.match(line):
                deletions += 1
                total_deletions += 1
            continue

        if in_hunk:
            if _DIFF_ADD.match(line):
                context_count = 0
                total_additions += 1
                additions += 1
                result.append(line)
            elif _DIFF_DEL.match(line):
                context_count = 0
                total_deletions += 1
                deletions += 1
                result.append(line)
            elif _DIFF_CONTEXT.match(line):
                context_count += 1
                if context_count <= max_context_lines:
                    result.append(line)
                else:
                    # Collapse consecutive context beyond max
                    pass
            else:
                # Metadata line (---/+++) — skip if context is collapsed
                if not line.startswith("---") and not line.startswith("+++"):
                    result.append(line)
        else:
            result.append(line)

    stats["files_affected"] = min(file_count, max_files)
    stats["additions"] = total_additions
    stats["deletions"] = total_deletions
    stats["hunks_kept"] = total_hunks
    stats["compressed_chars"] = len("\n".join(result))

    compressed = "\n".join(result)
    return compressed, stats
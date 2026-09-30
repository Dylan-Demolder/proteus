"""DiffCompressor — compress git diff output.

Strategy:
1. Parse unified diff format (git and plain `diff -u`, including `git show`
   and `git log -p` output with commit text between files)
2. Keep every addition and deletion; keep only the context lines closest
   to each change
3. Cap hunks per file and total files, and say what was left out
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import config

# ── Diff patterns ──
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

# How many omitted file names to list before summarising the rest as a count.
_MAX_OMITTED_NAMES = 20


@dataclass
class _File:
    name: str
    header: list[str] = field(default_factory=list)
    hunks: list[tuple[str, list[str]]] = field(default_factory=list)


def _changes(body: list[str]) -> tuple[int, int]:
    adds = sum(1 for line in body if line.startswith("+"))
    dels = sum(1 for line in body if line.startswith("-"))
    return adds, dels


def _parse(lines: list[str]) -> list[str | _File]:
    """Split a diff into an ordered list of plain lines and files.

    Hunk bodies are delimited by the line counts in their @@ header, so a
    removed line that happens to read "--- x" is not mistaken for the next
    file's header. Diff output that doesn't match its counts (hand-edited, or
    whitespace-stripped) falls back to "ends at the first non-diff line".
    """
    segments: list[str | _File] = []
    current: _File | None = None
    in_header = False
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]

        m = _HUNK.match(line)
        if m:
            if current is None:
                current = _File(name="?")
                segments.append(current)
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
            body: list[str] = []
            i += 1
            while i < n and (old_left > 0 or new_left > 0):
                body_line = lines[i]
                if body_line.startswith(" ") or body_line == "":
                    old_left -= 1
                    new_left -= 1
                elif body_line.startswith("-"):
                    old_left -= 1
                elif body_line.startswith("+"):
                    new_left -= 1
                elif not body_line.startswith("\\"):
                    break
                body.append(body_line)
                i += 1
            while i < n and lines[i].startswith("\\"):  # "\ No newline at end of file"
                body.append(lines[i])
                i += 1
            current.hunks.append((line, body))
            in_header = False
            continue

        git = _DIFF_GIT.match(line)
        plain = (
            not in_header
            and line.startswith("--- ")
            and i + 1 < n
            and lines[i + 1].startswith("+++ ")
        )
        if git or plain:
            if git:
                name = git.group(2)
            else:
                new_path = lines[i + 1][4:].split("\t")[0]
                old_path = line[4:].split("\t")[0]
                path = old_path if new_path == "/dev/null" else new_path
                name = path[2:] if path.startswith(("a/", "b/")) else path
            current = _File(name=name, header=[line])
            segments.append(current)
            in_header = True
            i += 1
            continue

        if in_header and current is not None:
            current.header.append(line)
        else:
            segments.append(line)
        i += 1

    return segments


def _trim_context(body: list[str], max_context_lines: int) -> list[str]:
    """Keep changed lines plus the context lines nearest to them."""
    changed = [i for i, line in enumerate(body) if line[:1] in ("+", "-")]
    if not changed:
        return body[:max_context_lines]
    kept: list[str] = []
    j = 0  # index into `changed` of the next change at or after i
    for i, line in enumerate(body):
        if line[:1] in ("+", "-", "\\"):
            kept.append(line)
            continue
        while j < len(changed) and changed[j] < i:
            j += 1
        dist_next = changed[j] - i if j < len(changed) else None
        dist_prev = i - changed[j - 1] if j > 0 else None
        nearest = min(d for d in (dist_next, dist_prev) if d is not None)
        if nearest <= max_context_lines:
            kept.append(line)
    return kept


def compress_diff(
    content: str,
    max_context_lines: int | None = None,
    max_hunks_per_file: int | None = None,
    max_files: int | None = None,
) -> tuple[str, dict]:
    """Compress git diff output.

    Args:
        content: Unified diff output.
        max_context_lines: Context lines to keep on each side of a change (default: config).
        max_hunks_per_file: Max hunks to show per file (default: config).
        max_files: Max files to show (default: config).

    Returns:
        (compressed_diff, stats_dict)
    """
    if max_context_lines is None:
        max_context_lines = config.DIFF_MAX_CONTEXT_LINES
    if max_hunks_per_file is None:
        max_hunks_per_file = config.DIFF_MAX_HUNKS_PER_FILE
    if max_files is None:
        max_files = config.DIFF_MAX_FILES
    stats = {
        "original_chars": len(content),
        "mode": "diff",
    }

    result: list[str] = []
    file_count = 0
    total_hunks = 0
    total_additions = 0
    total_deletions = 0
    omitted_files: list[tuple[str, int, int]] = []

    for segment in _parse(content.split("\n")):
        if isinstance(segment, str):
            result.append(segment)
            continue

        file_count += 1
        file_adds = file_dels = 0
        for _, body in segment.hunks:
            adds, dels = _changes(body)
            file_adds += adds
            file_dels += dels
        total_additions += file_adds
        total_deletions += file_dels

        if file_count > max_files:
            omitted_files.append((segment.name, file_adds, file_dels))
            continue

        result.extend(segment.header)
        for hunk_header, body in segment.hunks[:max_hunks_per_file]:
            result.append(hunk_header)
            result.extend(_trim_context(body, max_context_lines))
            total_hunks += 1
        extra = segment.hunks[max_hunks_per_file:]
        if extra:
            adds = sum(_changes(body)[0] for _, body in extra)
            dels = sum(_changes(body)[1] for _, body in extra)
            result.append(f"... {len(extra)} more hunks in {segment.name} omitted (+{adds} -{dels})")

    if omitted_files:
        adds = sum(a for _, a, _ in omitted_files)
        dels = sum(d for _, _, d in omitted_files)
        names = ", ".join(name for name, _, _ in omitted_files[:_MAX_OMITTED_NAMES])
        if len(omitted_files) > _MAX_OMITTED_NAMES:
            names += f", and {len(omitted_files) - _MAX_OMITTED_NAMES} more"
        result.append(f"... {len(omitted_files)} more files omitted (+{adds} -{dels}): {names}")

    compressed = "\n".join(result)
    stats["files_affected"] = min(file_count, max_files)
    stats["additions"] = total_additions
    stats["deletions"] = total_deletions
    stats["hunks_kept"] = total_hunks
    stats["compressed_chars"] = len(compressed)
    return compressed, stats

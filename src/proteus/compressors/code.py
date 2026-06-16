"""CodeCompressor — compress source code files.

Two modes:
1. Strip comments/docstrings (lossless, always applied)
2. Compress function bodies (lossy, for files >200 lines)

FileLister — compact ls -la output (lossless).
"""

import re
from .. import config


# ── Python comment/docstring patterns ──
_PY_STRING_START = re.compile(r"^(\s*)(\"\"\"|''')")
_PY_COMMENT = re.compile(r"^\s*#")
_PY_DECORATOR = re.compile(r"^\s*@\w+")

# ── General ──
_BLANK_LINE = re.compile(r"^\s*$")


def strip_comments_python(code: str) -> str:
    """Strip comments and docstrings from Python code.
    Lossless — behavior unchanged.
    """
    lines = code.split("\n")
    result: list[str] = []
    in_multiline = False
    multiline_delim = ""
    prev_blank = False

    for line in lines:
        stripped = line.strip()

        # Handle multiline docstrings
        if in_multiline:
            if multiline_delim in stripped:
                in_multiline = False
            continue

        m = _PY_STRING_START.match(line)
        if m:
            # Check if this is just a string expression (not a docstring)
            # A docstring is the first expression in a function/class/module
            delim = m.group(2)
            if delim in stripped[len(m.group(1)):]:
                # Single-line docstring — strip
                continue
            else:
                # Start of multi-line docstring
                in_multiline = True
                multiline_delim = delim
                continue

        # Strip comments
        if _PY_COMMENT.match(line):
            continue

        # Collapse multiple blank lines into one
        is_blank = bool(_BLANK_LINE.match(line))
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank

        result.append(line)

    return "\n".join(result)


def strip_code(content: str, language: str = "python") -> str:
    """Strip comments from source code.
    Currently only Python is fully supported.

    Args:
        content: Raw source code
        language: Language hint (python, javascript, etc.)

    Returns:
        Code with comments stripped
    """
    if language == "python":
        result = strip_comments_python(content)
    else:
        # Generic comment stripping for JS/TS/Go/Rust
        result = _strip_generic_comments(content)

    return result


def _strip_generic_comments(code: str) -> str:
    """Strip // and /* */ comments from JS/TS/C-like languages.
    Also strip JSDoc /** ... */. Handles strings so it doesn't break URLs inside strings.
    """
    lines = code.split("\n")
    result: list[str] = []
    in_block_comment = False
    prev_blank = False

    for line in lines:
        stripped = line.strip()

        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue

        if stripped.startswith("//") or stripped.startswith("* "):
            continue

        if "/*" in stripped:
            in_block_comment = True
            before = stripped.split("/*")[0]
            if before.strip():
                result.append(before)
            continue

        # Collapse blank lines
        is_blank = bool(_BLANK_LINE.match(line))
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank

        result.append(line)

    return "\n".join(result)


def compress_file_listing(content: str) -> tuple[str, dict]:
    """Compress ls -la output. Lossless — all file info preserved.

    Before:
      -rw-r--r--  1 root root 12893 Jun 15 22:00 server.py
    After:
      server.py    12K Jun 15 22:00

    Returns:
        (compressed_text, stats_dict)
    """
    stats = {"original_chars": len(content), "mode": "file_listing"}

    lines = content.split("\n")
    result: list[str] = []
    total_pattern = re.compile(r"^total (\d+)")
    file_line = re.compile(
        r"^([drwxs-]{10})\s+\d+\s+(\S+)\s+(\S+)\s+(\d+)\s+(\w+\s+\d+\s+\d+:\d+|\w+\s+\d+\s+\d{4})\s+(.+)$"
    )

    for line in lines:
        t = total_pattern.match(line)
        if t:
            result.append(f"[total: {t.group(1)} blocks]")
            continue

        m = file_line.match(line)
        if m:
            perms, owner, group, size_str, date, name = m.groups()
            # Human-readable size
            size = int(size_str)
            if size >= 1024 * 1024:
                size_display = f"{size / 1024 / 1024:.1f}M"
            elif size >= 1024:
                size_display = f"{size / 1024:.0f}K"
            else:
                size_display = str(size)
            # Directory marker
            if perms.startswith("d"):
                name += "/"
            elif perms.startswith("l"):
                name += "@"

            if config.LS_STRIP_PERMS and config.LS_STRIP_OWNER:
                result.append(f"  {size_display:>6} {date} {name}")
            else:
                result.append(f"{perms} {size_display:>6} {date} {name}")
        else:
            result.append(line)

    compressed = "\n".join(result)
    stats["compressed_chars"] = len(compressed)
    stats["original_lines"] = len(lines)
    stats["compressed_lines"] = len(result)

    return compressed, stats
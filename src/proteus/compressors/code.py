"""CodeCompressor — compress source code files.

Two modes:
1. Strip comments/docstrings (always applied; code behaviour is unchanged)
2. Compress function bodies (lossy, for files >200 lines)

FileLister — compact ls -la output (lossless).
"""

import io
import re
import tokenize

from .. import config

# ── General ──
_BLANK_LINE = re.compile(r"^\s*$")

# Comments that change how the file builds or type-checks. Stripping these
# would alter behaviour, so they are kept even though they look like comments.
_KEEP_DIRECTIVE = re.compile(r"^\s*(//go:|// \+build|///\s*<reference|//\s*@ts-)")

# A Rust/Go character literal: 'x', '\n', '\u{1F600}'. Anything else starting
# with an apostrophe in those languages is a Rust lifetime ('a), not a string.
_CHAR_LITERAL = re.compile(r"'(?:\\[^']*|[^'\\])'")

_SKIP_TOKENS = (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING)
_BLOCK_START = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)


def _collapse_blank_lines(lines: list[str], protected: set[int]) -> str:
    """Collapse runs of blank lines to one, leaving protected lines untouched."""
    result: list[str] = []
    prev_blank = False
    for i, line in enumerate(lines):
        if i in protected:
            result.append(line)
            prev_blank = False
            continue
        is_blank = bool(_BLANK_LINE.match(line))
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank
        result.append(line)
    return "\n".join(result)


def strip_comments_python(code: str) -> str:
    """Strip full-line comments and docstrings from Python code.

    Uses the tokenizer rather than line regexes, so string literals that merely
    *look* like docstrings or comments (SQL in a triple-quoted string, a ``#``
    inside text) are left alone. A docstring is a triple-quoted string that is
    a statement on its own; if removing it would leave a block empty, it is
    replaced with ``...`` so the code still compiles.

    Tool output is often a fragment (``head -n 200 file.py``). If tokenizing
    stops early, everything from that point on is passed through verbatim.
    """
    lines = code.split("\n")
    tokens: list[tokenize.TokenInfo] = []
    verbatim_from = len(lines)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            tokens.append(tok)
    except (tokenize.TokenError, SyntaxError):
        verbatim_from = tokens[-1].end[0] if tokens else 0

    drop: set[int] = set()        # 0-based line indices to remove
    replace: dict[int, str] = {}  # 0-based line index -> replacement text
    protected: set[int] = set(range(verbatim_from, len(lines)))

    significant = [t for t in tokens if t.type not in _SKIP_TOKENS]
    for i, tok in enumerate(significant):
        if tok.type != tokenize.STRING:
            continue
        first_row, last_row = tok.start[0] - 1, tok.end[0] - 1
        body = tok.string.lstrip("rRuUbBfF")
        prev = significant[i - 1] if i > 0 else None
        nxt = significant[i + 1] if i + 1 < len(significant) else None
        is_docstring = (
            body[:3] in ('"""', "'''")
            and (prev is None or prev.type in _BLOCK_START)
            and nxt is not None
            and nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER)
        )
        if not is_docstring:
            # Lines inside a multi-line string are data: never touch them.
            protected.update(range(first_row + 1, last_row + 1))
            continue
        after = significant[i + 2] if i + 2 < len(significant) else None
        block_would_be_empty = (
            prev is not None
            and prev.type == tokenize.INDENT
            and (after is None or after.type in (tokenize.DEDENT, tokenize.ENDMARKER))
        )
        drop.update(range(first_row, last_row + 1))
        if block_would_be_empty:
            drop.discard(first_row)
            replace[first_row] = " " * tok.start[1] + "..."

    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        row = tok.start[0] - 1
        if row == 0 and tok.string.startswith("#!"):
            continue  # shebang
        if tok.line.strip().startswith("#"):
            drop.add(row)

    kept: list[str] = []
    kept_protected: set[int] = set()
    for i, line in enumerate(lines):
        if i in drop and i not in protected:
            continue
        if i in protected:
            kept_protected.add(len(kept))
        kept.append(replace.get(i, line))
    return _collapse_blank_lines(kept, kept_protected)


def strip_code(content: str, language: str = "python") -> str:
    """Strip comments from source code.

    Args:
        content: Raw source code
        language: "python", or a C-family language ("javascript", "typescript",
            "go", "rust"); anything else is treated as generic C-family.

    Returns:
        Code with comments stripped
    """
    if language == "python":
        return strip_comments_python(content)
    return _strip_generic_comments(content, char_literals=language in ("go", "rust"))


def _strip_generic_comments(code: str, char_literals: bool = False) -> str:
    """Strip // and /* */ comments from JS/TS/Go/Rust and other C-like code.

    Tracks string literals ('...', "...", and multi-line `...`) so comment
    markers inside strings are left alone: URLs ("https://x") and globs
    ("src/**/*.ts") used to be treated as comments and silently deleted code.

    The failure mode is deliberately one-sided. If the scanner gets confused,
    it may miss a comment, but it never removes code. If a block comment is
    never closed, the input was misparsed, so it is returned unchanged.

    Args:
        code: Source code.
        char_literals: Treat an apostrophe as a one-character literal
            ('x', '\\n') instead of a string (Go runes, Rust chars and lifetimes).
    """
    lines = code.split("\n")
    kept: list[str] = []
    protected: set[int] = set()
    in_block = False
    quote: str | None = None

    for line in lines:
        if quote == "`":
            protected.add(len(kept))  # inside a multi-line template/raw string
        else:
            quote = None  # '...' and "..." never span lines
        if not in_block and quote is None and _KEEP_DIRECTIVE.match(line):
            kept.append(line)
            continue

        buf: list[str] = []
        had_comment = in_block
        i, n = 0, len(line)
        while i < n:
            if in_block:
                end = line.find("*/", i)
                if end == -1:
                    break
                in_block = False
                i = end + 2
                continue
            ch = line[i]
            if quote:
                if ch == "\\" and i + 1 < n:
                    buf.append(line[i:i + 2])
                    i += 2
                    continue
                buf.append(ch)
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch == "'" and char_literals:
                m = _CHAR_LITERAL.match(line, i)
                token = m.group(0) if m else ch  # no match: a Rust lifetime
                buf.append(token)
                i += len(token)
                continue
            if ch in "'\"`":
                quote = ch
                buf.append(ch)
                i += 1
                continue
            if line.startswith("//", i):
                had_comment = True
                break
            if line.startswith("/*", i):
                had_comment = True
                in_block = True
                i += 2
                continue
            buf.append(ch)
            i += 1

        text = "".join(buf)
        if had_comment:
            text = text.rstrip()
            if not text.strip():
                continue  # the line held nothing but comment
        kept.append(text)

    if in_block:
        return code
    return _collapse_blank_lines(kept, protected)


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

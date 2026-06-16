"""ContentRouter — detects content type and routes to the right compressor."""

import json
import re
from enum import Enum

from . import config


class ContentType(Enum):
    JSON = "json"
    CODE_PYTHON = "code_python"
    CODE_JS = "code_javascript"
    CODE_TS = "code_typescript"
    CODE_GO = "code_go"
    CODE_RUST = "code_rust"
    CODE_GENERIC = "code_generic"
    LOGS = "logs"
    SEARCH_RESULTS = "search_results"
    BUILD_OUTPUT = "build_output"
    FILE_LISTING = "file_listing"
    DIFF = "diff"
    TEXT = "text"


# ── Detection patterns ──

_CODE_PATTERNS: dict[ContentType, list[re.Pattern]] = {
    ContentType.CODE_PYTHON: [
        re.compile(r"^\s*(def |class |import |from |async def |@\w+)"),
    ],
    ContentType.CODE_JS: [
        re.compile(r"^\s*(function |const |let |var |import |export |=>)"),
    ],
    ContentType.CODE_TS: [
        re.compile(r"^\s*(interface |type |enum |namespace )"),
        re.compile(r":\s*(string|number|boolean|any)\b"),
    ],
    ContentType.CODE_GO: [
        re.compile(r"^\s*(func |type |package |import )"),
    ],
    ContentType.CODE_RUST: [
        re.compile(r"^\s*(fn |struct |enum |impl |mod |use |pub )"),
        re.compile(r"^\s*#\["),
    ],
}

_SEARCH_RESULT = re.compile(r"^[^\s:]+:\d+:")
_DIFF_HEADER = re.compile(
    r"^(diff --git |diff --combined |diff --cc |--- a/|@@\s+-\d+,\d+\s+\+\d+,\d+\s+@@)"
)
_LOG_LINE = re.compile(r"(\[INFO\]|\[ERROR\]|\[WARN\]|\[DEBUG\]|\[TRACE\]|\[FATAL\]|\[CRITICAL\]|\d{4}-\d{2}-\d{2}T|\d{2}:\d{2}:\d{2})")
_BUILD_LINE = re.compile(r"(error:|warning:|FAILED|passed|failed|✓|✘|×|Error:|Warning:)", re.IGNORECASE)
_LS_LINE = re.compile(r"^[drwxs-]{10}\s+\d+\s+\S+\s+\S+\s+")
_LS_TOTAL = re.compile(r"^total \d+")


def detect_content_type(content: str) -> ContentType:
    """Detect the content type of a tool output string.

    Uses rule-based heuristics (regex), ordered by specificity.
    Falls back to TEXT.
    """
    if not content or len(content) < 16:
        return ContentType.TEXT

    first_lines = content.split("\n")[:20]
    first_nonempty = ""
    for line in first_lines:
        stripped = line.strip()
        if stripped:
            first_nonempty = stripped
            break

    # 1. Try JSON parse
    try:
        parsed = json.loads(content)
        if isinstance(parsed, (dict, list)):
            return ContentType.JSON
    except (json.JSONDecodeError, ValueError):
        pass

    # Check first few lines against specific patterns
    hit_counts: dict[ContentType, int] = {}

    # Check for ls output (first line has patterns)
    for line in first_lines[:3]:
        if _LS_TOTAL.match(line) or _LS_LINE.match(line):
            hit_counts[ContentType.FILE_LISTING] = hit_counts.get(ContentType.FILE_LISTING, 0) + 1

    # Check for diff
    for line in first_lines[:10]:
        if _DIFF_HEADER.match(line):
            hit_counts[ContentType.DIFF] = hit_counts.get(ContentType.DIFF, 0) + 1

    # Check for code patterns
    for ctype, patterns in _CODE_PATTERNS.items():
        for pattern in patterns:
            for line in first_lines[:15]:
                if pattern.search(line):
                    hit_counts[ctype] = hit_counts.get(ctype, 0) + 1

    # Check for search results (file:line: format)
    search_hits = 0
    for line in first_lines[:20]:
        if _SEARCH_RESULT.match(line):
            search_hits += 1
    if search_hits >= 3:
        hit_counts[ContentType.SEARCH_RESULTS] = search_hits

    # Check for logs
    log_hits = 0
    for line in first_lines[:20]:
        if _LOG_LINE.search(line):
            log_hits += 1
    if log_hits >= 3:
        hit_counts[ContentType.LOGS] = log_hits

    # Check for build output
    build_hits = 0
    for line in first_lines[:20]:
        if _BUILD_LINE.search(line):
            build_hits += 1
    if build_hits >= 3:
        hit_counts[ContentType.BUILD_OUTPUT] = build_hits

    # Pick the type with the most hits
    if hit_counts:
        best = max(hit_counts, key=hit_counts.get)  # type: ignore
        return best

    return ContentType.TEXT


def estimate_chars_to_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/prose, ~3 for code."""
    return len(text) // 4


def should_compress(content: str) -> bool:
    """Return True if content is large enough to warrant compression."""
    return len(content) > config.MIN_COMPRESS_CHARS
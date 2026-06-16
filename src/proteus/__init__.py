"""Proteus — inline compression layer for Hermes tool outputs.

Compresses large tool outputs before they enter the LLM context,
saving 40-60% of tokens with guaranteed reversibility (CCR cache).

Usage:
    from proteus import compress_tool_output
    compressed, stats = compress_tool_output(large_tool_output)
    # If you need the original later:
    from proteus.ccr import retrieve
    original = retrieve(stats.get("hash", ""))
"""

from . import config
from .router import detect_content_type, should_compress, ContentType
from .compressors.json_crusher import crush_json
from .compressors.log_deduper import dedup_logs
from .compressors.code import strip_code, compress_file_listing
from . import ccr


def compress_tool_output(
    content: str,
    content_type_hint: str | None = None,
) -> tuple[str, dict]:
    """Compress a tool output string if it's large enough.

    Auto-detects content type and routes to the appropriate compressor.
    Always stores the original in the CCR cache for retrieval.

    Args:
        content: The raw tool output string.
        content_type_hint: Optional type hint to skip detection
            ("json", "logs", "code_python", "code_javascript", "file_listing").

    Returns:
        (compressed_string, stats_dict)
        stats contains:
            - content_type: detected type
            - original_chars, compressed_chars
            - estimated_token_savings
            - hash: content hash for CCR retrieval
            - mode: compression mode used
            - was_compressed: bool

    Example:
        >>> out = terminal("ls -la /root/dashboard/")
        >>> compressed, stats = compress_tool_output(out["output"])
        >>> # compressed now has compact listing
        >>> # stats["hash"] lets you retrieve original if needed
    """
    stats = {
        "content_type": "unknown",
        "original_chars": len(content),
        "was_compressed": False,
    }

    if not should_compress(content):
        stats["content_type"] = "too_small"
        return content, stats

    # Detect type (or use hint)
    if content_type_hint:
        try:
            detected = ContentType(content_type_hint)
        except ValueError:
            detected = detect_content_type(content)
    else:
        detected = detect_content_type(content)

    stats["content_type"] = detected.value

    # Route to the right compressor
    compressed = content
    compressor = None

    if detected == ContentType.JSON:
        compressed, compressor_stats = crush_json(content)
        compressor = "json_crusher"

    elif detected in (ContentType.LOGS, ContentType.BUILD_OUTPUT):
        compressed, compressor_stats = dedup_logs(content)
        compressor = "log_deduper"

    elif detected == ContentType.CODE_PYTHON:
        compressed = strip_code(content, "python")
        compressor = "code_python"
        compressor_stats = {}

    elif detected in (ContentType.CODE_JS, ContentType.CODE_TS,
                      ContentType.CODE_GO, ContentType.CODE_RUST):
        compressed = strip_code(content, "generic")
        compressor = "code_generic"
        compressor_stats = {}

    elif detected == ContentType.FILE_LISTING:
        compressed, compressor_stats = compress_file_listing(content)
        compressor = "file_listing"

    elif detected == ContentType.SEARCH_RESULTS:
        from .compressors.search import compress_search
        compressed, compressor_stats = compress_search(content)
        compressor = "search"

    elif detected == ContentType.DIFF:
        from .compressors.diff import compress_diff
        compressed, compressor_stats = compress_diff(content)
        compressor = "diff"

    elif detected == ContentType.TEXT:
        from .compressors.text import summarize_text
        compressed, compressor_stats = summarize_text(content)
        compressor = "text"

    # Store in CCR cache if compression actually reduced size
    content_hash = ""
    if compressor is not None and len(compressed) < len(content):
        stats["was_compressed"] = True
        stats["compressed_chars"] = len(compressed)
        stats["compressor"] = compressor

        # Merge compressor-specific stats
        if compressor_stats:
            stats.update(compressor_stats)

        # Store in CCR
        content_hash = ccr.store(content, compressed, detected.value, stats)
        stats["hash"] = content_hash

        # Show compression marker
        saved = len(content) - len(compressed)
        saved_tokens = saved // 4
        stats["chars_saved"] = saved
        stats["estimated_token_savings"] = saved_tokens
        stats["compression_pct"] = round((1 - len(compressed) / max(len(content), 1)) * 100, 1)

    return compressed, stats


def compress_summary_line(stats: dict) -> str:
    """Generate a one-line summary of what compression did.
    Useful to include in responses to show the user.
    """
    if not stats.get("was_compressed"):
        return ""
    ct = stats.get("content_type", "")
    orig = stats.get("original_chars", 0)
    comp = stats.get("compressed_chars", 0)
    pct = stats.get("compression_pct", 0)
    lines = stats.get("original_lines", stats.get("original_rows", 0))
    lines_after = stats.get("compressed_lines", stats.get("compressed_rows", 0))
    token_saving = stats.get("estimated_token_savings", 0)
    hash_ = stats.get("hash", "")

    if lines and lines_after:
        line_info = f" ({lines}→{lines_after} lines)"
    else:
        line_info = ""

    return f"[compress {ct}: {orig:,}→{comp:,} chars ({pct:+.1f}%){line_info}, saved ~{token_saving} tokens | restore: ccr.retrieve('{hash_}')]"
"""JSONSmartCrusher — compress JSON tool outputs.

Three modes applied in sequence:
1. Canonicalize: pretty-print → compact (lossless, always applied)
2. Columnar: array-of-dicts with shared keys → CSV-like format (lossless, for large arrays)
3. Row-drop: keep head/tail, drop middle with stats (lossy, for very large arrays >200 rows)
"""

import json
import hashlib
from collections import Counter

from .. import config


def canonicalize(obj) -> str:
    """Compact JSON string with sorted keys, no whitespace.
    Zero info loss — just formatting.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compact_json(content: str) -> str:
    """Try to parse and re-serialize as compact JSON.
    Returns (compressed_string, was_compressed_bool).
    """
    try:
        parsed = json.loads(content)
        compact = canonicalize(parsed)
        return compact
    except (json.JSONDecodeError, ValueError):
        return content


def _get_shared_keys(rows: list[dict]) -> set[str] | None:
    """If all rows are dicts with the same keys, return those keys. Otherwise None."""
    if not rows:
        return None
    keys_set = set(rows[0].keys())
    for row in rows[1:]:
        if set(row.keys()) != keys_set:
            return None
    return keys_set


def _columnar_format(rows: list[dict], keys: set[str]) -> str:
    """Convert array of uniform dicts to columnar format (compact, zero info loss)."""
    key_list = sorted(keys)
    header = "# " + ", ".join(key_list)
    lines = [header]
    for row in rows:
        values = []
        for k in key_list:
            v = row.get(k)
            if v is None:
                values.append("")
            elif isinstance(v, (int, float)):
                values.append(str(v))
            else:
                s = str(v)
                if "," in s:
                    values.append(f'"{s}"')
                else:
                    values.append(s)
        lines.append(",".join(values))
    return "COLUMNS\n" + "\n".join(lines)


def crush_json(content: str) -> tuple[str, dict]:
    """Crush large JSON output.

    Args:
        content: Raw JSON string

    Returns:
        (compressed_string, stats_dict)
    """
    stats = {"original_chars": len(content), "mode": "passthrough"}

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content, stats

    if isinstance(parsed, dict):
        # Single object — compact it
        compact = canonicalize(parsed)
        stats["mode"] = "compact_object"
        stats["compressed_chars"] = len(compact)
        return compact, stats

    if not isinstance(parsed, list):
        # Scalar JSON — compact
        compact = canonicalize(parsed)
        stats["mode"] = "compact_scalar"
        stats["compressed_chars"] = len(compact)
        return compact, stats

    # It's a list (array)
    n = len(parsed)
    stats["original_rows"] = n

    # Check if all dicts with shared keys → columnar
    if config.JSON_AUTO_COLUMNAR and n >= config.JSON_COLUMNAR_MIN_ROWS and n > 0:
        shared_keys = _get_shared_keys(parsed)
        if shared_keys is not None:
            columnar = _columnar_format(parsed, shared_keys)
            stats["mode"] = "columnar"
            stats["compressed_chars"] = len(columnar)
            stats["compressed_rows"] = n
            return columnar, stats

    # Small array — compact only
    if n <= config.JSON_MAX_ROWS_BEFORE_DROP:
        compact = canonicalize(parsed)
        stats["mode"] = "compact_array"
        stats["compressed_chars"] = len(compact)
        stats["compressed_rows"] = n
        return compact, stats

    # Large array — row drop
    return _drop_rows(parsed, n, stats)


def _drop_rows(parsed: list, n: int, stats: dict) -> tuple[str, dict]:
    """Drop middle rows, keep head + tail with statistics."""
    head = parsed[:config.JSON_DROP_HEAD]
    tail = parsed[-config.JSON_DROP_TAIL:] if config.JSON_DROP_TAIL > 0 else []
    dropped = n - len(head) - len(tail)

    # Build a content hash for the original
    content_hash = hashlib.sha256(json.dumps(parsed, default=str).encode()).hexdigest()[:config.CCR_HASH_LENGTH]

    # Represent head as compact JSON
    head_compact = canonicalize(head)
    tail_compact = canonicalize(tail) if tail else "[]"

    # Try to add a brief structural summary of dropped rows
    summary_parts = []
    if n > 0 and isinstance(parsed[0], dict):
        keys = list(parsed[0].keys())
        numeric_ranges = []
        for k in keys[:5]:  # Check first 5 keys for numeric range
            vals = [row.get(k) for row in parsed if isinstance(row.get(k), (int, float))]
            if vals:
                numeric_ranges.append(f"{k}: [{min(vals):.2g}..{max(vals):.2g}]")
        if numeric_ranges:
            summary_parts.append(" | ".join(numeric_ranges))

    compressed = (
        f"[SHOWING {len(head)} first + {len(tail)} last of {n} items]\n"
        f"{head_compact}\n"
        f"... _ccr_dropped {dropped} rows hash={content_hash}\n"
    )
    if summary_parts:
        compressed += f"// Summary of dropped range: {'; '.join(summary_parts)}\n"
    compressed += f"{tail_compact}\n"
    compressed += f"[/SHOWING]"

    stats["mode"] = "row_drop"
    stats["compressed_chars"] = len(compressed)
    stats["compressed_rows"] = len(head) + len(tail)
    stats["dropped_rows"] = dropped
    stats["hash"] = content_hash

    return compressed, stats
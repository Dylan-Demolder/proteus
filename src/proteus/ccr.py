"""CCR Cache — Compress-Cache-Retrieve local store.

Stores original content indexed by hash so the LLM can retrieve
uncompressed originals when needed.
"""

import hashlib
import json
import os
import time
from pathlib import Path

from . import config


def _cache_dir() -> Path:
    path = Path(config.CCR_CACHE_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:config.CCR_HASH_LENGTH]


def store(original: str, compressed: str, content_type: str, stats: dict) -> str:
    """Store original content in CCR cache.

    Args:
        original: The full original content before compression
        compressed: The compressed version
        content_type: Detected type (json, logs, code, etc.)
        stats: Compression statistics

    Returns:
        content_hash: Short hash key that can be used in markers
    """
    content_hash = _hash_content(original)
    cache_dir = _cache_dir()

    entry = {
        "original": original,
        "compressed": compressed,
        "metadata": {
            "content_type": content_type,
            "original_chars": len(original),
            "compressed_chars": len(compressed),
            "original_lines": stats.get("original_lines", stats.get("original_rows", 0)),
            "compressed_lines": stats.get("compressed_lines", stats.get("compressed_rows", 0)),
            "compression_pct": round((1 - len(compressed) / max(len(original), 1)) * 100, 1),
            "compressed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    }

    cache_path = cache_dir / f"{content_hash}.json"
    with open(cache_path, "w") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    _maybe_evict(cache_dir)

    return content_hash


def retrieve(content_hash: str) -> str | None:
    """Retrieve original content by hash.

    Args:
        content_hash: The short hash (12 chars)

    Returns:
        Original content string, or None if not found
    """
    cache_dir = _cache_dir()
    cache_path = cache_dir / f"{content_hash}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            entry = json.load(f)
        return entry.get("original")
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def retrieve_compressed(content_hash: str) -> str | None:
    """Retrieve the compressed version by hash (for inspection)."""
    cache_dir = _cache_dir()
    cache_path = cache_dir / f"{content_hash}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            entry = json.load(f)
        return entry.get("compressed")
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _maybe_evict(cache_dir: Path):
    """LRU eviction: if over CCR_MAX_ENTRIES, remove oldest files.

    Entries are collected defensively: another process (or a concurrent proxy
    request) may unlink a file between glob() and stat(), and getmtime() on a
    vanished path raises FileNotFoundError. Skip anything that disappears.
    """
    entries: list[tuple[float, Path]] = []
    for path in cache_dir.glob("*.json"):
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            # Vanished (or unreadable) — another worker already evicted it.
            continue
    entries.sort(key=lambda item: item[0])

    while len(entries) > config.CCR_MAX_ENTRIES:
        _oldest_mtime, oldest = entries[0]
        try:
            oldest.unlink()
        except OSError:
            # Already gone — not an error, we just wanted it removed.
            pass
        entries = entries[1:]


def stats() -> dict:
    """Get CCR cache statistics."""
    cache_dir = _cache_dir()
    total_size = 0
    count = 0
    for path in cache_dir.glob("*.json"):
        try:
            total_size += path.stat().st_size
            count += 1
        except OSError:
            # Evicted by a concurrent worker between glob() and stat().
            continue

    return {
        "entries": count,
        "max_entries": config.CCR_MAX_ENTRIES,
        "total_size_bytes": total_size,
        "cache_dir": str(cache_dir),
    }


def clear():
    """Clear all cached content."""
    cache_dir = _cache_dir()
    for f in cache_dir.glob("*.json"):
        f.unlink()

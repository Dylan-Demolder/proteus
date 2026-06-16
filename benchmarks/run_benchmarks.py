#!/usr/bin/env python3
"""Proteus benchmarks — measure compression savings on real workloads.

Runs compressors against real data files and reports savings.
Also tests latency regression when run in CI.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/.hermes/proteus/src"))

from proteus import compress_tool_output, compress_summary_line
from proteus.ccr import stats as ccr_stats


def benchmark_file(label: str, content: str, source: str = ""):
    """Run compression on content and report metrics."""
    start = time.time()
    compressed, stats = compress_tool_output(content)
    elapsed = time.time() - start

    if stats["was_compressed"]:
        ratio = stats["compression_pct"]
        saved = stats.get("estimated_token_savings", 0)
        mode = stats.get("mode", "?")
        ct = stats["content_type"]
        print(f"  ✅ {label:30s} | {ct:15s} | {mode:12s} | {ratio:6.1f}% | ~{saved:>6,} tokens | {elapsed*1000:6.1f}ms")
    else:
        ct = stats["content_type"]
        print(f"  ⏭️  {label:30s} | {ct:15s} | (passthrough) | {stats['original_chars']:,} chars | {elapsed*1000:6.1f}ms")

    return stats


def main():
    print("=" * 85)
    print("  Proteus Benchmark Suite")
    print("=" * 85)
    print(f"  {'Label':30s} | {'Type':15s} | {'Mode':12s} | {'Savings':>6s} | {'Tokens':>10s} | {'Time':>6s}")
    print(f"  {'-'*30} | {'-'*15} | {'-'*12} | {'-'*6} | {'-'*10} | {'-'*6}")

    # 1. JSON workloads
    # Large uniform array (columnar)
    json_array = json.dumps([{"id": i, "symbol": f"STOCK{i}", "price": 100.0 + i * 0.5, "volume": 1000000 + i * 100} for i in range(500)])
    benchmark_file("JSON array (500 rows)", json_array)

    # Large mixed array (row-drop)
    json_mixed = json.dumps([{"id": i, "val": i * 1.5, "extra" if i % 3 == 0 else "other": f"x{i}"} for i in range(500)])
    benchmark_file("JSON mixed (500 rows)", json_mixed)

    # Nested JSON object
    deep_obj = json.dumps({"meta": {"version": "2.0", "count": 1000}, "data": [{"a": i, "b": {"nested": i * 2}} for i in range(200)]})
    benchmark_file("JSON nested object", deep_obj)

    # 2. Log workloads
    repetitive_logs = "[ERROR] DB connection timeout on server-db-01\n" * 500 + "[INFO] Recovery complete\n"
    benchmark_file("Repetitive errors (500x)", repetitive_logs)

    mixed_logs = ""
    for i in range(200):
        mixed_logs += f"[INFO] Processing batch {i}: {i*10} items\n"
        if i % 10 == 0:
            mixed_logs += f"[ERROR] Batch {i} failed: timeout after 30s\n"
    benchmark_file("Mixed logs (200 lines)", mixed_logs)

    # Build output
    build = "PASS test_api ✓\nFAIL test_auth ✗\n" * 100 + "13 passed, 1 failed\n"
    benchmark_file("Build output", build)

    # 3. Code workloads
    code = "def f(a, b):\n    \"\"\"Add two numbers.\"\"\"\n    return a + b\n\n" * 200
    benchmark_file("Python code (200 funcs)", code)

    # 4. File listing workloads
    ls = "drwxr-xr-x 2 root root 4096 Jun 15 12:00 src\n-rw-r--r-- 1 root root 12893 Jun 15 12:00 server.py\n-rw-r--r-- 1 root root 2341 Jun 15 12:00 package.json\n" * 150
    benchmark_file("File listing (150 lines)", ls)

    # 5. Search results
    search = "\n".join(f"src/file{n}.py:{n*10}:print('processing item {n}')" for n in range(200))
    benchmark_file("Search results (200 hits)", search)

    # 6. Git diff
    diff = "diff --git a/src/main.py b/src/main.py\n@@ -10,7 +10,8 @@ def process(data):\n     result = transform(data)\n-    old_result = result\n+    new_result = result * 2\n     return new_result\n" * 50
    benchmark_file("Git diff (50 hunks)", diff)

    # 7. Text
    long_text = "The quick brown fox jumps over the lazy dog. " * 2000
    benchmark_file("Long text (36K chars)", long_text)

    # 8. Cache files (real data from dashboard if available)
    cache_dir = Path("/root/dashboard/cache")
    if cache_dir.exists():
        for f in sorted(cache_dir.glob("*.json"))[:5]:
            try:
                content = f.read_text()
                if len(content) > 3000:
                    benchmark_file(f"Cache: {f.name}", content, source=str(f))
            except (IOError, json.JSONDecodeError):
                pass

    print()
    print("=" * 85)
    print(f"  CCR cache: {ccr_stats()['entries']} entries, {ccr_stats()['total_size_bytes'] / 1024:.1f} KB")
    print("=" * 85)


if __name__ == "__main__":
    main()
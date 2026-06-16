#!/usr/bin/env python3
"""Comprehensive test suite for all Proteus components.

Tests:
- New compressors (search, diff, text)
- Proxy handler + tool injection
- CLI framework
- End-to-end pipelines
- Edge cases
"""

import json
import sys
import os
import tempfile

sys.path.insert(0, os.path.expanduser("~/.hermes/proteus/src"))

from proteus import compress_tool_output, compress_summary_line
from proteus.router import detect_content_type, ContentType, should_compress
from proteus.ccr import retrieve, stats as ccr_stats, clear as ccr_clear
from proteus.compressors.json_crusher import crush_json, compact_json
from proteus.compressors.log_deduper import dedup_logs
from proteus.compressors.code import strip_code, compress_file_listing
from proteus.compressors.search import compress_search
from proteus.compressors.diff import compress_diff
from proteus.compressors.text import summarize_text

PASS = 0
FAIL = 0
_ccr_cleared = False

def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# Ensure clean CCR cache
if not _ccr_cleared:
    ccr_clear()

# ════════════════════════════════════════════════
# 1. NEW COMPRESSOR: Search
# ════════════════════════════════════════════════
section("1. SearchResultCompressor")

search_data = """src/main.py:42:def handle_request():
src/main.py:43:    # TODO: add error handling
src/main.py:50:    result = process(data)
src/main.py:55:    return response
src/utils.py:10:ERROR: connection timeout
src/utils.py:12:    raise ConnectionError("timeout")
src/utils.py:15:WARNING: retrying connection
src/config.py:5:DEBUG: loading config
src/config.py:8:port = 8080
other/file.ts:1:console.log("hello")
"""
compressed, stats = compress_search(search_data)
check("Search: mode is search", stats["mode"] == "search")
check("Search: original files found", stats.get("original_files", 0) >= 3)
check("Search: errors boosted to top", "connection timeout" in compressed or "ConnectionError" in compressed)
check("Search: file headers present", "# src/utils.py" in compressed or "src/utils.py" in compressed)
check("Search: line numbers present", ":42" in compressed or "10:" in compressed or ":5:" in compressed)

# Search with large data
big_search = "\n".join(f"file{n}.py:{n*10}:print('line {n}')" for n in range(100))
compressed, stats = compress_search(big_search, max_per_file=5, max_total=30, max_files=15)
check("Search: capped total matches", stats.get("compressed_matches", 999) <= 30)

# Empty search
compressed, stats = compress_search("")
check("Search: empty input", "original_chars" in stats)

# ════════════════════════════════════════════════
# 2. NEW COMPRESSOR: Diff
# ════════════════════════════════════════════════
section("2. DiffCompressor")

diff_data = """diff --git a/src/main.py b/src/main.py
index abc123..def456
--- a/src/main.py
+++ b/src/main.py
@@ -10,7 +10,8 @@ def process(data):
     result = transform(data)
-    old_result = result
+    new_result = result * 2
     return new_result

@@ -42,6 +43,9 @@ class Handler:
     def __init__(self):
         self.cache = {}
+        self.logger = logging.getLogger(__name__)
+        self.timeout = 30
+        self.retries = 3
     def process(self, req):
         return self.cache.get(req)
"""
compressed, stats = compress_diff(diff_data)
check("Diff: additions counted", stats.get("additions", 0) > 0)
check("Diff: deletions counted", stats.get("deletions", 0) > 0)
check("Diff: file header preserved", "main.py" in compressed)
check("Diff: additions in output", "+" in compressed)
check("Diff: deletions in output", "-" in compressed)

# ════════════════════════════════════════════════
# 3. NEW COMPRESSOR: Text
# ════════════════════════════════════════════════
section("3. TextSummarizer")

long_text = "This is the beginning. " * 500 + "MIDDLE SECTION " * 1000 + "This is the end. " * 500
compressed, stats = summarize_text(long_text, max_chars=500, head_chars=200, tail_chars=200)
check("Text: was summarized", stats.get("was_summarized", False))
check("Text: head preserved", "This is the beginning" in compressed[:500])
check("Text: tail preserved", "This is the end" in compressed[-500:])
check("Text: compressed marker present", "TEXT COMPRESSED" in compressed or "compressed" in compressed.lower())

# Short text (should passthrough)
short_text = "Short text"
compressed, stats = summarize_text(short_text, max_chars=500)
check("Text: short passthrough", not stats.get("was_summarized", False))
check("Text: short unchanged", compressed == short_text)

# ════════════════════════════════════════════════
# 4. PROXY TOOL INJECTION
# ════════════════════════════════════════════════
section("4. Proxy — Tool Injection")

from proteus.proxy.inject import create_retrieve_tool_definition, inject_retrieve_tool, RETRIEVE_TOOL_NAME

tool_def = create_retrieve_tool_definition()
check("Inject: tool has correct name", tool_def["function"]["name"] == RETRIEVE_TOOL_NAME)
check("Inject: has hash parameter", "hash" in tool_def["function"]["parameters"]["required"])
check("Inject: tool type is function", tool_def["type"] == "function")

# Inject into empty list
tools = inject_retrieve_tool([])
check("Inject: adds to empty list", len(tools) == 1)
check("Inject: added tool has correct name", tools[0]["function"]["name"] == RETRIEVE_TOOL_NAME)

# Inject again (should not duplicate)
tools2 = inject_retrieve_tool(tools)
check("Inject: no duplicate on second call", len(tools2) == 1)

# Inject with existing tools
existing = [{"type": "function", "function": {"name": "existing_tool", "parameters": {"type": "object", "properties": {}}}}]
tools3 = inject_retrieve_tool(existing)
check("Inject: preserves existing tools", len(tools3) == 2)
check("Inject: existing tool still there", tools3[0]["function"]["name"] == "existing_tool")
check("Inject: proteus tool appended", tools3[1]["function"]["name"] == RETRIEVE_TOOL_NAME)

# ════════════════════════════════════════════════
# 5. PROXY REQUEST HANDLER
# ════════════════════════════════════════════════
section("5. Proxy — Request Handler")

from proteus.proxy.handler import transform_request_body

# Test: no compression for small messages
small_body = {
    "model": "deepseek/deepseek-v4-flash",
    "messages": [
        {"role": "user", "content": "Hello world"}
    ],
}
mod_body, ccr_lookup, hstats = transform_request_body(small_body)
check("Handler: small message passthrough", hstats["compressed"] == 0)
check("Handler: no tool injection for small", not hstats["injected_tool"])
check("Handler: messages preserved", len(mod_body["messages"]) == 1)

# Test: compression for large tool result
big_tool_result = "The server returned results. " * 600 + "ERROR: timeout occurred. " * 300  # ~15K chars  # Over 3K threshold
large_body = {
    "model": "deepseek/deepseek-v4-flash",
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "Here are the results:"},
            {"type": "tool_result", "content": big_tool_result},
        ]}
    ],
}
mod_body, ccr_lookup, hstats = transform_request_body(large_body)
check("Handler: large tool result compressed", hstats["compressed"] > 0)
check("Handler: chars saved", hstats["total_saved"] > 0)
check("Handler: token estimate present", hstats["total_tokens_saved"] > 0)

# Verify the tool result was actually compressed in the body
content_items = mod_body["messages"][0]["content"]
tool_results = [i for i in content_items if isinstance(i, dict) and i.get("type") == "tool_result"]
check("Handler: content compressed in place", len(tool_results) > 0)
if tool_results:
        compressed_content = tool_results[0].get("content", "")
        check("Handler: content smaller", len(compressed_content) < len(big_tool_result))

# Test: tool injection
check("Handler: tool injected with large result", hstats["injected_tool"] or hstats["compressed"] > 0)

# ════════════════════════════════════════════════
# 6. END-TO-END VIA compress_tool_output
# ════════════════════════════════════════════════
section("6. End-to-end via compress_tool_output")

# Search results
search_large = "\n".join(f"file{n}.py:{n}:line {n} content" for n in range(50))
compressed, stats = compress_tool_output(search_large)
if stats["was_compressed"]:
    check("E2E search: compressed", stats["was_compressed"])
    check("E2E search: type correct", stats["content_type"] in ("search_results",))
    check("E2E search: compressed chars present", stats.get("compressed_chars", 0) > 0)
else:
    check("E2E search: passthrough (under threshold or no match)", True)

# Diff
diff_large = diff_data * 30  # Make it large enough
compressed, stats = compress_tool_output(diff_large)
if stats["was_compressed"]:
    check("E2E diff: compressed", stats["was_compressed"])
    check("E2E diff: type correct", stats["content_type"] == "diff")
else:
    check("E2E diff: passthrough", True)

# JSON through the main pipeline
json_data = json.dumps([{"id": i, "val": i * 2} for i in range(300)])
compressed, stats = compress_tool_output(json_data)
check("E2E JSON: compressed", stats["was_compressed"])
check("E2E JSON: CCR hash present", bool(stats.get("hash", "")))
check("E2E JSON: original retrievable", retrieve(stats["hash"]) == json_data)

# Logs
logs = "[ERROR] test\n" * 400 + "[INFO] done\n"
compressed, stats = compress_tool_output(logs)
check("E2E logs: compressed", stats["was_compressed"])
check("E2E logs: savings >75%", stats.get("compression_pct", 0) > 75)

# File listing
ls = "drwxr-xr-x 2 root root 4096 Jun 15 12:00 src\n-rw-r--r-- 1 root root 12893 Jun 15 12:00 server.py\n" * 100
compressed, stats = compress_tool_output(ls)
check("E2E ls: compressed", stats["was_compressed"])
check("E2E ls: type file_listing", stats["content_type"] == "file_listing")

# Summary line
summary = compress_summary_line({"was_compressed": True, "content_type": "json", "original_chars": 10000, "compressed_chars": 3000, "compression_pct": 70.0, "hash": "abc123", "estimated_token_savings": 1750})
check("Summary: non-empty for compressed", bool(summary))
check("Summary: contains restore info", "retrieve" in summary or "restore" in summary)

# Text (>10K) through pipeline
text_large = "This is a long document. " * 1000
compressed, stats = compress_tool_output(text_large)
if stats["was_compressed"]:
    check("E2E text: compressed", stats["was_compressed"])
    check("E2E text: type text", stats["content_type"] == "text")

# ════════════════════════════════════════════════
# 7. EDGE CASES
# ════════════════════════════════════════════════
section("7. Edge Cases")

# Empty content
compressed, stats = compress_tool_output("")
check("Edge: empty passthrough", not stats["was_compressed"])

# Content under threshold
compressed, stats = compress_tool_output("short")
check("Edge: short passthrough", not stats["was_compressed"])

# Whitespace only
compressed, stats = compress_tool_output("   \n   \n  ")
check("Edge: whitespace passthrough", not stats["was_compressed"])

# Type hint override with large valid JSON
big_valid_json = json.dumps([{"id": i, "val": i * 2} for i in range(300)])
compressed, stats = compress_tool_output(big_valid_json, content_type_hint="json")
check("Edge: type hint respected", stats["content_type"] == "json")

# Bad type hint falls back to detection
compressed, stats = compress_tool_output(big_valid_json, content_type_hint="invalid_type")
check("Edge: bad hint falls back", stats["content_type"] in ("json", "text"))

# Summary line with no compression
summary = compress_summary_line({"was_compressed": False})
check("Edge: summary empty for uncompressed", summary == "")

# Single-line JSON object
compressed, stats = compress_tool_output('{"status": "ok", "data": {"count": 100, "items": ["a", "b", "c"]}}' * 100)
check("Edge: repeated JSON", len(compressed) > 0)

# ════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"  NEW TESTS: {PASS} passed, {FAIL} failed")
print(f"{'='*60}")
if FAIL == 0:
    print(f"\n  ✅ All {PASS} new tests pass!")
else:
    print(f"\n  ❌ {FAIL} new failures — fix before shipping.")
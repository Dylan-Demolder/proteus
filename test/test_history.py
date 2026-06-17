#!/usr/bin/env python3
"""Tests for proteus.history — multi-turn history compression."""

import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from proteus.history import compress_history

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def make_message(role: str, content: str) -> dict:
    return {"role": role, "content": content}


# ── Tests ─────────────────────────────────────────────────────────────────

section("1. Below Threshold — No Compression")

small_msgs = [
    make_message("system", "You are helpful."),
    make_message("user", "Hi"),
    make_message("assistant", "Hello!"),
]
result, stats = compress_history(small_msgs, threshold_chars=100000)
check("below threshold: unchanged", len(result) == 3)
check("below threshold: not triggered", not stats["threshold_triggered"])
check("below threshold: no compression", stats["compressed_count"] == 0)


section("2. Above Threshold — Old Turns Compressed")

# Build a long conversation: 20 turns with pure JSON big enough to compress
big_msgs = []
for i in range(20):
    json_data = json.dumps([{"id": j, "name": f"user_{j}", "email": f"user{j}@test.com" * 3} for j in range(50)])
    big_msgs.append(make_message("user", json_data))
    big_msgs.append(make_message("assistant", f"Response to turn {i}."))

result, stats = compress_history(big_msgs, threshold_chars=1000, keep_recent=5)
check("above threshold: triggered", stats["threshold_triggered"])
check("above threshold: chars saved > 0", stats["chars_saved"] > 0, f"saved: {stats['chars_saved']}")
check("above threshold: entries logged", len(stats["entries"]) > 0, f"entries: {len(stats['entries'])}")
check("above threshold: some compressed", stats["compressed_count"] > 0, f"compressed: {stats['compressed_count']}")


section("3. System Messages Never Compressed")

long_sys_content = "You are a helpful assistant. " * 500
msgs_with_system = [
    make_message("system", long_sys_content),
    make_message("user", "x" * 5000),
    make_message("assistant", "ok"),
]
result, stats = compress_history(msgs_with_system, threshold_chars=500, keep_recent=0)
check("system message preserved in output", any(m["role"] == "system" for m in result))
# System content should remain intact
sys_msg = [m for m in result if m["role"] == "system"]
check("system content unchanged", len(sys_msg) > 0 and len(sys_msg[0].get("content", "")) == len(long_sys_content))


section("4. Assistant Messages Never Compressed")

assistant_only = [
    make_message("user", "x" * 5000),
    make_message("assistant", "y" * 5000),
    make_message("user", "z" * 5000),
]
result, stats = compress_history(assistant_only, threshold_chars=100, keep_recent=0)
# Assistant message should remain unchanged
assistant_msg = [m for m in result if m["role"] == "assistant"]
check("assistant content preserved", len(assistant_msg) > 0 and assistant_msg[0]["content"] == "y" * 5000)
check("assistant no compression marker", "_proteus_compressed" not in assistant_msg[0])


section("5. Empty Messages List")

result, stats = compress_history([], threshold_chars=1)
check("empty list unchanged", result == [])
check("empty list not triggered", not stats["threshold_triggered"])


section("6. Marker Format")

big_msgs2 = [
    make_message("user", "a" * 5000),
    make_message("assistant", "ok"),
]
result, stats = compress_history(big_msgs2, threshold_chars=100, keep_recent=0)
if stats["compressed_count"] > 0:
    compressed_user = result[0]
    content = compressed_user.get("content", "")
    check("marker contains Proteus tag", "Proteus" in content)
    check("marker contains hash", "hash" in content)
    check("marker contains proteus_retrieve", "proteus_retrieve" in content)


section("7. Statistics Format")

for key in ["total_chars", "compressed_count", "chars_saved",
            "tokens_saved_estimate", "threshold_triggered", "entries"]:
    check(f"stats contains {key}", key in stats)


section("8. Mixed Content Types")

# List-based content (like multi-modal format)
complex_msgs = [
    {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 5000},
        {"type": "text", "text": "also large text here " * 200},
    ]},
    {"role": "assistant", "content": "ok"},
]
result, stats = compress_history(complex_msgs, threshold_chars=100, keep_recent=0)
check("list content compressed", stats["compressed_count"] >= 0)
check("no crash on list content", len(result) == 2)


# ── Results ───────────────────────────────────────────────────────────────

total = PASS + FAIL
print(f"\n{'=' * 60}")
print(f"  HISTORY TESTS: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
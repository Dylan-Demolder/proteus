"""Targeted coverage tests for Proteus — exercises uncovered lines.

Covers:
  1. CCR: retrieve_compressed, eviction, corrupt cache files
  2. Handler: tool_calls handling, user message compression
  3. Proxy server: integration tests via aiohttp test client
  4. History: system/assistant message helpers, list content, edge cases
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# ── Bootstrap ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PASS = 0
FAIL = 0


def check(name: str, ok: bool):
    global PASS, FAIL
    if ok:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}")
        FAIL += 1


def section(s: str):
    print(f"\n{'=' * 60}")
    print(f"  {s}")
    print(f"{'=' * 60}")


# =============================================================================
#  1. CCR — retrieve_compressed, eviction, corrupt files
# =============================================================================
section("CCR — Persistence & Edge Cases")

import proteus.ccr as ccr
from proteus import config

# Get default cache dir
default_cache = config.CCR_CACHE_DIR
max_entries = config.CCR_MAX_ENTRIES

# Save and restore original cache dir
_orig_cache = ccr.config.CCR_CACHE_DIR

# Test with a temp cache directory
_tmpdir = tempfile.mkdtemp()
ccr.config.CCR_CACHE_DIR = _tmpdir

# Clear any existing entries
for f in Path(_tmpdir).glob("*.json"):
    f.unlink()

# Store some entries
original = "Hello world, this is a test string for CCR storage and retrieval."
compressed = "Hello world..."
content_type = "text"
stats = {"original_lines": 1, "compressed_lines": 1}
h = ccr.store(original, compressed, content_type, stats)
check("store returns a hash string", isinstance(h, str) and len(h) == 12)

# Retrieve compressed
retrieved_compressed = ccr.retrieve_compressed(h)
check("retrieve_compressed returns compressed content", retrieved_compressed == compressed)

# Retrieve original
retrieved_orig = ccr.retrieve(h)
check("retrieve returns original content", retrieved_orig == original)

# Missing hash
missing = ccr.retrieve("nonexistenthash")
check("retrieve missing hash returns None", missing is None)

missing_comp = ccr.retrieve_compressed("nonexistenthash")
check("retrieve_compressed missing hash returns None", missing_comp is None)

# Corrupt cache file — write invalid JSON
cache_path = Path(_tmpdir) / f"{h}.json"
with open(cache_path, "w") as f:
    f.write("not valid json {")
corrupt_orig = ccr.retrieve(h)
check("retrieve corrupt file returns None", corrupt_orig is None)
corrupt_comp = ccr.retrieve_compressed(h)
check("retrieve_compressed corrupt file returns None", corrupt_comp is None)

# Test eviction
# Set max_entries temporarily low
_orig_max = ccr.config.CCR_MAX_ENTRIES
ccr.config.CCR_MAX_ENTRIES = 3

# Remove the corrupt file first
if cache_path.exists():
    cache_path.unlink()

# Store 4 entries — should evict down to 3
for i in range(4):
    ccr.store(f"original {i}", f"compressed {i}", "text", {})
check("eviction keeps 3 entries after storing 4", len(list(Path(_tmpdir).glob("*.json"))) <= 3)

# Test stats
cache_stats = ccr.stats()
check("stats returns entries count", cache_stats["entries"] <= 3)
check("stats has total_size_bytes", cache_stats["total_size_bytes"] > 0)
check("stats has cache_dir", _tmpdir in cache_stats["cache_dir"])
check("stats has max_entries", cache_stats["max_entries"] == 3)

# Test clear
ccr.clear()
check("clear removes all entries", len(list(Path(_tmpdir).glob("*.json"))) == 0)

# Restore
ccr.config.CCR_CACHE_DIR = _orig_cache
ccr.config.CCR_MAX_ENTRIES = _orig_max

# Clean up
import shutil

shutil.rmtree(_tmpdir, ignore_errors=True)


# =============================================================================
#  2. Handler — tool_calls handling, user message compression
# =============================================================================
section("Handler — Tool Calls & User Messages")

from proteus.proxy.handler import (
    _process_messages,
    handle_tool_calls,
    transform_request_body,
)

# Test handle_tool_calls — empty/invalid scenarios
result = handle_tool_calls({}, {})
check("handle_tool_calls empty body returns dict", isinstance(result, dict))

result = handle_tool_calls({"choices": []}, {})
check("handle_tool_calls empty choices unchanged", result == {"choices": []})

# Test with proteus_retrieve tool call
response = {
    "choices": [{
        "message": {
            "tool_calls": [{
                "function": {
                    "name": "proteus_retrieve",
                    "arguments": json.dumps({"hash": "abc123"}),
                }
            }]
        }
    }]
}
ccr_lookup = {"abc123": "original content here"}
result = handle_tool_calls(response, ccr_lookup)
tc = result["choices"][0]["message"]["tool_calls"][0]["function"]
check("handle_tool_calls injects original", tc.get("proteus_original") == "original content here")

# Test with missing hash in lookup
response2 = {
    "choices": [{
        "message": {
            "tool_calls": [{
                "function": {
                    "name": "proteus_retrieve",
                    "arguments": json.dumps({"hash": "nonexistent"}),
                }
            }]
        }
    }]
}
result2 = handle_tool_calls(response2, {"other": "data"})
tc2 = result2["choices"][0]["message"]["tool_calls"][0]["function"]
check("handle_tool_calls missing hash doesn't set original", "proteus_original" not in tc2)

# Test with malformed arguments JSON
response3 = {
    "choices": [{
        "message": {
            "tool_calls": [{
                "function": {
                    "name": "proteus_retrieve",
                    "arguments": "not valid json",
                }
            }]
        }
    }]
}
result3 = handle_tool_calls(response3, ccr_lookup)
check("handle_tool_calls handles malformed JSON gracefully", result3 is not None)

# Test non-proteus tool calls pass through unchanged
response4 = {
    "choices": [{
        "message": {
            "tool_calls": [{
                "function": {
                    "name": "get_weather",
                    "arguments": json.dumps({"city": "London"}),
                }
            }]
        }
    }]
}
result4 = handle_tool_calls(response4, ccr_lookup)
tc4 = result4["choices"][0]["message"]["tool_calls"][0]["function"]
check("handle_tool_calls non-proteus tool passes through", tc4["name"] == "get_weather")

# Test _process_messages with user message (plain text large content)
# This hits lines 69-77
user_msg_body = {
    "messages": [
        {"role": "user", "content": "A" * 5000},  # large user message
        {"role": "assistant", "content": "Short reply"},
    ]
}
mod_body, lookup, pstats = transform_request_body(user_msg_body)
# User messages may or may not compress depending on content type detect,
# but the code path is exercised
check("_process_messages user content runs without error", pstats is not None)


# =============================================================================
#  3. Proxy Server — Integration Tests via aiohttp TestClient
# =============================================================================
section("Proxy Server — Integration Tests")

# We patch the handler to avoid real upstream calls
# Test app creation and route registration
from proteus.proxy.server import ProteusProxy, create_app

app = create_app(backend="openrouter")
check("create_app returns web.Application", app is not None)
check("proxy stored in app", "proxy" in app)

# Check routes are registered
routes = app.router.routes()
route_paths = [r.resource.canonical for r in routes if r.resource]
check("has /v1/chat/completions route", any("/v1/chat/completions" in p for p in route_paths))
check("has /livez route", any("/livez" in p for p in route_paths))
check("has /readyz route", any("/readyz" in p for p in route_paths))
check("has /health route", any("/health" in p for p in route_paths))

# Test with aiohttp TestClient
try:
    import unittest

    from aiohttp import web

    class ProxyServerTest(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            # Local stand-in for the upstream API.
            #
            # These tests used to call the real openrouter.ai, which made CI
            # depend on a third party's uptime and status codes — commit
            # 2d8dbfb had to weaken the assertions to "non-2xx"/"non-5xx"
            # precisely because the live upstream's answers kept moving.
            # A local mock lets us assert exact status codes and relayed
            # bodies, with zero network egress.
            self.mock_app = web.Application()
            self.mock_app.router.add_post("/v1/chat/completions", self._mock_chat)
            self.mock_app.router.add_get("/v1/models", self._mock_models)
            self.mock_app.router.add_get("/v1/unauthorized", self._mock_unauthorized)
            self.mock_runner = web.AppRunner(self.mock_app)
            await self.mock_runner.setup()
            self.mock_site = web.TCPSite(self.mock_runner, "127.0.0.1", 0)
            await self.mock_site.start()
            mock_port = self.mock_site._server.sockets[0].getsockname()[1]

            self.app = create_app(
                backend="generic",
                upstream_url=f"http://127.0.0.1:{mock_port}/v1",
                api_key_env="PROTEUS_TEST_KEY",
            )
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await self.site.start()
            # Get the actual port
            _, port = self.site._server.sockets[0].getsockname()
            self.port = port
            self.base = f"http://127.0.0.1:{port}"

        async def asyncTearDown(self):
            await self.runner.cleanup()
            await self.mock_runner.cleanup()

        # ── Mock upstream handlers ─────────────────────────────────────

        async def _mock_chat(self, request: web.Request) -> web.Response:
            """Echo back what the proxy actually forwarded."""
            body = await request.json()
            return web.json_response({
                "id": "mock-chat-1",
                "object": "chat.completion",
                "model": body.get("model", ""),
                "received_messages": body.get("messages", []),
                "received_authorization": request.headers.get("Authorization", ""),
            })

        async def _mock_models(self, request: web.Request) -> web.Response:
            return web.json_response({"object": "list", "data": [{"id": "mock/model"}]})

        async def _mock_unauthorized(self, request: web.Request) -> web.Response:
            return web.json_response(
                {"error": {"message": "No API key provided", "code": 401}},
                status=401,
            )

        # ── Tests ──────────────────────────────────────────────────────

        async def test_livez(self):
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base}/livez") as resp:
                    data = await resp.json()
                    self.assertEqual(data["status"], "healthy")
                    self.assertTrue(data["alive"])

        async def test_readyz(self):
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base}/readyz") as resp:
                    data = await resp.json()
                    self.assertEqual(data["status"], "healthy")
                    self.assertTrue(data["ready"])

        async def test_health(self):
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base}/health") as resp:
                    data = await resp.json()
                    self.assertEqual(data["status"], "healthy")

        async def test_invalid_json_body(self):
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base}/v1/chat/completions",
                    data="not json at all",
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    self.assertEqual(resp.status, 400)
                    data = await resp.json()
                    self.assertIn("error", data)

        async def test_chat_completions_small_body(self):
            """Small body passes through unmodified and the reply is relayed."""
            import aiohttp
            messages = [
                {"role": "user", "content": "Hello! How are you?"},
                {"role": "assistant", "content": "I'm fine, thanks!"},
            ]
            body = json.dumps({
                "model": "deepseek/deepseek-chat",
                "messages": messages,
            })
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base}/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    # Exact code, not a range: the upstream is local, so a
                    # failure here is our bug rather than someone else's outage.
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()

            # The proxy must forward the body verbatim (input is below the
            # 3000-char compression threshold) and relay the reply.
            self.assertEqual(data["id"], "mock-chat-1")
            self.assertEqual(data["received_messages"], messages)
            self.assertEqual(data["model"], "deepseek/deepseek-chat")

        async def test_unknown_route(self):
            """Unknown routes are forwarded to the upstream and relayed back."""
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base}/models") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()

            self.assertEqual(data["object"], "list")
            self.assertEqual(data["data"][0]["id"], "mock/model")

        async def test_upstream_error_is_relayed(self):
            """A 4xx from upstream reaches the client as 4xx, not 502."""
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base}/unauthorized") as resp:
                    self.assertEqual(resp.status, 401)
                    data = await resp.json()

            self.assertIn("No API key provided", data["error"]["message"])

        async def test_no_live_network_calls(self):
            """The proxy's upstream must be the local mock, not a real host."""
            self.assertIn("127.0.0.1", self.app["proxy"].upstream_url)
            self.assertTrue(self.app["proxy"].upstream_url.startswith("http://127.0.0.1:"))

    # Run tests
    suite = unittest.TestLoader().loadTestsFromTestCase(ProxyServerTest)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    check(f"proxy server: {result.testsRun} integration tests", result.wasSuccessful())
    for test, tb in result.failures + result.errors:
        print(f"    -- {test}: {tb[:200]}...")

except ImportError:
    check("aiohttp test utils available", False)


# =============================================================================
#  4. History — Edge Cases
# =============================================================================
section("History — Edge Cases")

from proteus.history import (
    _count_message_chars,
    _extract_tool_content,
    _is_assistant_message,
    _is_system_message,
    compress_history,
)

# Helpers
check("_is_system_message True for system role", _is_system_message({"role": "system"}))
check("_is_system_message False for user role", not _is_system_message({"role": "user"}))
check("_is_assistant_message True for assistant", _is_assistant_message({"role": "assistant"}))
check("_is_assistant_message False for user", not _is_assistant_message({"role": "user"}))

# _extract_tool_content — text type items (hits lines 48-52)
text_msg = {
    "role": "user",
    "content": [
        {"type": "text", "text": "X" * 200},
        {"type": "tool_result", "content": "short"},
    ],
}
extracted = _extract_tool_content(text_msg)
check("_extract_tool_content finds text type content", extracted and len(extracted) >= 100)

# _extract_tool_content — no text/tool_result items
empty_list_msg = {"role": "user", "content": [{"type": "image", "url": "x.jpg"}]}
check("_extract_tool_content no text items returns None", _extract_tool_content(empty_list_msg) is None)

# _extract_tool_content — short content returns None
short_msg = {"role": "user", "content": "short"}
check("_extract_tool_content short string returns None", _extract_tool_content(short_msg) is None)

# _count_message_chars — with list content
list_content_msg = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": [{"type": "text", "text": "hello world"}, {"type": "tool_result", "content": "big result here"}]},
]
total = _count_message_chars(list_content_msg)
check("_count_message_chars handles list content", total > 0)

# History — below threshold (no compression)
small_msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
h_result, h_stats = compress_history(small_msgs, threshold_chars=100000)
check("history below threshold unchanged", h_result is small_msgs)
check("history below threshold not triggered", not h_stats["threshold_triggered"])

# History — with system messages preserved
large_system_msg = [{"role": "system", "content": "X" * 60000}]
large_result, large_stats = compress_history(large_system_msg, threshold_chars=10000, keep_recent=2)
check("history system message preserved", len(large_result) == 1)

# History — all assistant messages (should not compress)
assistant_only = [{"role": "assistant", "content": "X" * 60000}]
asst_result, asst_stats = compress_history(assistant_only, threshold_chars=10000)
check("history assistant messages not compressed", not asst_stats["compressed_count"] > 0)

# History — with list content tool results (hits lines 137-153)
list_tool_msgs = [
    {"role": "user", "content": "Do something"},
    {"role": "assistant", "content": "Running tool..."},
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "content": "A" * 60000,  # Large enough to trigger compression
            }
        ],
    },
]
list_result, list_stats = compress_history(list_tool_msgs, threshold_chars=5000, keep_recent=0)
check("history with list content tool result runs", list_stats["threshold_triggered"])

# History — content not compressible (random-like data may not compress well)
not_compressible = [
    {"role": "user", "content": "run tool"},
    {"role": "user", "content": "a" * 50000},  # Repetitive but not matchable by compressors
]
nc_result, nc_stats = compress_history(not_compressible, threshold_chars=1000, keep_recent=0)
check("history non-compressible content doesn't crash", True)

# History — empty messages
empty_msgs = []
empty_result, empty_stats = compress_history(empty_msgs)
check("history empty messages unchanged", empty_result == [])
check("history empty not triggered", not empty_stats["threshold_triggered"])


# =============================================================================
#  5. __init__.py — Fallback passthrough paths (partial coverage)
# =============================================================================
section("Package Init — Passthrough Paths")

from proteus import compress_tool_output

# Empty content
empty_result, empty_cstats = compress_tool_output("")
check("compress_tool_output empty string passthrough", not empty_cstats.get("was_compressed"))

# Whitespace-only
ws_result, ws_cstats = compress_tool_output("   \n  \t  ")
check("compress_tool_output whitespace passthrough", not ws_cstats.get("was_compressed"))

# Very short content
short_result, short_cstats = compress_tool_output("Hello")
check("compress_tool_output short passthrough", not short_cstats.get("was_compressed"))

# Non-matching content (binary-like / not detected by router)
bin_result, bin_cstats = compress_tool_output("a" * 5000)
check("compress_tool_output random content doesn't crash", bin_cstats is not None)


# =============================================================================
#  RESULTS
# =============================================================================
section(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    print("\n  ❌ Some tests failed!")
    sys.exit(1)
else:
    print("\n  ✅ All coverage tests pass!")

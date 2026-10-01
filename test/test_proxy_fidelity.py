"""Regression tests: the proxy compresses what agents actually send, and
compressors never corrupt what the model sees.

Covers:
  1. Code stripping never deletes code (strings, globs, inline block comments)
  2. JSON columnar output round-trips exactly; scalar arrays don't crash
  3. Row-drop marker hash is the one the CCR cache can retrieve
  4. CCR rejects hashes that aren't hashes
  5. Proxy handler: OpenAI `role: tool` messages, markers, retrieve helpers
  6. Proxy server end-to-end against a local mock upstream
"""

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from proteus import config

# Keep this suite's cache entries out of the user's real ~/.proteus cache.
config.CCR_CACHE_DIR = tempfile.mkdtemp(prefix="proteus-test-")

from proteus import ccr, compress_tool_output
from proteus.compressors.code import strip_code
from proteus.compressors.json_crusher import crush_json
from proteus.proxy.handler import (
    pending_retrieve_calls,
    run_retrieve,
    strip_retrieve_calls,
    transform_request_body,
)
from proteus.proxy.inject import RETRIEVE_TOOL_NAME

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


def big_json(rows: int = 300) -> str:
    return json.dumps(
        [{"id": i, "name": f"item-{i}", "tags": ["a", "b"], "ok": i % 2 == 0} for i in range(rows)],
        indent=2,
    )


# =============================================================================
#  1. Code stripping never deletes code
# =============================================================================
section("1. Code stripping keeps code")

js = """const a = 1; /* inline */
const b = "https://example.com/path"; // trailing
const g = ['src/**/*.ts'];
/**
 * JSDoc block
 */
const t = `multi
// inside a template literal

line`;
//go:build linux
const c = a /* mid */ + b;
"""
out = strip_code(js, "generic")
check("JS: line after an inline /* */ comment kept", "const b = " in out)
check("JS: URL inside a string kept", '"https://example.com/path"' in out)
check("JS: glob containing /* kept", "['src/**/*.ts']" in out)
check("JS: // inside template literal kept", "// inside a template literal" in out)
check("JS: blank line inside template literal kept", "literal\n\nline`" in out)
check("JS: //go:build directive kept", "//go:build linux" in out)
check("JS: code around a mid-line comment kept", "const c = a" in out and "+ b;" in out)
check("JS: comments removed", "inline" not in out and "trailing" not in out
      and "JSDoc" not in out and "mid" not in out)
check("JS: unterminated block comment returns input unchanged",
      strip_code("x = 1 /* never closed\ny = 2\n", "generic") == "x = 1 /* never closed\ny = 2\n")

rust = "fn f<'a>(x: &'a str) -> &'a str { let u = \"http://x\"; let c = '/'; x } // note\n"
out = strip_code(rust, "rust")
check("Rust: lifetimes don't hide the URL string", 'let u = "http://x";' in out)
check("Rust: char literal '/' kept", "let c = '/';" in out)
check("Rust: trailing comment removed", "note" not in out)

py = '''#!/usr/bin/env python
"""Module doc."""
import os  # inline comments stay

QUERY = (
    """SELECT *
    # not a comment

    FROM t"""
)

class OnlyDoc(Exception):
    """Only a docstring."""

def f(x):
    """Doc
    more doc
    """
    # full-line comment
    s = """
# data line
"""
    return x
'''
out = strip_code(py, "python")
check("Python: shebang kept", out.startswith("#!/usr/bin/env python"))
check("Python: docstrings removed", "Module doc" not in out and "more doc" not in out
      and "Only a docstring" not in out)
check("Python: full-line comment removed", "full-line comment" not in out)
check("Python: triple-quoted SQL that isn't a docstring kept", '"""SELECT *' in out and 'FROM t"""' in out)
check("Python: '#' line inside a string kept", "    # not a comment" in out and "# data line" in out)
check("Python: blank line inside a string kept", "comment\n\n    FROM" in out)
try:
    compile(out, "<stripped>", "exec")
    compiles = True
except SyntaxError:
    compiles = False
check("Python: docstring-only class body still compiles", compiles)

fragment = 'def f():\n    """doc"""\n    x = 1\n    y = """unterminated\n# kept verbatim\n'
out = strip_code(fragment, "python")
check("Python fragment: docstring before the break removed", '"""doc"""' not in out)
check("Python fragment: unterminated string passed through", '"""unterminated\n# kept verbatim' in out)

big_js = "".join(
    f"export const v{i} = '{i}/*';  // n{i}\nexport const w{i} = {i};\n" for i in range(150)
)
compressed, stats = compress_tool_output(big_js, "code_javascript")
check("E2E JS: compressed", stats["was_compressed"])
check("E2E JS: every statement survives",
      all(f"export const v{i} = '{i}/*';" in compressed and f"export const w{i} = {i};" in compressed
          for i in range(150)))


# =============================================================================
#  2. JSON columnar round-trips; scalar arrays don't crash
# =============================================================================
section("2. JSON crusher fidelity")


def decode_columnar(text: str) -> list[dict]:
    """Reference reader for the COLUMNS format."""
    decoder = json.JSONDecoder()

    def cells(line: str) -> list:
        values, i = [], 0
        while True:
            if i < len(line) and line[i] in '"{[':
                value, i = decoder.raw_decode(line, i)
            else:
                end = line.find(",", i)
                end = len(line) if end == -1 else end
                raw, i = line[i:end], end
                if raw == "":
                    value = None
                else:
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = raw
            values.append(value)
            if i >= len(line):
                return values
            i += 1  # the comma

    lines = text.split("\n")
    assert lines[0] == "COLUMNS"
    keys = [json.loads(k) if k.startswith('"') else k for k in lines[1][2:].split(", ")]
    return [dict(zip(keys, cells(line), strict=True)) for line in lines[2:]]


tricky = [
    {"flag": True, "off": False, "nested": {"x": [1, 2]}, "quote": 'say "hi", ok',
     "newline": "a\nb", "none": None, "empty": "", "numstr": "42", "litstr": "true",
     "plain": f"row {i}", "pad": " padded ", "num": i * 1.5}
    for i in range(60)
]
out, stats = crush_json(json.dumps(tricky))
check("Columnar: chosen for uniform rows", stats["mode"] == "columnar")
check("Columnar: one line per row (newlines escaped)", len(out.split("\n")) == 2 + len(tricky))
try:
    roundtrip = decode_columnar(out)
except Exception:
    roundtrip = None
check("Columnar: decodes back to exactly the input", roundtrip == tricky)
check("Columnar: JSON literals, not Python reprs", "True" not in out and "{'x'" not in out)

strings = json.dumps([f"/usr/lib/file_{i}.so" for i in range(300)], indent=1)
try:
    out, stats = compress_tool_output(strings)
    ok = stats["was_compressed"]
except AttributeError:
    ok = False
check("JSON array of strings compresses instead of crashing", ok)
try:
    crush_json("[1, 2, 3, 4, 5, 6]")
    ok = True
except AttributeError:
    ok = False
check("JSON array of numbers doesn't crash", ok)


# =============================================================================
#  3. Row-drop marker hash is retrievable
# =============================================================================
section("3. Row-drop marker hash")

mixed = json.dumps([{"i": i, "v": [i]} if i % 2 else {"i": i} for i in range(400)], indent=2)
out, stats = compress_tool_output(mixed)
m = re.search(r"hash=(\w+)", out)
check("Row drop: marker present", stats.get("mode") == "row_drop" and m is not None)
check("Row drop: marker hash == stored hash", m is not None and m.group(1) == stats["hash"])
check("Row drop: marker hash retrieves the original", m is not None and ccr.retrieve(m.group(1)) == mixed)


# =============================================================================
#  4. CCR hash validation
# =============================================================================
section("4. CCR hash validation")

outside = Path(config.CCR_CACHE_DIR).parent / "outside.json"
outside.write_text(json.dumps({"original": "secret"}))
check("CCR: path traversal hash rejected", ccr.retrieve("../outside") is None)
check("CCR: non-hex hash rejected", ccr.retrieve("not-a-hash") is None)
check("CCR: retrieve_compressed validates too", ccr.retrieve_compressed("../outside") is None)
outside.unlink()
check("CCR: no temp files left behind", not list(Path(config.CCR_CACHE_DIR).glob(".tmp-*")))


# =============================================================================
#  5. Proxy handler
# =============================================================================
section("5. Proxy handler — OpenAI tool messages")

payload = big_json()


def openai_body(content, **extra):
    return {
        "model": "m",
        "messages": [
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": content},
        ],
        **extra,
    }


body, _, st = transform_request_body(openai_body(payload))
tool_msg = body["messages"][2]["content"]
m = re.search(r'proteus_retrieve\(hash="(\w+)"\)', tool_msg)
check("role:tool string content compressed", st["compressed"] == 1 and len(tool_msg) < len(payload))
check("marker tells the model how to retrieve", m is not None)
check("marker hash retrieves the exact original", m is not None and ccr.retrieve(m.group(1)) == payload)
check("proteus_retrieve injected", any(t["function"]["name"] == RETRIEVE_TOOL_NAME for t in body["tools"]))
check("no private _proteus keys sent upstream", "_proteus" not in json.dumps(body))

body, _, st = transform_request_body(openai_body([{"type": "text", "text": payload}]))
check("role:tool list-of-text-parts compressed",
      st["compressed"] == 1 and len(body["messages"][2]["content"][0]["text"]) < len(payload))

body, _, st = transform_request_body(openai_body(payload, stream=True), inject_tool=False)
check("inject_tool=False still compresses", st["compressed"] == 1)
check("inject_tool=False adds no tool", "tools" not in body and not st["injected_tool"])
check("inject_tool=False marker doesn't promise a tool",
      "original cached as" in body["messages"][2]["content"]
      and "proteus_retrieve(" not in body["messages"][2]["content"])

client_tool = {"type": "function", "function": {"name": RETRIEVE_TOOL_NAME, "parameters": {}}}
body, _, st = transform_request_body(openai_body(payload, tools=[client_tool]), inject_tool=False)
check("client-defined proteus_retrieve: marker offers the call",
      "proteus_retrieve(" in body["messages"][2]["content"] and len(body["tools"]) == 1)

retrieved = {
    "model": "m",
    "messages": [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "r1", "type": "function",
             "function": {"name": RETRIEVE_TOOL_NAME, "arguments": '{"hash": "abc"}'}}]},
        {"role": "tool", "tool_call_id": "r1", "content": payload},
    ],
}
body, _, st = transform_request_body(retrieved)
check("results of proteus_retrieve are not re-compressed",
      st["compressed"] == 0 and body["messages"][1]["content"] == payload)

# Compression that saves little is not worth a possible retrieve round
small_diff = "\n".join(
    f"diff --git a/app/mod_{f}.py b/app/mod_{f}.py\nindex 1..2 100644\n--- a/app/mod_{f}.py\n"
    f"+++ b/app/mod_{f}.py\n@@ -1,3 +1,3 @@\n import os\n-VERSION = '1.{f}'\n+VERSION = '2.{f}'\n"
    f" print(VERSION)" for f in range(26))
body, _, st = transform_request_body(openai_body(small_diff))
check("little savings: tool output sent as is", st["compressed"] == 0
      and body["messages"][2]["content"] == small_diff)
config.update({"MIN_SAVINGS_PCT": 0})
body, _, st = transform_request_body(openai_body(small_diff))
check("min_savings_pct 0: small savings still compressed", st["compressed"] == 1)
config.reset()

# The marker says what was removed, so the model knows whether to retrieve
from proteus.proxy.handler import _marker


def mk(**st):
    return _marker({"original_chars": 9000, "compressed_chars": 900, "hash": "abc", **st}, True)


check("marker: lossless columnar says nothing dropped",
      "nothing dropped" in mk(compressor="json_crusher", mode="columnar", original_rows=300))
check("marker: row drop says how many rows are hidden",
      "480 of 500 rows not shown" in mk(compressor="json_crusher", mode="row_drop", original_rows=500,
                                        dropped_rows=480))
check("marker: code says code unchanged", "code unchanged" in mk(compressor="code_python"))
check("marker: logs say whether errors were kept",
      "every error line kept" in mk(compressor="log_deduper", errors_dropped=0)
      and "3 error lines not shown" in mk(compressor="log_deduper", errors_dropped=3))
check("marker: still names the hash", 'proteus_retrieve(hash="abc")' in mk(compressor="unknown"))
check("marker: mentions the query option", 'query="..."' in mk(compressor="unknown"))

# The tools list must not change between an agent's turns: it starts the
# prompt, and a change throws away the provider's cached prefix.
agent_tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
turn1 = {"model": "m", "tools": agent_tools, "messages": [{"role": "user", "content": "go"}]}
turn2 = {**openai_body(payload), "tools": agent_tools}
t1, _, _ = transform_request_body(json.loads(json.dumps(turn1)))
t2, _, _ = transform_request_body(json.loads(json.dumps(turn2)))
check("tools list identical before and after the first compression", t1["tools"] == t2["tools"]
      and RETRIEVE_TOOL_NAME in json.dumps(t1["tools"]))
t0, _, _ = transform_request_body({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
check("plain chat without tools gets no tool added", "tools" not in t0)

section("5b. Proxy handler — retrieve helpers")

h = ccr.store("alpha line\nBeta LINE\ngamma\n" + "x" * 100, "c", "text", {})


def call(args, name=RETRIEVE_TOOL_NAME, id_="t1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": args}}


check("run_retrieve returns the original", run_retrieve(call(json.dumps({"hash": h}))).startswith("alpha line"))
doc = "\n".join(f"filler {i}" for i in range(100))
doc = doc.replace("filler 10\n", "alpha line\n").replace("filler 11\n", "Beta LINE\n")
doc = doc.replace("filler 60\n", "The staging database listens on port 6543\n")
doc = doc.replace("filler 80\n", '  "order_id": 2217,\n').replace("filler 81\n", '  "status": "refunded",\n')
hd = ccr.store(doc, "c", "text", {})
q = run_retrieve(call(json.dumps({"hash": hd, "query": "line"})))
check("run_retrieve query filters lines (case-insensitive)", "11: alpha line" in q and "12: Beta LINE" in q
      and "port 6543" not in q)
check("run_retrieve query shows context lines", "10- filler 9" in q and "14- filler 13" in q
      and "15- filler 14" not in q)
q = run_retrieve(call(json.dumps({"hash": hd, "query": "refunded"})))
check("run_retrieve context carries the neighbouring field", '81-   "order_id": 2217,' in q)
q = run_retrieve(call(json.dumps({"hash": hd, "query": "staging database port"})))
check("run_retrieve multi-word query matches a line with every word", "61: The staging database" in q)
q = run_retrieve(call(json.dumps({"hash": hd, "query": "order_id.: [0-9]+"})))
check("run_retrieve query as a regex", "81: " in q and "refunded" in q and "port 6543" not in q)
orders = [{"order_id": 5000 + i, "status": "refunded" if i == 7 else "shipped", "total": i,
           **({"coupon": "SAVE8"} if i % 10 == 3 else {})} for i in range(60)]
hj = ccr.store(json.dumps(orders, indent=2), "c", "json", {})
q = run_retrieve(call(json.dumps({"hash": hj, "query": "SAVE8"})))
check("run_retrieve on a JSON array returns whole records",
      q.count('"order_id"') == 6 and '{"order_id": 5003, "status": "shipped", "total": 3, "coupon": "SAVE8"}' in q)
q = run_retrieve(call(json.dumps({"hash": hj, "query": '"status": "refunded"'})))
check("run_retrieve JSON query written as pretty-printed key/value", '"order_id": 5007' in q and q.count("order_id") == 1)
q = run_retrieve(call(json.dumps({"hash": hj, "query": "nothing Beta"})))
check("run_retrieve JSON with no matching record says so", q.startswith("No records"))
q = run_retrieve(call(json.dumps({"hash": hd, "query": "nothing Beta"})))
check("run_retrieve falls back to lines with any word", "12: Beta LINE" in q)
check("run_retrieve no match is a readable message",
      run_retrieve(call(json.dumps({"hash": hd, "query": "zebra"}))).startswith("No lines"))
check("run_retrieve returns the original when the query matches most of it",
      run_retrieve(call(json.dumps({"hash": h, "query": "a"}))).startswith("alpha line"))
check("run_retrieve unknown hash is a readable error", "no cached content" in run_retrieve(call('{"hash": "ffff"}')))
check("run_retrieve bad arguments is a readable error", run_retrieve(call("not json")).startswith("Error"))

only_retrieve = {"choices": [{"message": {"role": "assistant", "content": None,
                                          "tool_calls": [call(json.dumps({"hash": h}))]},
                              "finish_reason": "tool_calls"}]}
follow = pending_retrieve_calls(only_retrieve)
check("pending_retrieve_calls answers a retrieve-only turn",
      follow is not None and follow[1][0]["tool_call_id"] == "t1"
      and follow[1][0]["content"].startswith("alpha line"))
mixed_turn = {"choices": [{"message": {"role": "assistant", "content": None,
                                       "tool_calls": [call("{}", name="read", id_="x"),
                                                      call(json.dumps({"hash": h}))]},
                           "finish_reason": "tool_calls"}]}
check("pending_retrieve_calls leaves mixed turns to the client", pending_retrieve_calls(mixed_turn) is None)
stripped = strip_retrieve_calls(mixed_turn)["choices"][0]
check("strip_retrieve_calls keeps the client's own calls",
      [tc["function"]["name"] for tc in stripped["message"]["tool_calls"]] == ["read"])
stripped = strip_retrieve_calls(json.loads(json.dumps(only_retrieve)))["choices"][0]
check("strip_retrieve_calls turns a retrieve-only turn into a plain stop",
      "tool_calls" not in stripped["message"] and stripped["finish_reason"] == "stop"
      and stripped["message"]["content"] == "")


# =============================================================================
#  6. Proxy server end-to-end (local mock upstream, no network)
# =============================================================================
section("5c. Streamed replies: proteus_retrieve rounds")

from proteus.proxy.stream import StreamRound, split_events


def ev(**choice):
    return "data: " + json.dumps({"id": "x", "choices": [choice]})


def tc(index, name=None, args="", id_=None):
    fn = {"arguments": args} if name is None else {"name": name, "arguments": args}
    return {"index": index, **({"id": id_} if id_ else {}), "function": fn}


events, rest = split_events('data: {"a":1}\r\n\r\ndata: {"b"')
check("SSE: complete events split off, partial kept", events == ['data: {"a":1}'] and rest == 'data: {"b"')

# A mixed turn: retrieve call at index 0, the client's own tool at index 1
rnd = StreamRound()
out = rnd.feed(ev(delta={"tool_calls": [tc(0, RETRIEVE_TOOL_NAME, "", "r0")]}))
out += rnd.feed(ev(delta={"tool_calls": [tc(1, "read_file", '{"p"', "c1")]}))
out += rnd.feed(ev(delta={"tool_calls": [tc(0, None, '{"hash":"h"}'), tc(1, None, ':1}')]}))
out += rnd.feed(ev(delta={}, finish_reason="tool_calls"))
sent = [json.loads(e[6:]) for e in out]
check("Stream: retrieve deltas held back, client tool renumbered to 0",
      all(t["index"] == 0 and "proteus" not in json.dumps(t)
          for c in sent for t in c["choices"][0]["delta"].get("tool_calls", [])) and len(sent) == 2)
check("Stream: mixed turn gets no extra round", rnd.followup() is None)
fin = json.loads(rnd.closing()[0][6:])
check("Stream: mixed turn still finishes with tool_calls", fin["choices"][0]["finish_reason"] == "tool_calls")

# Retrieve only, but the round limit is reached: the turn ends for the client
rnd = StreamRound(can_retrieve=False)
rnd.feed(ev(delta={"tool_calls": [tc(0, RETRIEVE_TOOL_NAME, '{"hash":"h"}', "r0")]}))
rnd.feed(ev(delta={}, finish_reason="tool_calls"))
check("Stream: out of rounds, no followup", rnd.followup() is None)
check("Stream: out of rounds, finish becomes stop", json.loads(rnd.closing()[0][6:])["choices"][0]["finish_reason"] == "stop")

rnd = StreamRound()
check("Stream: comments and keep-alives pass through", rnd.feed(": ping") == [": ping"])
check("Stream: [DONE] held for the end", rnd.feed("data: [DONE]") == [] and rnd.done)
check("Stream: usage-only chunk held, usage recorded",
      rnd.feed('data: {"choices":[],"usage":{"prompt_tokens":7,"prompt_tokens_details":{"cached_tokens":3}}}') == []
      and rnd.usage == {"prompt_tokens": 7, "prompt_tokens_details": {"cached_tokens": 3}})

section("6. Proxy server end-to-end")

from aiohttp import ClientSession, web

from proteus.proxy.server import create_app


async def e2e():
    received: list[dict] = []
    paths: list[str] = []
    retrieve_once = {"armed": False, "always": False}

    async def chat(request):
        body = await request.json()
        received.append(body)
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            if retrieve_once["armed"]:
                retrieve_once["armed"] = False
                content = body["messages"][-1]["content"]
                args = json.dumps({"hash": re.search(r'hash="(\w+)"', content).group(1)})
                # The call arrives split across chunks, the way providers stream it
                for piece in (
                    {"delta": {"role": "assistant", "content": "Checking. "}},
                    {"delta": {"tool_calls": [{"index": 0, "id": "r1", "type": "function",
                                               "function": {"name": RETRIEVE_TOOL_NAME, "arguments": ""}}]}},
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[:5]}}]}},
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]}},
                    {"delta": {}, "finish_reason": "tool_calls"},
                ):
                    await resp.write(b"data: " + json.dumps({"id": "c1", "choices": [piece]}).encode() + b"\n\n")
                await resp.write(b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":5}}\n\n')
                await resp.write(b"data: [DONE]\n\n")
                return resp
            # Split an event across writes to exercise reassembly
            await resp.write(b'data: {"id":"c2","choices":[{"delta":{"role":"assistant","con')
            await resp.write(b'tent":"hi"}}]}\n\n')
            await resp.write(b'data: {"id":"c2","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
            await resp.write(b'data: {"id":"c2","choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":10}}\n\n')
            await resp.write(b"data: [DONE]\n\n")
            return resp
        if retrieve_once["always"] and body.get("tool_choice") != "none":
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Need more.",
                "tool_calls": [{"id": f"r{len(received)}", "type": "function", "function": {
                    "name": RETRIEVE_TOOL_NAME, "arguments": json.dumps({"hash": "ffff"})}}]},
                "finish_reason": "tool_calls"}]})
        if retrieve_once["armed"]:
            retrieve_once["armed"] = False
            content = body["messages"][-1]["content"]
            wanted = re.search(r'hash="(\w+)"', content).group(1)
            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "r1", "type": "function", "function": {
                        "name": RETRIEVE_TOOL_NAME, "arguments": json.dumps({"hash": wanted})}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            })
        return web.json_response({
            "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010},
        })

    async def anything(request):
        paths.append(request.path)
        if request.path.endswith("/broken"):
            return web.Response(text="<html>bad gateway</html>", status=502, content_type="text/html")
        return web.json_response({"object": "list", "data": []})

    async def chat_html_error(request):
        return web.Response(text="<html>overloaded</html>", status=503, content_type="text/html")

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat)
    upstream.router.add_post("/err/v1/chat/completions", chat_html_error)
    upstream.router.add_route("*", "/{p:.*}", anything)
    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    async def serve(upstream_url):
        app = create_app(backend="generic", upstream_url=upstream_url, api_key_env="PROTEUS_TEST_KEY")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return app, runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    app, runner, base = await serve(f"http://127.0.0.1:{up_port}/v1")
    err_app, err_runner, err_base = await serve(f"http://127.0.0.1:{up_port}/err/v1")

    async with ClientSession() as client:
        # Plain non-streaming request with an OpenAI tool message
        async with client.post(f"{base}/v1/chat/completions", json=openai_body(payload)) as r:
            data = await r.json()
        sent = received[-1]["messages"][2]["content"]
        check("E2E: upstream receives the compressed tool message", len(sent) < len(payload))
        check("E2E: reply relayed", r.status == 200 and data["choices"][0]["message"]["content"] == "done")

        # Streaming request: compressed too, and no tool the proxy can't serve
        async with client.post(f"{base}/v1/chat/completions", json=openai_body(payload, stream=True)) as r:
            text = await r.text()
        sent = received[-1]
        check("E2E stream: upstream receives the compressed tool message",
              len(sent["messages"][2]["content"]) < len(payload))
        check("E2E stream: proteus_retrieve offered", RETRIEVE_TOOL_NAME in json.dumps(sent.get("tools")))
        check("E2E stream: SSE relayed", r.status == 200 and '"content":"hi"' in text and "[DONE]" in text)

        # Streamed: the model calls proteus_retrieve; the proxy answers it and streams the next round
        retrieve_once["armed"] = True
        n_before = len(received)
        async with client.post(f"{base}/v1/chat/completions", json=openai_body(payload, stream=True)) as r:
            text = await r.text()
        followup = received[-1]["messages"]
        chunks = [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: {")]
        deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
        check("E2E stream retrieve: proxy made a second upstream call", len(received) == n_before + 2)
        check("E2E stream retrieve: second call carries the full original",
              followup[-1]["role"] == "tool" and followup[-1]["content"] == payload
              and followup[-2]["tool_calls"][0]["function"]["name"] == RETRIEVE_TOOL_NAME)
        check("E2E stream retrieve: client sees both rounds' text, no retrieve call",
              "".join(d.get("content", "") for d in deltas) == "Checking. hi"
              and RETRIEVE_TOOL_NAME not in text)
        check("E2E stream retrieve: one finish, one role, one [DONE]",
              [c["choices"][0].get("finish_reason") for c in chunks if c.get("choices")].count("stop") == 1
              and "tool_calls" not in text and sum("role" in d for d in deltas) == 1
              and text.count("[DONE]") == 1)
        check("E2E stream retrieve: usage summed and sent once",
              [c["usage"] for c in chunks if "usage" in c] == [{"prompt_tokens": 1100, "completion_tokens": 15}])
        check("E2E stream retrieve: count in a trailing SSE comment", ": proteus retrievals=1" in text)

        # The model calls proteus_retrieve: the proxy answers it and asks again
        retrieve_once["armed"] = True
        n_before = len(received)
        async with client.post(f"{base}/v1/chat/completions", json=openai_body(payload)) as r:
            data = await r.json()
        followup = received[-1]["messages"]
        check("E2E retrieve: proxy made a second upstream call", len(received) == n_before + 2)
        check("E2E retrieve: second call carries the full original",
              followup[-1]["role"] == "tool" and followup[-1]["content"] == payload)
        check("E2E retrieve: client gets the final answer, not the tool call",
              data["choices"][0]["message"]["content"] == "done"
              and "tool_calls" not in data["choices"][0]["message"])
        check("E2E retrieve: count reported in X-Proteus-Retrievals", r.headers.get("X-Proteus-Retrievals") == "1")
        check("E2E retrieve: usage summed across rounds",
              data["usage"] == {"prompt_tokens": 1100, "completion_tokens": 15, "total_tokens": 1115})
        async with client.get(f"{base}/readyz") as r:
            ready = await r.json()
        check("E2E retrieve: counted in /readyz (streamed and not)", ready["stats"]["retrievals_served"] == 2)

        # A model that keeps asking for retrieves gets one final round where it must answer
        retrieve_once["always"] = True
        n_before = len(received)
        async with client.post(f"{base}/v1/chat/completions", json=openai_body(payload)) as r:
            data = await r.json()
        retrieve_once["always"] = False
        last = received[-1]
        check("E2E retrieve limit: final round forces a text answer",
              len(received) == n_before + 4 and last.get("tool_choice") == "none"
              and all(b.get("tool_choice") != "none" for b in received[n_before:-1]))
        check("E2E retrieve limit: model told to say what it couldn't check",
              "last retrieve for this turn" in last["messages"][-1]["content"])
        check("E2E retrieve limit: client gets the answer", data["choices"][0]["message"]["content"] == "done")

        # Pass-through routes: /v1 prefix is not doubled
        async with client.get(f"{base}/v1/models") as r:
            await r.read()
        check("E2E: /v1/models reaches upstream as /v1/models", paths[-1] == "/v1/models")

        # Non-JSON upstream errors are relayed, not replaced with a generic 502
        async with client.post(f"{err_base}/v1/chat/completions", json=openai_body("small")) as r:
            text = await r.text()
        check("E2E: HTML upstream error relayed with its status", r.status == 503 and "overloaded" in text)

    proxy = app["proxy"]
    await runner.cleanup()
    await err_runner.cleanup()
    await up_runner.cleanup()
    check("E2E: upstream session closed on shutdown", proxy._session is None or proxy._session.closed)


asyncio.run(e2e())


# =============================================================================
#  7. Routing: only real grep output goes to the search compressor
# =============================================================================
section("7. Routing & log fidelity")


from proteus.compressors.search import compress_search as _cs

hits = [f"src/svc_{f:02d}.py:{10 + j * 7}:    timeout = get_setting('timeout_{f}_{j}')"
        for f in range(40) for j in range(8)]
hits.insert(5, "src/lonely.py:3:    timeout = 4711  # hard-coded")
out, st = _cs("\n".join(hits))
check("Search: a lone unusual match in a one-match file is kept", "timeout = 4711" in out)
check("Search: hidden matches are counted, all files accounted for",
      f"{321 - st['compressed_matches']} more matches not shown, {41 - st['compressed_files']} more files" in out)
check("Search: hidden matches summarized by shape", "× `timeout = get_setting('…')`" in out)
check("Search: stats say no unusual match was hidden", st["rare_hidden"] == 0)
from proteus.compressors.search import compress_search
from proteus.router import ContentType, detect_content_type

yaml_like = "\n".join(f"key_{i}: value number {i} with some text" for i in range(150))
out, stats = compress_tool_output(yaml_like)
check("YAML-style 'key: value' is not routed to search", stats["content_type"] != "search_results")
check("YAML-style: every key survives", all(f"key_{i}:" in out for i in range(150)))

describe = "\n".join(f"Name:  pod-{i}\nNamespace:  default\nStatus:  Running\n" for i in range(80))
out, stats = compress_tool_output(describe)
check("kubectl-describe style is not routed to search", stats["content_type"] != "search_results")
check("kubectl-describe style: every pod survives", all(f"pod-{i}\n" in out for i in range(80)))

log_lines = [
    f"2026-09-30T10:{i // 60:02d}:{i % 60:02d}Z "
    + (f"ERROR payment failed for order {i}" if i % 10 == 5 else f"INFO request {i} served")
    for i in range(300)
]
log = "\n".join(log_lines)
check("ISO-timestamped log detected as logs, not search",
      detect_content_type(log) == ContentType.LOGS)
out, stats = compress_tool_output(log)
check("ISO-timestamped log: every error survives",
      all(f"order {i}" in out for i in range(5, 300, 10)))

pytest_out = "\n".join(f"tests/test_m{i}.py::test_{j} PASSED" for i in range(10) for j in range(10))
check("pytest node ids are not grep hits", detect_content_type(pytest_out) != ContentType.SEARCH_RESULTS)

grep_n = "\n".join(f"src/pkg/mod_{i}.py:{i * 3}:    value = compute({i})" for i in range(100))
check("real `grep -n` output is still search results",
      detect_content_type(grep_n) == ContentType.SEARCH_RESULTS)
rg_ctx = "\n".join(f"src/app.js-{i}-  const x{i} = {i};" for i in range(1, 60))
check("real `rg -C` context output is still search results",
      detect_content_type(rg_ctx) == ContentType.SEARCH_RESULTS)
grep_no_n = "\n".join(f"docs/page_{i}.md: mentions the config flag" for i in range(80))
check("`grep -r` without line numbers is still search results",
      detect_content_type(grep_no_n) == ContentType.SEARCH_RESULTS)

access_log = "\n".join(
    f'10.0.1.{i % 7} - - [17/Jun/2025:08:00:{i % 60:02d} +0000] "GET /api/{i} HTTP/1.1" 200 {i * 13}'
    for i in range(300)
)
out, stats = compress_search(access_log)
check("search compressor returns non-search input unchanged", out == access_log)
out, stats = compress_tool_output(access_log, content_type_hint="search_results")
check("a wrong type hint never yields empty output", out.strip() != "")

section("7b. Log truncation keeps errors")

def word(i: int) -> str:
    """A distinct letters-only token, so routine lines differ in more than numbers."""
    return "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))


def ts(i: int) -> str:
    return f"2026-09-30T10:{i // 60 % 60:02d}:{i % 60:02d}Z "


many = "\n".join(
    ts(i) + (f"ERROR job {i} failed: exit {i}" if i % 50 == 25 else f"INFO job {word(i)} finished")
    for i in range(1000)
)
out, stats = compress_tool_output(many)
check("long log is compressed", stats["was_compressed"])
check("errors from the truncated middle survive",
      all(f"ERROR job {i} failed" in out for i in range(25, 1000, 50)))
check("omitted runs are marked", "lines omitted" in out)
check("log stats: no errors dropped", stats["errors_dropped"] == 0)
check("omitted runs say no errors were dropped", "no errors or stack frames among them" in out)
flood = "\n".join(ts(i) + (f"ERROR e{i}" if 300 <= i < 600 else f"INFO r {word(i)}") for i in range(900))
fstats = compress_tool_output(flood)[1]
check("log stats: errors past the cap are counted", fstats.get("errors_dropped") == 100)

# Routine lines that differ only in numbers collapse to one pattern, and an
# outlier value in them is kept (a model asked for the slowest request).
timed = "\n".join(ts(i) + (f"ERROR order {7000 + i} failed" if i % 100 == 7
                            else f"INFO request {i} served in {2950 if i == 577 else 10 + i % 50}ms")
                   for i in range(1000))
out, stats = compress_tool_output(timed)
check("Logs: lines differing only in numbers collapse", "[x9" in out and stats["compressed_chars"] < 2500)
check("Logs: the outlier value is kept", "[outlier]" in out and "request 577 served in 2950ms" in out)
check("Logs: error lines keep their numbers (each order id kept)",
      all(f"order {7000 + i} failed" in out for i in range(7, 1000, 100)))
from proteus.compressors.log_deduper import _truncate_keeping_errors

capped = _truncate_keeping_errors(["x"] * 4 + ["ERROR a", "ERROR b", "ERROR c"] + ["x"] * 4, 2)
check("omitted runs that dropped errors don't claim otherwise",
      "ERROR c" not in capped and "... 4 lines omitted ..." in capped)


# =============================================================================
#  8. Diff compressor: nothing disappears silently
# =============================================================================
section("8. Diff fidelity")

from proteus.compressors.diff import compress_diff

parts = []
for f in range(25):
    parts.append(f"diff --git a/src/f{f}.py b/src/f{f}.py\nindex 1..2 100644\n--- a/src/f{f}.py\n+++ b/src/f{f}.py")
    for h in range(12):
        parts.append(f"@@ -{h * 20 + 1},7 +{h * 20 + 1},7 @@ def fn{h}():")
        parts += [f"     ctx {f} {h} {k}" for k in range(3)]
        parts += [f"-    old_{f}_{h}", f"+    new_{f}_{h}"]
        parts += [f"     ctx {f} {h} {k}" for k in range(3, 6)]
big_diff = "\n".join(parts)
out, stats = compress_diff(big_diff)
check("Diff: context adjacent to a change is kept",
      "     ctx 0 0 2\n-    old_0_0" in out and "+    new_0_0\n     ctx 0 0 3" in out)
check("Diff: distant context is dropped", "ctx 0 0 0" not in out and "ctx 0 0 5" not in out)
check("Diff: omitted hunks are announced", "... 2 more hunks in src/f0.py omitted (+2 -2)" in out)
check("Diff: files past the cap are named, not shown as empty headers",
      "diff --git a/src/f20.py" not in out
      and "5 more files omitted (+60 -60): src/f20.py, src/f21.py" in out)
check("Diff: stats count every change", stats["additions"] == 300 and stats["deletions"] == 300)

tricky_diff = """commit abc123
Author: A <a@x>

    Fix the thing

diff --git a/x.sql b/x.sql
--- a/x.sql
+++ b/x.sql
@@ -1,3 +1,3 @@
 SELECT 1;
---- old comment
+++++ new comment
 SELECT 2;"""
out, stats = compress_diff(tricky_diff)
check("Diff: removed line reading '--- x' is not a new file", out == tricky_diff and stats["files_affected"] == 1)
check("Diff: commit text in `git log -p` output is kept", "    Fix the thing" in out)


# =============================================================================
#  RESULTS
# =============================================================================
section(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    print("\n  ❌ Some tests failed!")
    sys.exit(1)
else:
    print("\n  ✅ All proxy & fidelity tests pass!")

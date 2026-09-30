"""Live evaluation: does a real model still answer correctly through Proteus?

Each scenario is an agent-style conversation: the model called a tool, got a
large result, and is asked a question whose answer is somewhere in it. Every
scenario runs twice against the same model, once straight to the API and
once through an in-process Proteus proxy, and the report compares:

  - correct: all expected facts appear in the answer
  - prompt tokens: as billed by the API (summed over retrieve rounds)
  - retrieves: proteus_retrieve calls the proxy answered for the model

Some answers survive compression (columnar JSON, deduped logs). Others sit in
content the compressor drops (the middle of a long text, dropped JSON rows),
so the model only gets them right if it uses the proteus_retrieve marker.

Usage:
    export OPENCODE_GO_API_KEY=...
    python benchmarks/live_eval.py --list-models
    python benchmarks/live_eval.py --model <model-id>
    python benchmarks/live_eval.py --model <model-id> --scenario json_dropped_rows -v

    # No network or key needed: shows whether each answer is still visible
    # after compression, i.e. which scenarios need proteus_retrieve.
    python benchmarks/live_eval.py --dry-run

Options --backend/--upstream-url/--api-key-env select another OpenAI-
compatible provider, exactly as for `proteus proxy`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from proteus import config

# Keep evaluation entries out of the user's real cache.
config.CCR_CACHE_DIR = tempfile.mkdtemp(prefix="proteus-live-eval-")

from proteus import compress_tool_output
from proteus.proxy.backends import get_backend, list_backends


@dataclass
class Scenario:
    name: str
    tool: str
    tool_args: dict
    output: str
    question: str
    expected: list[str]  # every string must appear in the answer (case-insensitive)
    note: str


def _scenarios() -> list[Scenario]:
    rng = random.Random(7)
    out: list[Scenario] = []

    # 1. Uniform rows → columnar (lossless): answer is visible.
    rows = [
        {"sku": f"SKU-{i:04d}", "name": f"Widget {i}", "price": round(5 + rng.random() * 95, 2),
         "stock": rng.randint(0, 500), "warehouse": rng.choice(["AMS", "SFO", "SIN", "GRU"])}
        for i in range(300)
    ]
    target = rows[173]
    out.append(Scenario(
        "json_columnar", "http_get", {"url": "https://inventory.internal/api/products"},
        json.dumps(rows, indent=2),
        f"What is the price and warehouse of {target['sku']}? Answer with just the price and warehouse code.",
        [str(target["price"]), target["warehouse"]],
        "uniform rows → columnar, lossless",
    ))

    # 2. Mixed rows → row-drop: the target is in the dropped middle.
    orders = []
    for i in range(500):
        order = {"order_id": 2000 + i, "status": rng.choice(["shipped", "delivered", "processing"]),
                 "total": round(rng.random() * 300, 2)}
        if i % 3 == 0:
            order["coupon"] = f"C{i}"
        orders.append(order)
    orders[217]["status"] = "refunded"
    out.append(Scenario(
        "json_dropped_rows", "http_get", {"url": "https://shop.internal/api/orders"},
        json.dumps(orders, indent=2),
        "Which order_id has status 'refunded'? Answer with just the number.",
        [str(orders[217]["order_id"])],
        "row-drop hides the answer; needs proteus_retrieve",
    ))

    # 3. Long log with errors scattered through it.
    lines = []
    failed = []
    for i in range(1200):
        ts = f"2026-09-30T10:{i // 60 % 60:02d}:{i % 60:02d}Z"
        if i % 97 == 41:
            oid = 9000 + i
            failed.append(str(oid))
            lines.append(f"{ts} ERROR payment failed for order {oid}: card_declined")
        else:
            lines.append(f"{ts} INFO request {i} served in {rng.randint(3, 90)}ms")
    out.append(Scenario(
        "log_errors", "run_command", {"cmd": "kubectl logs deploy/payments --since=1h"},
        "\n".join(lines),
        "List every order number whose payment failed. Answer with the numbers only, comma-separated.",
        failed,
        "errors must survive dedup + truncation",
    ))

    # 4. Long document; the fact is in the middle the summarizer drops.
    paragraphs = [
        f"## Section {i}\n\nThis section describes subsystem {i}. It covers setup, monitoring and "
        f"on-call procedures in detail, including escalation paths and dashboards for team {i}."
        for i in range(120)
    ]
    paragraphs[61] += "\n\nThe staging database listens on port 6543 and is reset every Sunday."
    out.append(Scenario(
        "text_middle_fact", "read_file", {"path": "docs/RUNBOOK.md"},
        "\n\n".join(paragraphs),
        "According to the runbook, which port does the staging database listen on? Answer with just the number.",
        ["6543"],
        "text summarizer drops the middle; needs proteus_retrieve",
    ))

    # 5. Grep output: the match of interest is beyond the per-file cap.
    hits = []
    for f in range(40):
        for j in range(8):
            hits.append(f"src/services/svc_{f:02d}.py:{10 + j * 7}:    timeout = get_setting('timeout_{f}_{j}')")
    hits.append("src/services/svc_17.py:99:    timeout = 4711  # FIXME hard-coded")
    out.append(Scenario(
        "grep_capped", "run_command", {"cmd": "rg -n 'timeout =' src/"},
        "\n".join(hits),
        "Which file and line hard-codes the timeout as a number, and what is the value? Answer as file:line value.",
        ["svc_17.py", "99", "4711"],
        "search keeps ~30 matches; FIXME scores high so should survive",
    ))

    # 6. Source file: comments stripped, code intact.
    funcs = []
    for i in range(160):
        funcs.append(
            f"def rate_limit_{i}(user):\n"
            f"    \"\"\"Return the per-minute request limit for tier {i}.\"\"\"\n"
            f"    # tiers are tuned by the platform team\n"
            f"    return {100 + i * 3}\n"
        )
    out.append(Scenario(
        "code_function", "read_file", {"path": "src/limits.py"},
        "\n\n".join(funcs),
        "What does rate_limit_137 return? Answer with just the number.",
        [str(100 + 137 * 3)],
        "comment/docstring stripping keeps code",
    ))

    # 7. Diff across more files than the cap.
    parts = []
    for f in range(26):
        parts.append(
            f"diff --git a/app/mod_{f}.py b/app/mod_{f}.py\nindex 1..2 100644\n"
            f"--- a/app/mod_{f}.py\n+++ b/app/mod_{f}.py\n@@ -1,3 +1,3 @@\n import os\n"
            f"-VERSION = '1.{f}'\n+VERSION = '2.{f}'\n print(VERSION)"
        )
    out.append(Scenario(
        "diff_many_files", "run_command", {"cmd": "git diff main"},
        "\n".join(parts),
        "What is the new VERSION value in app/mod_24.py? Answer with just the value.",
        ["2.24"],
        "files past the cap are named; the value needs proteus_retrieve",
    ))
    return out


def build_body(s: Scenario, model: str) -> dict:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": "You are a precise assistant. Answer from the tool results."},
            {"role": "user", "content": s.question},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": s.tool, "arguments": json.dumps(s.tool_args)},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": s.output},
        ],
        "tools": [{
            "type": "function",
            "function": {"name": s.tool, "description": f"{s.tool} tool",
                         "parameters": {"type": "object", "properties": {}}},
        }],
    }


def score(answer: str, expected: list[str]) -> tuple[bool, float]:
    text = answer.casefold()
    found = sum(1 for e in expected if e.casefold() in text)
    return found == len(expected), found / len(expected)


def dry_run(scenarios: list[Scenario]) -> int:
    print(f"{'scenario':20s} {'type':15s} {'original':>9s} {'compressed':>10s}  answer visible  note")
    for s in scenarios:
        compressed, stats = compress_tool_output(s.output)
        visible = sum(1 for e in s.expected if e.casefold() in compressed.casefold())
        mark = "yes" if visible == len(s.expected) else f"{visible}/{len(s.expected)}"
        print(f"{s.name:20s} {stats['content_type']:15s} {len(s.output):>9,} {len(compressed):>10,}  "
              f"{mark:14s}  {s.note}")
    return 0


async def _post(session, url: str, body: dict, headers: dict) -> tuple[int, dict, float]:
    start = time.monotonic()
    async with session.post(url, json=body, headers=headers) as resp:
        try:
            data = await resp.json(content_type=None)
        except ValueError:
            data = {"error": (await resp.text())[:300]}
        return resp.status, data if isinstance(data, dict) else {"error": str(data)}, time.monotonic() - start


def _answer(data: dict) -> str:
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


async def live(args, scenarios: list[Scenario]) -> int:
    import aiohttp
    from aiohttp import web

    from proteus.proxy.server import create_app

    backend = get_backend(args.backend, upstream_url=args.upstream_url, api_key_env=args.api_key_env)
    if not backend.api_key:
        print(f"error: {backend.api_key_env} is not set", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {backend.api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        if args.list_models:
            async with session.get(f"{backend.upstream_url}/models", headers=headers) as resp:
                data = await resp.json(content_type=None)
            for m in (data.get("data") or []) if isinstance(data, dict) else []:
                print(m.get("id"))
            return 0 if resp.status == 200 else 1

        app = create_app(backend=backend)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        proxy_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1/chat/completions"
        proxy = app["proxy"]

        rows = []
        try:
            for s in scenarios:
                for mode, url in (("direct", f"{backend.upstream_url}/chat/completions"), ("proteus", proxy_url)):
                    body = backend.transform_request(build_body(s, args.model))
                    served_before = proxy._stats["retrievals_served"]
                    status, data, secs = await _post(session, url, body, headers)
                    answer = _answer(data)
                    ok, frac = score(answer, s.expected)
                    usage = data.get("usage") or {}
                    rows.append({
                        "scenario": s.name, "mode": mode, "status": status, "correct": ok,
                        "recall": round(frac, 2), "prompt_tokens": usage.get("prompt_tokens"),
                        "retrieves": proxy._stats["retrievals_served"] - served_before if mode == "proteus" else 0,
                        "seconds": round(secs, 1), "answer": answer.strip()[:200],
                        "error": None if status == 200 else json.dumps(data)[:300],
                    })
                    if args.verbose:
                        print(f"[{s.name}/{mode}] {status} {secs:.1f}s ok={ok} :: {answer.strip()[:160]!r}",
                              file=sys.stderr)
        finally:
            await runner.cleanup()

    print(f"\nModel: {args.model} via {backend.name}\n")
    print("| scenario | mode | correct | prompt tokens | retrieves | seconds |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        correct = "✅" if r["correct"] else ("❌" if r["status"] == 200 else f"HTTP {r['status']}")
        if not r["correct"] and r["recall"] and r["status"] == 200:
            correct += f" ({r['recall']:.0%})"
        print(f"| {r['scenario']} | {r['mode']} | {correct} | {r['prompt_tokens']} | {r['retrieves']} | {r['seconds']} |")

    for mode in ("direct", "proteus"):
        mine = [r for r in rows if r["mode"] == mode and r["status"] == 200]
        tokens = sum(r["prompt_tokens"] or 0 for r in mine)
        print(f"\n{mode}: {sum(r['correct'] for r in mine)}/{len(mine)} correct, {tokens:,} prompt tokens")
    for r in rows:
        if r["error"]:
            print(f"\n{r['scenario']}/{r['mode']} error: {r['error']}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nRaw results: {args.json}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", default="opencode-go", choices=list(list_backends()))
    parser.add_argument("--upstream-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--model", help="model id (see --list-models)")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--scenario", action="append", help="run only this scenario (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="no API calls: show what compression keeps")
    parser.add_argument("--json", help="also write raw results to this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    scenarios = _scenarios()
    if args.scenario:
        unknown = set(args.scenario) - {s.name for s in scenarios}
        if unknown:
            parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s.name in args.scenario]

    if args.dry_run:
        return dry_run(scenarios)
    if not args.model and not args.list_models:
        parser.error("--model is required (use --list-models to see what the backend offers)")
    return asyncio.run(live(args, scenarios))


if __name__ == "__main__":
    sys.exit(main())

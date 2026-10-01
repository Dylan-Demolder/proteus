"""Agent evaluation: a real multi-turn tool loop, direct versus through Proteus.

live_eval.py asks one question about one tool result. This runs the loop an
agent actually runs: the model gets a task and tools, calls them as it sees
fit, the harness executes the calls and sends back the results, and so on
until the model answers. Every request streams, every conversation keeps one
session ID (so the provider can cache the growing prefix), and the tools
return full-size output, which is what makes agents expensive.

The workspace is synthetic and generated from a fixed seed: source files, a
payment log, a runbook, a settings file and an HTTP API. The tools are
implemented in Python against it (no shell commands are run):

  list_files(path)          every file under path, with sizes
  read_file(path)           the whole file
  search(pattern, path)     regex search, rg -n style output (path:line:text)
  http_get(url)             the API fixture

Each task needs several tool calls and has a checkable answer.

Reported per task, for direct and for Proteus: correct runs, model calls,
tool calls, prompt tokens (and how many of them the provider served from
cache), completion tokens, estimated cost at the model's per-token prices,
and proteus_retrieve calls the proxy answered.

Usage:
    export OPENCODE_GO_API_KEY=...
    python benchmarks/agent_eval.py --model deepseek-v4.1-flash --repeat 5
    python benchmarks/agent_eval.py --model mimo-v2.6-flash --task refunds -v
    python benchmarks/agent_eval.py --workspace aiohttp --model <model-id>   # real code
    python benchmarks/agent_eval.py --show-workspace      # offline: sizes, tasks, answers
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import math
import random
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from proteus import config

config.CCR_CACHE_DIR = tempfile.mkdtemp(prefix="proteus-agent-eval-")

from proteus.proxy.backends import get_backend, list_backends
from proteus.proxy.stream import add_usage, split_events

# $ per 1M tokens: input, cached input, output (OpenCode Go, off-peak where it varies).
PRICES = {
    "deepseek-v4.1-flash": (0.15, 0.003, 0.60),
    "deepseek-v4-flash": (0.15, 0.003, 0.60),
    "mimo-v2.6-flash": (0.14, 0.0028, 0.28),
}


# ── Workspace ──

def build_workspace(seed: int = 11) -> tuple[dict[str, str], dict[str, str]]:
    """Files by path, and API responses by URL."""
    rng = random.Random(seed)
    files: dict[str, str] = {}

    # Services: timeouts come from settings, except one hard-coded.
    for f in range(40):
        lines = [f'"""Service {f}: request handling for the {rng.choice(["billing", "search", "auth", "catalog"])} API."""',
                 "", "from app.settings import get_setting", ""]
        for j in range(8):
            lines += [
                f"def handler_{f}_{j}(request):",
                f'    """Handle request type {j} for service {f}."""',
                f"    timeout = get_setting('timeout_{f}_{j}')",
                "    return call_backend(request, timeout=timeout)",
                "",
            ]
        if f == 17:
            lines += ["def handler_17_legacy(request):",
                      "    timeout = 4711  # FIXME should use get_setting('payments_timeout')",
                      "    return call_backend(request, timeout=timeout)", ""]
        files[f"src/services/svc_{f:02d}.py"] = "\n".join(lines)

    # Payments: the gateway error codes and who raises them.
    gateway = ['"""Payment gateway client."""', "", "class GatewayError(Exception):", "    pass", ""]
    for i in range(30):
        gateway += [f"def refund_step_{i}(order):", f'    """Refund step {i}."""', f"    return order.refund({i})", ""]
    gateway += ["def charge_with_retry(order, attempts=3):",
                '    """Charge the card, retrying transient gateway failures."""',
                "    for _ in range(attempts):",
                "        if order.gateway.charge(order):",
                "            return True",
                '    raise GatewayError("PG-4012")', ""]
    files["src/payments/gateway.py"] = "\n".join(gateway)
    files["src/payments/cards.py"] = "\n".join(
        ['"""Card checks."""', "", "from .gateway import GatewayError", ""]
        + [f"def check_{i}(card):\n    return card.valid({i})\n" for i in range(40)]
        + ["def validate_expiry(card):", '    raise GatewayError("PG-4011")', ""])

    # Payment log: routine lines and card declines, three gateway failures in the middle.
    log = []
    for i in range(1500):
        ts = f"2026-09-30T{10 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z"
        if i in (611, 702, 845):
            log.append(f"{ts} ERROR payment failed for order {7000 + i}: gateway error PG-4012 after 3 attempts")
        elif i % 83 == 5:
            log.append(f"{ts} WARN payment declined for order {7000 + i}: card_declined")
        else:
            ms = 2950 if i == 977 else rng.randint(3, 90)  # one slow request, mid-log
            log.append(f"{ts} INFO request {i} served in {ms}ms")
    files["logs/payments.log"] = "\n".join(log)

    # Runbook and settings disagree about the staging database port.
    sections = [
        f"## Section {i}\n\nThis section covers subsystem {i}: setup, monitoring, dashboards, "
        f"escalation paths and on-call procedures for team {i}." for i in range(120)
    ]
    sections[64] += "\n\nThe staging database listens on port 6543 and is reset every Sunday."
    sections[77] += "\n\nOn-call owner for the ledger reconciler: Priya Raman, pager 4471."
    files["docs/RUNBOOK.md"] = "# Runbook\n\n" + "\n\n".join(sections)
    settings = ["# Service settings", ""]
    for f in range(40):
        settings.append(f"service_{f}:")
        settings += [f"  timeout_{f}_{j}: {rng.randint(5, 60)}" for j in range(8)]
    settings += ["retries:", "  default: 3", "  payments: 5", "  search: 2", "",
                 "payments_timeout: 30", "", "staging:", "  db_host: staging-db.internal", "  db_port: 6544",
                 "  reset: weekly", ""]
    files["config/settings.yaml"] = "\n".join(settings)
    files["README.md"] = "# Shop backend\n\nServices in src/services, payments in src/payments, docs in docs/.\n"

    # Orders API: four refunds among 500 mixed rows.
    orders = []
    refunded = {88, 203, 317, 441}
    for i in range(500):
        order: dict[str, Any] = {"order_id": 5000 + i,
                                 "status": "refunded" if i in refunded else rng.choice(["shipped", "delivered", "processing"]),
                                 "total": round(10 + rng.random() * 200, 2)}
        if i % 4 == 0:
            order["coupon"] = f"SAVE{i % 30}"
        orders.append(order)
    api = {"https://shop.internal/api/orders": json.dumps(orders, indent=2)}
    return files, api


@dataclass
class Task:
    name: str
    prompt: str
    expected: list[str]


def build_tasks(files: dict[str, str], api: dict[str, str]) -> list[Task]:
    orders = json.loads(api["https://shop.internal/api/orders"])
    refunds = [o for o in orders if o["status"] == "refunded"]
    total = f"{sum(o['total'] for o in refunds):.2f}"
    coupon = [o for o in orders if o.get("coupon") == "SAVE8"]
    coupon_total = f"{sum(o['total'] for o in coupon):.2f}"
    return [
        Task("gateway_error",
             "Some payments are failing with a gateway error, not a card decline. Find the gateway error code "
             "in the payment logs, then find the function in the source that raises it. Answer with the code, "
             "the function name and its file.",
             ["PG-4012", "charge_with_retry", "gateway.py"]),
        Task("refunds",
             "Using the orders API at https://shop.internal/api/orders, list every refunded order and the total "
             "amount refunded. Answer with the order ids and the total.",
             [str(o["order_id"]) for o in refunds] + [total]),
        Task("staging_port",
             "The runbook in docs/ and config/settings.yaml disagree about the staging database port. "
             "What port does each one give?",
             ["6543", "6544"]),
        Task("hardcoded_timeout",
             "One service in src/services hard-codes its timeout as a number instead of reading a setting. "
             "Find it, then look up the value of the setting it should use in config/settings.yaml. "
             "Answer with the file, the hard-coded value and the setting's value.",
             ["svc_17", "4711", "30"]),
        # Harder: each answer sits where a compressor drops or splits content.
        Task("coupon_orders",
             "Which orders in https://shop.internal/api/orders used the coupon SAVE8? Answer with every "
             "order id and their combined total.",
             [str(o["order_id"]) for o in coupon] + [coupon_total]),
        Task("slowest_request",
             "What was the slowest request in logs/payments.log, and how long did it take? "
             "Answer with the request number and its time.",
             ["977", "2950"]),
        Task("runbook_owner",
             "According to docs/RUNBOOK.md, who is the on-call owner for the ledger reconciler, "
             "and what is their pager number?",
             ["Priya Raman", "4471"]),
        Task("retry_settings",
             "How many retries does the payments service get according to config/settings.yaml, "
             "and how many does search get? Then tell me which function in src/payments retries "
             "charges and how many attempts it makes by default.",
             [r"re:\b5\b", r"re:\b2\b", "charge_with_retry", r"re:\b3\b"]),
    ]


def build_aiohttp_workspace() -> tuple[dict[str, str], dict[str, str], list[Task]]:
    """Real code: the installed aiohttp package (a dependency, so always present).

    The expected answers are read out of the source with regexes, so the
    tasks follow whatever aiohttp version is installed. A task whose regex
    no longer matches is left out rather than scored against a stale answer.
    """
    import aiohttp

    root = Path(aiohttp.__file__).parent
    files = {f"aiohttp/{p.relative_to(root)}": p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(root.rglob("*.py"))}

    def grab(path: str, pattern: str) -> str | None:
        m = re.search(pattern, files.get(f"aiohttp/{path}", ""))
        return m.group(1) if m else None

    tasks: list[Task] = []
    limit = grab("connector.py", r"\blimit: int = (\d+)")
    per_host = grab("connector.py", r"\blimit_per_host: int = (\d+)")
    keepalive = grab("connector.py", r"keepalive_timeout = (\d+)(?:\.0)?\b")
    if limit and per_host and keepalive:
        tasks.append(Task("connector_defaults",
                          "In this aiohttp source, what are a connector's default total connection limit and "
                          "per-host limit, and its default keep-alive timeout in seconds?",
                          [rf"re:\b{limit}\b", rf"re:\b{per_host}\b", rf"re:\b{keepalive}\b"]))
    statuses = grab("client.py", r"resp\.status in \(([\d, ]+)\) and allow_redirects")
    if statuses:
        tasks.append(Task("redirect_statuses",
                          "Which HTTP status codes make aiohttp's ClientSession follow a redirect?",
                          [rf"re:\b{code.strip()}\b" for code in statuses.split(",")]))
    if "# For 301 and 302, mimic IE" in files.get("aiohttp/client.py", ""):
        # Only a comment says why: the code compressor removes it.
        tasks.append(Task("redirect_reason",
                          "When following a 301 or 302 redirect for a POST, aiohttp switches to GET. "
                          "According to the comment in the source, which browser's behaviour does it mimic?",
                          [r"re:\bIE\b|Internet Explorer"]))
    total = grab("client.py", r"DEFAULT_TIMEOUT: Final\[ClientTimeout\] = ClientTimeout\(total=([^,]+),")
    connect = grab("client.py", r"DEFAULT_TIMEOUT: Final\[ClientTimeout\] = ClientTimeout\(total=[^,]+, sock_connect=(\d+)")
    if total and connect and re.fullmatch(r"[\d\s*]+", total):
        seconds = math.prod(int(n) for n in total.split("*"))  # e.g. "5 * 60"
        tasks.append(Task("default_timeout",
                          "What is the default total timeout of an aiohttp ClientSession, in seconds, and its "
                          "default socket-connect timeout?",
                          [rf"re:\b{seconds}\b", rf"re:\b{connect}\b"]))
    if "RFC 7616" in files.get("aiohttp/client_middleware_digest_auth.py", ""):
        tasks.append(Task("digest_rfc",
                          "Which RFC does aiohttp's digest authentication middleware follow?",
                          ["7616"]))
    return files, {}, tasks


TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List every file under a directory, with its size in bytes.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory, default ."}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a whole file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Search files for a regular expression, like `rg -n`. "
                                         "Returns path:line:text for every match.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string", "description": "Directory or file, default ."}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "http_get", "description": "GET a URL and return the response body.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]


def run_tool(name: str, raw_args: str, files: dict[str, str], api: dict[str, str]) -> str:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "Error: arguments must be a JSON object."
    prefix = str(args.get("path") or ".").strip().strip("/")
    prefix = "" if prefix in (".", "") else prefix

    def under(p: str) -> bool:
        return not prefix or p == prefix or p.startswith(prefix + "/")

    if name == "list_files":
        hits = [f"{p}  ({len(c):,} bytes)" for p, c in sorted(files.items()) if under(p)]
        return "\n".join(hits) or f"No files under {prefix or '.'}"
    if name == "read_file":
        path = str(args.get("path", "")).strip().lstrip("./")
        return files.get(path, f"Error: {path} not found.")
    if name == "search":
        try:
            pattern = re.compile(str(args.get("pattern", "")))
        except re.error as e:
            return f"Error: bad regex: {e}"
        out = [f"{p}:{n}:{line}" for p, c in sorted(files.items()) if under(p)
               for n, line in enumerate(c.split("\n"), 1) if pattern.search(line)]
        return "\n".join(out) or "No matches."
    if name == "http_get":
        url = str(args.get("url", "")).split("?")[0].rstrip("/")
        return api.get(url, f"HTTP 404 for {url}")
    return f"Error: unknown tool {name}."


# ── Agent loop ──

async def stream_call(session, url: str, body: dict, headers: dict) -> dict:
    """One streamed chat completion, reassembled."""
    content, reasoning, calls, usage = [], [], {}, {}
    retrievals, finish, error = 0, None, None
    async with session.post(url, json=body, headers=headers) as resp:
        if resp.status != 200 or "text/event-stream" not in resp.content_type:
            return {"error": f"HTTP {resp.status}: {(await resp.text())[:300]}"}
        buffer = ""
        # Incremental: a multi-byte character can be split across chunks.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        async for data, _ in resp.content.iter_chunks():
            buffer += decoder.decode(data)
            events, buffer = split_events(buffer)
            for event in events:
                m = re.match(r": proteus retrievals=(\d+)", event)
                if m:
                    retrievals = int(m.group(1))
                if not event.startswith("data:") or event[5:].strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(event[5:])
                except ValueError:
                    continue
                if "error" in chunk:
                    error = json.dumps(chunk["error"])[:300]
                if isinstance(chunk.get("usage"), dict):
                    add_usage(usage, chunk["usage"])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        call["id"] = tc.get("id") or call["id"]
                        fn = tc.get("function") or {}
                        call["name"] = call["name"] or fn.get("name") or ""
                        call["arguments"] += fn.get("arguments") or ""
                    finish = choice.get("finish_reason") or finish
    return {"content": "".join(content), "reasoning": "".join(reasoning), "error": error,
            "tool_calls": [calls[i] for i in sorted(calls)], "usage": usage,
            "retrievals": retrievals, "finish": finish}


async def run_agent(session, url: str, headers: dict, model: str, task: Task, files, api,
                    max_calls: int, verbose: str | None) -> dict:
    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent working in a repository. Use the tools to "
                                      "investigate, then answer concisely."},
        {"role": "user", "content": task.prompt},
    ]
    headers = {**headers, "x-opencode-session": f"agent-eval-{uuid.uuid4().hex}"}
    usage: dict = {}
    model_calls = tool_calls = retrievals = 0
    answer, outcome, error = "", "max_calls", None
    start = time.monotonic()
    while model_calls < max_calls:
        body = {"model": model, "stream": True, "temperature": 0, "max_tokens": 4000,
                "messages": messages, "tools": TOOLS}
        reply = None
        for attempt in range(4):
            try:
                reply = await stream_call(session, url, body, headers)
            except (OSError, asyncio.TimeoutError) as e:
                reply = {"error": f"{type(e).__name__}: {e}"}
            err = reply.get("error") or ""
            if not err or not re.search(r"HTTP (429|5\d\d)|Error|Timeout", err):
                break
            await asyncio.sleep(2 ** attempt * 2)
        model_calls += 1
        if reply.get("error") and not reply.get("tool_calls") and not reply.get("content"):
            outcome, error = "error", reply["error"]
            break
        add_usage(usage, reply["usage"])
        retrievals += reply["retrievals"]
        if not reply["tool_calls"]:
            answer = reply["content"]
            outcome = "answered"
            break
        assistant: dict = {"role": "assistant", "content": reply["content"] or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in reply["tool_calls"]]}
        if reply["reasoning"]:
            assistant["reasoning_content"] = reply["reasoning"]  # DeepSeek thinking mode wants it back
        messages.append(assistant)
        for c in reply["tool_calls"]:
            tool_calls += 1
            result = run_tool(c["name"], c["arguments"], files, api)
            if verbose:
                print(f"  [{verbose}] {c['name']}({c['arguments'][:80]}) -> {len(result):,} chars", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    found = sum(matches(answer, e) for e in task.expected)
    if outcome == "answered":
        outcome = "correct" if found == len(task.expected) else "wrong"
    return {"model_calls": model_calls, "tool_calls": tool_calls, "retrievals": retrievals,
            "usage": usage, "answer": answer.strip()[:300], "outcome": outcome,
            "recall": round(found / len(task.expected), 2), "error": error,
            "seconds": round(time.monotonic() - start, 1)}


def matches(answer: str, expected: str) -> bool:
    """Case-insensitive substring, or a regex when written as "re:<pattern>".

    Thousands separators in the answer are ignored: "2,950 ms" is 2950.
    """
    answer = re.sub(r"(?<=\d),(?=\d{3}\b)", "", answer)
    if expected.startswith("re:"):
        return re.search(expected[3:], answer, re.IGNORECASE) is not None
    return expected.casefold() in answer.casefold()


def cost(model: str, usage: dict) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_cached, p_out = PRICES[model]
    prompt = usage.get("prompt_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return ((prompt - cached) * p_in + cached * p_cached + usage.get("completion_tokens", 0) * p_out) / 1e6


async def live(args, tasks: list[Task], files, api) -> int:
    import aiohttp
    from aiohttp import web

    from proteus.proxy.server import create_app

    backend = get_backend(args.backend, upstream_url=args.upstream_url, api_key_env=args.api_key_env)
    if not backend.api_key:
        print(f"error: {backend.api_key_env} is not set", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {backend.api_key}", "Content-Type": "application/json",
               "User-Agent": "proteus-agent-eval/1.0"}
    app = create_app(backend=backend)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    proxy_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1/chat/completions"
    modes = {"direct": f"{backend.upstream_url}/chat/completions", "proteus": proxy_url}
    limit = asyncio.Semaphore(args.concurrency)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)

    async def one(task: Task, mode: str, trial: int) -> dict:
        async with limit:
            tag = f"{task.name}/{mode}#{trial}" if args.verbose else None
            row = await run_agent(session, modes[mode], headers, args.model, task, files, api, args.max_calls, tag)
        row.update(task=task.name, mode=mode, trial=trial, cost=cost(args.model, row["usage"]))
        if args.verbose:
            print(f"[{task.name}/{mode}#{trial}] {row['outcome']} calls={row['model_calls']} "
                  f"tools={row['tool_calls']} retrieves={row['retrievals']} :: {row['answer'][:100]!r}",
                  file=sys.stderr)
        return row

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            rows = await asyncio.gather(*(one(t, m, i) for i in range(args.repeat) for t in tasks for m in modes))
    finally:
        await runner.cleanup()
    report(rows, args.model, backend.name, args.repeat)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nRaw results: {args.json}")
    return 0


def _avg(values: list) -> float:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def report(rows: list[dict], model: str, backend: str, repeat: int) -> None:
    def cached(r: dict) -> int:
        return (r["usage"].get("prompt_tokens_details") or {}).get("cached_tokens", 0)

    print(f"\nModel: {model} via {backend}, {repeat} run(s) per task and mode, streaming\n")
    print("| task | mode | correct | model calls | tool calls | prompt tokens | cached | completion | cost/run | retrieves |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for task in dict.fromkeys(r["task"] for r in rows):
        for mode in ("direct", "proteus"):
            rs = [r for r in rows if r["task"] == task and r["mode"] == mode]
            other = {k: sum(r["outcome"] == k for r in rs) for k in ("wrong", "max_calls", "error")}
            extra = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in other.items() if v)
            prompt = _avg([r["usage"].get("prompt_tokens") for r in rs])
            share = _avg([cached(r) / r["usage"]["prompt_tokens"] for r in rs if r["usage"].get("prompt_tokens")])
            c = _avg([r["cost"] for r in rs])
            print(f"| {task} | {mode} | {sum(r['outcome'] == 'correct' for r in rs)}/{len(rs)}"
                  f"{f' ({extra})' if extra else ''} | {_avg([r['model_calls'] for r in rs]):.1f} "
                  f"| {_avg([r['tool_calls'] for r in rs]):.1f} | {prompt:,.0f} | {share:.0%} "
                  f"| {_avg([r['usage'].get('completion_tokens') for r in rs]):,.0f} "
                  f"| ${c:.5f} | {_avg([r['retrievals'] for r in rs]):.1f} |")
    print()
    for mode in ("direct", "proteus"):
        rs = [r for r in rows if r["mode"] == mode]
        prompt = sum(r["usage"].get("prompt_tokens", 0) for r in rs)
        cache = sum(cached(r) for r in rs)
        total_cost = sum(r["cost"] or 0 for r in rs)
        print(f"{mode}: {sum(r['outcome'] == 'correct' for r in rs)}/{len(rs)} correct; "
              f"{prompt:,} prompt tokens ({cache / prompt if prompt else 0:.0%} cached), "
              f"{sum(r['usage'].get('completion_tokens', 0) for r in rs):,} completion; est. ${total_cost:.4f}")
    bad = [r for r in rows if r["outcome"] != "correct"]
    if bad:
        print("\nNot correct:")
    for r in bad:
        print(f"  {r['task']}/{r['mode']}#{r['trial']}: {r['outcome']} {r['error'] or repr(r['answer'][:120])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", default="opencode-go", choices=list(list_backends()))
    parser.add_argument("--upstream-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--model")
    parser.add_argument("--workspace", default="synthetic", choices=["synthetic", "aiohttp"],
                        help="synthetic repo (default), or the installed aiohttp source as real code")
    parser.add_argument("--task", action="append", help="run only this task (repeatable)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=15, help="model calls per run before giving up")
    parser.add_argument("--json")
    parser.add_argument("--show-workspace", action="store_true", help="print files, tasks and answers; no API calls")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.workspace == "aiohttp":
        files, api, tasks = build_aiohttp_workspace()
    else:
        files, api = build_workspace()
        tasks = build_tasks(files, api)
    if args.task:
        unknown = set(args.task) - {t.name for t in tasks}
        if unknown:
            parser.error(f"unknown task(s): {', '.join(sorted(unknown))}")
        tasks = [t for t in tasks if t.name in args.task]
    if args.show_workspace:
        print(f"{len(files)} files, {sum(map(len, files.values())):,} chars; API: "
              + ", ".join(f"{u} ({len(b):,} chars)" for u, b in api.items()))
        for t in tasks:
            print(f"\n{t.name}: {t.prompt}\n  expected: {t.expected}")
        return 0
    if not args.model:
        parser.error("--model is required")
    return asyncio.run(live(args, tasks, files, api))


if __name__ == "__main__":
    sys.exit(main())

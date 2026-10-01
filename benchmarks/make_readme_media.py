#!/usr/bin/env python3
"""Generate the README's screenshots and proxy GIF from real Proteus output.

Nothing here is drawn by hand. The screenshots show what the proxy's own
request transform sends to the model for three tool outputs. The GIF is a
real run of the proxy against a local stand-in model. The stand-in calls
proteus_retrieve when it sees the marker and answers from what it gets back,
so the run needs no network or API key. Everything the proxy does in it
(compressing, answering the retrieve, relaying the reply) is the real code
path.

Re-run after any compressor or marker change so the README can't drift:

    pip install pillow
    python benchmarks/make_readme_media.py

Writes docs/media/{logs,json,python}.png and docs/media/proxy.gif.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_demo_gif import font  # noqa: E402


def _load_live_eval():
    """The live-eval scenarios are the README's example data."""
    spec = importlib.util.spec_from_file_location("live_eval", Path(__file__).with_name("live_eval.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_eval"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


live_eval = _load_live_eval()  # also points the CCR cache at a temp dir

from proteus.proxy.handler import transform_request_body  # noqa: E402

# GitHub dark palette
BG = (13, 17, 23)
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
FG = (201, 209, 217)
DIM = (110, 118, 129)
WHITE = (240, 246, 252)
GREEN = (63, 185, 80)
YELLOW = (210, 153, 34)
RED = (248, 81, 73)
BLUE = (88, 166, 255)
PURPLE = (188, 140, 255)

FNT = font(14)
TITLE_FNT = font(15)
CW = FNT.getlength("M")
LINE_H = 19
PAD = 16


def colour_for(line: str) -> tuple[int, int, int]:
    """Syntax-ish colouring so the eye finds what matters."""
    s = line.strip()
    if s.startswith("[proteus:") or s.startswith("...  # proteus") or "# proteus:" in s:
        return BLUE
    if "ERROR" in line or "Traceback" in line:
        return RED
    if s.startswith("[x") or s.startswith("[last]") or s.startswith("... ") or s.startswith("[SHOWING"):
        return YELLOW
    if s.startswith("#") or s.startswith("//"):
        return DIM
    if s.startswith(("def ", "async def ", "class ")):
        return PURPLE
    return FG


def wrap(lines: list[str], cols: int) -> list[str]:
    """Wrap the proteus marker (it must be readable whole); clip everything else."""
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line.startswith("[proteus:"):
            out.append(clip(line, cols))
            continue
        words, cur = line.split(" "), ""
        for word in words:
            if cur and len(cur) + 1 + len(word) > cols:
                out.append(cur)
                cur = " " + word
            else:
                cur = f"{cur} {word}" if cur else word
        out.append(cur)
    return out


def clip(line: str, cols: int) -> str:
    return line if len(line) <= cols else line[: cols - 1] + "…"


def panel(title: str, subtitle: str, lines: list[str], cols: int, rows: int, wrap_long: bool = False) -> Image.Image:
    body = wrap(lines, cols) if wrap_long else [clip(ln, cols) for ln in lines]
    if len(body) > rows:
        hidden = len(body) - rows + 1
        body = body[: rows - 1] + [f"   …  {hidden:,} more lines"]
    w = int(PAD * 2 + cols * CW)
    h = int(PAD * 2 + 30 + rows * LINE_H)
    img = Image.new("RGB", (w, h), PANEL)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=BORDER)
    d.text((PAD, PAD - 2), title, font=TITLE_FNT, fill=WHITE)
    d.text((PAD + TITLE_FNT.getlength(title + "  "), PAD - 2), subtitle, font=TITLE_FNT, fill=DIM)
    y = PAD + 30
    for line in body:
        d.text((PAD, y), line, font=FNT, fill=DIM if line.startswith("   …") else colour_for(line))
        y += LINE_H
    return img


def side_by_side(left: Image.Image, right: Image.Image, arrow: str) -> Image.Image:
    gap = 70
    w = left.width + right.width + gap + 2 * PAD
    h = max(left.height, right.height) + 2 * PAD
    img = Image.new("RGB", (w, h), BG)
    img.paste(left, (PAD, PAD))
    img.paste(right, (PAD + left.width + gap, PAD))
    d = ImageDraw.Draw(img)
    cx, cy = PAD + left.width + gap // 2, PAD + 70
    d.text((cx - TITLE_FNT.getlength("→") / 2, cy - 12), "→", font=font(26), fill=GREEN)
    d.text((cx - TITLE_FNT.getlength(arrow) / 2, cy + 22), arrow, font=TITLE_FNT, fill=GREEN)
    return img


def sent_to_model(tool_output: str) -> str:
    body = {
        "messages": [{"role": "tool", "tool_call_id": "t", "content": tool_output}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    }
    transformed, _, _ = transform_request_body(body)
    return transformed["messages"][0]["content"]


def screenshot(name: str, label: str, tool_output: str, rows: int = 22, cols: int = 62) -> None:
    model_sees = sent_to_model(tool_output)
    pct = round((1 - len(model_sees) / len(tool_output)) * 100)
    left = panel("Tool output", f"{label} · {len(tool_output):,} chars", tool_output.split("\n"), cols, rows)
    right = panel("Sent to the model", f"{len(model_sees):,} chars", model_sees.split("\n"), cols, rows, wrap_long=True)
    side_by_side(left, right, f"−{pct}%").save(OUT / f"{name}.png", optimize=True)
    print(f"  {name}.png  {len(tool_output):,} → {len(model_sees):,} chars")


def python_source() -> tuple[str, str]:
    """A real, large Python file: aiohttp's client.py when available."""
    try:
        import aiohttp

        path = Path(aiohttp.__file__).with_name("client.py")
        return path.read_text(), "aiohttp/client.py"
    except (ImportError, OSError):  # pragma: no cover - aiohttp is a dependency
        return Path(__file__).read_text() * 6, Path(__file__).name


def skeleton_excerpt(src: str) -> str:
    """Start both Python panels at the same method, so the hidden bodies line up."""
    marker = "    def __init_subclass__"
    i = src.find(marker)
    return src[i:] if i != -1 else src


# ── Proxy GIF ─────────────────────────────────────────────────────────────


async def proxy_run(model_id: str | None = None) -> dict:
    """One real request through the proxy.

    With model_id, the proxy talks to that model on OpenCode Go (needs
    OPENCODE_GO_API_KEY) through a local relay that records each request, so
    the GIF can show what the model asked for. The same request also goes
    once straight to the API, for a real token comparison. Without model_id,
    a local stand-in model plays the model's part.
    """
    import uuid

    from aiohttp import ClientSession, web

    from proteus.proxy.backends import get_backend
    from proteus.proxy.server import create_app

    scenario = next(s for s in live_eval._scenarios() if s.name == "json_dropped_rows")
    seen: list[dict] = []
    real = get_backend("opencode-go") if model_id else None
    if real is not None and not real.api_key:
        raise SystemExit(f"--model needs {real.api_key_env}")
    client_headers = {"User-Agent": "proteus-readme-media/1.0",
                      "x-opencode-session": f"readme-media-{uuid.uuid4().hex}"}

    async def stand_in(body: dict) -> dict:
        text = body["messages"][-1].get("content") or ""
        marker = re.search(r'proteus_retrieve\(hash="(\w+)"\)', text)
        if marker:  # compressed output: ask for the rows that matter
            args = {"hash": marker.group(1), "query": "refunded"}
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "r1", "type": "function",
                    "function": {"name": "proteus_retrieve", "arguments": json.dumps(args)}}]}}]}
        found = re.search(r'"order_id": (\d+)', text)
        answer = f"Order {found.group(1)} is the refunded one." if found else "I can't tell."
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]}

    async with ClientSession() as http:

        async def relay(request: web.Request) -> web.Response:
            body = await request.json()
            seen.append(body)
            if real is None:
                return web.json_response(await stand_in(body))
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() in ("authorization", "content-type", "user-agent")
                       or "session" in k.lower()}
            async with http.post(f"{real.upstream_url}/chat/completions", json=body, headers=headers) as r:
                return web.Response(body=await r.read(), status=r.status, content_type="application/json")

        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", relay)
        up_runner = web.AppRunner(upstream)
        await up_runner.setup()
        up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
        await up_site.start()
        up_port = up_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        key_env = real.api_key_env if real else "PROTEUS_MEDIA_KEY"
        os.environ.setdefault(key_env, "unused")
        app = create_app(backend="generic", upstream_url=f"http://127.0.0.1:{up_port}/v1", api_key_env=key_env)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        request = live_eval.build_body(scenario, model_id or "stand-in")
        async with http.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=request,
                             headers=client_headers) as resp:
            reply = await resp.json(content_type=None)
            retrievals = resp.headers.get("X-Proteus-Retrievals", "0")
        await runner.cleanup()
        await up_runner.cleanup()
        if resp.status != 200:
            raise SystemExit(f"proxied request failed: HTTP {resp.status}: {json.dumps(reply)[:300]}")

        direct_tokens = None
        if real is not None:
            direct = {**client_headers, "Authorization": f"Bearer {real.api_key}",
                      "x-opencode-session": f"readme-media-{uuid.uuid4().hex}"}
            async with http.post(f"{real.upstream_url}/chat/completions", json=request, headers=direct) as r:
                direct_tokens = ((await r.json(content_type=None)).get("usage") or {}).get("prompt_tokens")

    calls = [tc for b in seen[1:] for tc in (b["messages"][-2].get("tool_calls") or [])
             if b["messages"][-2].get("role") == "assistant"]
    retrieves = [json.loads(tc["function"]["arguments"]) for tc in calls
                 if tc["function"]["name"] == "proteus_retrieve"]
    results = [b["messages"][-1]["content"] for b in seen[1:]]
    compressed = seen[0]["messages"][-1]["content"]
    return {
        "model": model_id or "local stand-in model",
        "original": len(scenario.output),
        "compressed": len(compressed),
        "hash": re.search(r'hash="(\w+)"', compressed).group(1),  # type: ignore[union-attr]
        "marker": compressed.split("\n")[-1],
        "retrieves": retrieves,
        "results": [r.split("\n") for r in results],
        "answer": (reply["choices"][0]["message"].get("content") or "").strip(),
        "retrievals": retrievals,
        "proxied_tokens": (reply.get("usage") or {}).get("prompt_tokens"),
        "direct_tokens": direct_tokens,
        "direct_chars": len(json.dumps(request)),
        "proxied_chars": sum(len(json.dumps(b)) for b in seen),
        "rounds": len(seen),
    }


def proxy_gif(run: dict) -> None:
    cols = 96
    def frame(lines: list[tuple[str, tuple[int, int, int]]]) -> Image.Image:
        img = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(img)
        y = PAD
        for text, colour in lines:
            d.text((PAD, y), clip(text, cols), font=FNT, fill=colour)
            y += LINE_H
        return img

    marker = run["marker"]
    marker_short = re.sub(r"\. For the full original.*", ". …]", marker)
    answer = " ".join(run["answer"].split())
    script: list[tuple[str, tuple[int, int, int]]] = [
        (f"$ proteus proxy --backend opencode-go          # model: {run['model']}", WHITE),
        ("  proxy on http://127.0.0.1:8787 — point your agent's base URL here", DIM),
        ("", FG),
        ("agent   → POST /v1/chat/completions   \"Which order_id has status 'refunded'?\"", BLUE),
        (f"          last tool result: GET /api/orders  ({run['original']:,} chars, 500 orders)", FG),
        (f"proteus   compressed to {run['compressed']:,} chars, original cached as {run['hash']}", GREEN),
        (f"          {marker_short}", DIM),
        ("", FG),
    ]
    for args, result in zip(run["retrieves"], run["results"], strict=False):
        call = ", ".join(f'{k}="{v}"' for k, v in args.items())
        script += [
            ("model   → calls the tool from the marker:", BLUE),
            (f"          proteus_retrieve({call})", PURPLE),
            ("proteus   answers from its local cache; the agent never sees this:", GREEN),
            *[(f"          {ln}", FG) for ln in result[:2]],
            ("", FG),
        ]
    script.append((f"model   → \"{answer}\"", BLUE))
    script.append(("", FG))
    script.append((f"agent   ← 200 OK   X-Proteus-Retrievals: {run['retrievals']}   (never saw proteus_retrieve)", WHITE))
    if run["direct_tokens"] and run["proxied_tokens"]:
        script.append((f"          prompt tokens billed: {run['proxied_tokens']:,} through Proteus"
                       f"  vs  {run['direct_tokens']:,} direct", YELLOW))
    else:
        script.append((f"          sent upstream: {run['proxied_chars']:,} chars over {run['rounds']} rounds"
                       f"  vs  {run['direct_chars']:,} chars direct", YELLOW))
    rows = len(script)
    w = int(PAD * 2 + cols * CW)
    h = int(PAD * 2 + rows * LINE_H)

    # Reveal the script a step at a time: each "speaker" line starts a new frame.
    frames: list[Image.Image] = []
    durations: list[int] = []
    shown: list[tuple[str, tuple[int, int, int]]] = []
    for text, colour in script:
        shown.append((text, colour))
        if text.startswith(("$", "agent", "proteus", "model")) or text.lstrip().startswith(("sent upstream", "prompt tokens")):
            frames.append(frame(shown))
            durations.append(1100)
    frames.append(frame(shown))
    durations.append(4500)  # hold the result
    # Open on the finished picture: GitHub shows only the first frame to
    # readers with reduced motion, and static previews show nothing else.
    frames.insert(0, frames[-1])
    durations.insert(0, 3500)
    frames[0].save(OUT / "proxy.gif", save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True)
    print(f"  proxy.gif  {len(frames)} frames; model {run['model']}; {run['retrievals']} retrieve(s); "
          f"tokens {run['proxied_tokens']} vs {run['direct_tokens']} direct; answer: {run['answer']!r}")


def main(model_id: str | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    scenarios = {s.name: s for s in live_eval._scenarios()}
    screenshot("logs", "kubectl logs", scenarios["log_errors"].output)
    screenshot("json", "GET /api/products", scenarios["json_columnar"].output)
    src, label = python_source()
    left_src = skeleton_excerpt(src)
    # The skeleton is computed on the whole file (that's what the proxy sends);
    # both panels then start at the same class so they line up.
    model_sees = sent_to_model(src)
    right = skeleton_excerpt(model_sees)
    pct = round((1 - len(model_sees) / len(src)) * 100)
    cols, rows = 62, 22
    side_by_side(
        panel("Tool output", f"{label} · {len(src):,} chars", left_src.split("\n"), cols, rows),
        panel("Sent to the model", f"{len(model_sees):,} chars", right.split("\n"), cols, rows, wrap_long=True),
        f"−{pct}%",
    ).save(OUT / "python.png", optimize=True)
    print(f"  python.png  {len(src):,} → {len(model_sees):,} chars")
    proxy_gif(asyncio.run(proxy_run(model_id)))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="real model on OpenCode Go for the proxy GIF (needs OPENCODE_GO_API_KEY)")
    model_id = parser.parse_args().model
    main(model_id)

"""Proteus proxy — aiohttp server.

Transparent compression proxy that sits between any OpenAI-compatible
client and API backend. Compresses large tool outputs inline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from proteus.proxy.handler import transform_request_body

logger = logging.getLogger(__name__)

# ── Upstream URLs ──
BACKEND_URLS = {
    "openrouter": os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "openai": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "generic": None,  # Must be set via --upstream-url
}

# ── API key env vars ──
BACKEND_API_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "generic": "OPENROUTER_API_KEY",  # fallback
}


class ProteusProxy:
    """aiohttp-based transparent compression proxy."""

    def __init__(
        self,
        backend: str = "openrouter",
        upstream_url: str | None = None,
        config_path: str | None = None,
        log_file: str | None = None,
    ):
        self.backend = backend
        self.upstream_url = upstream_url or BACKEND_URLS.get(backend, "")
        self.config_path = config_path
        self.log_file = log_file
        self._session: aiohttp.ClientSession | None = None
        self._stats = {
            "requests_total": 0,
            "requests_compressed": 0,
            "chars_saved": 0,
            "tokens_saved": 0,
            "start_time": time.time(),
        }

    async def _get_upstream_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _process_and_forward(self, body: dict, request_headers) -> web.Response:
        """Core logic: compress body tool results and forward to upstream.

        Extracted from handle_chat_completions for testability.
        Takes a parsed JSON body dict and original request headers.
        Returns a web.Response (JSON, status 200/502).
        """
        # Transform: compress tool results
        start = time.time()
        mod_body, ccr_lookup, cstats = transform_request_body(body)
        transform_time = time.time() - start

        # Forward to upstream
        upstream = await self._get_upstream_session()
        api_key = os.environ.get(BACKEND_API_KEY_ENV.get(self.backend, ""), "")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        # Pass through any extra headers from original request
        for h in ["X-Title", "HTTP-Referer"]:
            if h in request_headers:
                headers[h] = request_headers[h]

        upstream_url = f"{self.upstream_url}/chat/completions"

        try:
            start_fwd = time.time()
            async with upstream.post(
                upstream_url,
                json=mod_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                fwd_time = time.time() - start_fwd
                response_data = await resp.json()

                # Log if configured
                if self.log_file:
                    self._write_log_entry({
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "status": resp.status,
                        "transform_ms": round(transform_time * 1000),
                        "forward_ms": round(fwd_time * 1000),
                        "compressed": cstats["compressed"],
                        "total_saved": cstats["total_saved"],
                        "tool_calls": cstats.get("tool_calls_count", 0),
                    })

                # Update stats
                self._stats["requests_compressed"] += 1 if cstats["compressed"] > 0 else 0
                self._stats["chars_saved"] += cstats["total_saved"]
                self._stats["tokens_saved"] += cstats["total_tokens_saved"]

                return web.json_response(response_data, status=resp.status)

        except aiohttp.ClientError as e:
            logger.error("Upstream request failed: %s", e)
            return web.json_response(
                {"error": f"Upstream request failed: {str(e)}"}, status=502
            )

    async def handle_chat_completions(self, request: web.Request) -> web.Response:
        """Handle POST /v1/chat/completions — compress + forward."""
        self._stats["requests_total"] += 1

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400
            )

        return await self._process_and_forward(body, request.headers)

    async def handle_livez(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "service": "proteus-proxy",
            "status": "healthy",
            "alive": True,
            "uptime_seconds": round(time.time() - self._stats["start_time"]),
        })

    async def handle_readyz(self, request: web.Request) -> web.Response:
        """Readiness check endpoint."""
        upstream_ok = False
        if self.upstream_url:
            upstream_ok = True  # Assume up — we'll know on first request
        return web.json_response({
            "service": "proteus-proxy",
            "status": "healthy",
            "ready": True,
            "checks": {
                "upstream": {
                    "url": self.upstream_url,
                    "status": "ok" if upstream_ok else "not_configured",
                },
            },
            "stats": {
                "requests_total": self._stats["requests_total"],
                "requests_compressed": self._stats["requests_compressed"],
                "chars_saved": self._stats["chars_saved"],
                "tokens_saved_estimate": self._stats["tokens_saved"],
            },
        })

    async def handle_unknown(self, request: web.Request) -> web.Response:
        """Handle unknown paths by proxying to upstream."""
        upstream = await self._get_upstream_session()
        api_key = os.environ.get(BACKEND_API_KEY_ENV.get(self.backend, ""), "")

        headers = {"Authorization": f"Bearer {api_key}"}
        upstream_url = f"{self.upstream_url}{request.path}"
        if request.query_string:
            upstream_url += f"?{request.query_string}"

        try:
            body = await request.read() if request.can_read_body else None
            async with upstream.request(
                request.method, upstream_url, headers=headers, data=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.read()
                return web.Response(body=data, status=resp.status,
                                    content_type=resp.content_type)
        except aiohttp.ClientError as e:
            return web.json_response(
                {"error": str(e)}, status=502
            )

    def _write_log_entry(self, entry: dict):
        """Write a JSONL log entry."""
        if not self.log_file:
            return
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def create_app(
    backend: str = "openrouter",
    upstream_url: str | None = None,
    config_path: str | None = None,
    log_file: str | None = None,
) -> web.Application:
    """Create the aiohttp application with routes."""
    proxy = ProteusProxy(
        backend=backend,
        upstream_url=upstream_url,
        config_path=config_path,
        log_file=log_file,
    )

    app = web.Application()
    app["proxy"] = proxy

    # Routes
    app.router.add_post("/v1/chat/completions", proxy.handle_chat_completions)
    app.router.add_get("/livez", proxy.handle_livez)
    app.router.add_get("/readyz", proxy.handle_readyz)
    app.router.add_get("/health", proxy.handle_livez)

    # Catch-all: proxy everything else to upstream
    app.router.add_route("*", "/{path:.*}", proxy.handle_unknown)

    return app


def start_proxy(
    host: str = "127.0.0.1",
    port: int = 8787,
    backend: str = "openrouter",
    upstream_url: str | None = None,
    config_path: str | None = None,
    log_file: str | None = None,
):
    """Start the Proteus proxy server (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = create_app(
        backend=backend,
        upstream_url=upstream_url,
        config_path=config_path,
        log_file=log_file,
    )

    print(f"🌊 Proteus proxy running on http://{host}:{port}")
    print(f"   Backend: {backend} -> {app['proxy'].upstream_url}")
    print(f"   Configure your client to use http://{host}:{port}/v1")

    web.run_app(app, host=host, port=port, print=lambda *a, **kw: None)


if __name__ == "__main__":
    # For testing: python -m proteus.proxy.server
    start_proxy()
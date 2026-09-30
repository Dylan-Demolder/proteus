"""Proteus proxy — aiohttp server.

Transparent compression proxy that sits between any OpenAI-compatible
client and API backend. Compresses large tool outputs inline.

Backends: openrouter, opencode-go, openai, generic
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

import aiohttp
from aiohttp import web

from proteus import config
from proteus.proxy.backends import Backend, get_backend
from proteus.proxy.handler import (
    pending_retrieve_calls,
    strip_retrieve_calls,
    transform_request_body,
)

logger = logging.getLogger(__name__)

# How many times one request may loop through proteus_retrieve before the
# answer goes back to the client regardless.
MAX_RETRIEVE_ROUNDS = 3

# No overall deadline: a long generation or a slow SSE stream is normal. What
# we bound is time to connect and time between bytes, which catches a hung
# upstream without cutting off a healthy response.
CHAT_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
PASSTHROUGH_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

try:
    from importlib.metadata import version as _pkg_version

    USER_AGENT = f"proteus/{_pkg_version('proteus-compress')}"
except Exception:  # running from a source checkout
    USER_AGENT = "proteus"

# Client headers relayed upstream as-is. Providers use them to identify the
# client and route or cache per conversation (OpenCode Go rejects requests
# without a session ID), so a proxy that drops them breaks the client.
FORWARD_HEADERS = ("X-Title", "HTTP-Referer", "User-Agent")


def _is_session_header(name: str) -> bool:
    """x-opencode-session, session_id (Codex), x-claude-code-session-id, ..."""
    name = name.lower().replace("_", "-")
    return name.startswith("x-opencode-") or "session" in name


def client_headers(request_headers) -> dict[str, str]:
    """Headers from the client that go upstream unchanged."""
    headers = {h: request_headers[h] for h in FORWARD_HEADERS if h in request_headers}
    headers.setdefault("User-Agent", USER_AGENT)
    for h, v in request_headers.items():
        if _is_session_header(h):
            headers[h] = v
    return headers


def conversation_id(body: dict) -> str:
    """A stable ID for a conversation, from its model and opening messages.

    Every turn of an agent conversation resends the same system prompt and
    first user message, so they hash to the same ID across turns while
    different conversations get different IDs.
    """
    opening = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        opening.append([msg.get("role"), msg.get("content")])
        if msg.get("role") == "user":
            break
    seed = json.dumps([body.get("model"), opening], sort_keys=True, default=str)
    return "proteus-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


class ProteusProxy:
    """aiohttp-based transparent compression proxy.

    Compresses large tool outputs in requests before forwarding to
    the upstream API backend. Supports multiple backends via the
    backend abstraction layer.
    """

    def __init__(
        self,
        backend: str | Backend = "openrouter",
        upstream_url: str | None = None,
        api_key_env: str | None = None,
        config_path: str | None = None,
        log_file: str | None = None,
        profile: str | None = None,
    ):
        # Resolve backend
        if isinstance(backend, Backend):
            self.backend = backend
        else:
            self.backend = get_backend(backend, upstream_url=upstream_url, api_key_env=api_key_env)

        self.config_path = config_path
        self.profile = profile
        self._config_mtime = self._config_file_mtime()
        if config_path or profile:
            # Invalid settings fail here, at startup, rather than on reload.
            config.configure(config_path, profile)
        self.log_file = log_file
        self._session: aiohttp.ClientSession | None = None
        self._stats = {
            "requests_total": 0,
            "requests_compressed": 0,
            "chars_saved": 0,
            "tokens_saved": 0,
            "retrievals_served": 0,
            "start_time": time.time(),
        }

    @property
    def upstream_url(self) -> str:
        return self.backend.upstream_url

    async def _get_upstream_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _config_file_mtime(self) -> float | None:
        if not self.config_path:
            return None
        try:
            return os.stat(self.config_path).st_mtime
        except OSError:
            return None

    def maybe_reload_config(self) -> bool:
        """Re-apply the config file if it changed since it was last loaded.

        One stat() per request. A file that fails to parse or validate is
        logged and ignored: the proxy keeps serving with the settings it had.
        Only compression settings reload; the proxy: section (host, port,
        backend) needs a restart.

        Returns:
            True if new settings were applied.
        """
        mtime = self._config_file_mtime()
        if mtime is None or mtime == self._config_mtime:
            return False
        self._config_mtime = mtime
        try:
            config.configure(self.config_path, self.profile)
        except Exception as e:
            logger.error("Config reload failed, keeping previous settings: %s", e)
            return False
        logger.info("Reloaded config from %s", self.config_path)
        return True

    async def close(self) -> None:
        """Close the upstream connection pool (called on app shutdown)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _forward_stream(self, resp: aiohttp.ClientResponse, request: web.BaseRequest) -> web.StreamResponse:
        """Forward an upstream SSE stream to the client."""
        response = web.StreamResponse(
            status=resp.status,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async for data, _ in resp.content.iter_chunks():
            if data:
                try:
                    await response.write(data)
                    await response.drain()
                except (ConnectionResetError, ConnectionAbortedError):
                    break

        return response

    async def _process_and_forward(self, body: dict, request_headers, request: web.Request | None = None) -> web.StreamResponse:
        """Core logic: compress body tool results and forward to upstream.

        If the model calls proteus_retrieve, the proxy answers the call itself
        and asks again, so the client only ever sees its own tools. This needs
        a complete response to inspect, so it only happens for non-streaming
        requests. Streaming requests are still compressed, but the tool is not
        offered, and the marker just records the cache hash.

        Args:
            body: Parsed JSON request body.
            request_headers: Original request headers.
            request: Original request (needed for streaming SSE responses).

        Returns:
            A web.StreamResponse — web.Response (JSON) for non-streaming,
            web.StreamResponse for SSE.
        """
        is_stream = bool(body.get("stream", False))

        # Compression is about the *request*: tool outputs going to the model.
        # Whether the *response* streams doesn't matter, so both are compressed.
        start = time.time()
        mod_body, _ccr_lookup, cstats = transform_request_body(body, inject_tool=not is_stream)
        transform_time = time.time() - start
        serve_retrieve = bool(cstats.get("injected_tool"))

        self._stats["requests_compressed"] += 1 if cstats["compressed"] > 0 else 0
        self._stats["chars_saved"] += cstats["total_saved"]
        self._stats["tokens_saved"] += cstats["total_tokens_saved"]

        # Apply backend-specific request transformations
        mod_body = self.backend.transform_request(mod_body)

        # Forward to upstream
        upstream = await self._get_upstream_session()
        api_key = self.backend.api_key

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        for h, v in self.backend.extra_headers.items():
            headers[h] = v
        headers.update(client_headers(request_headers))
        session_header = self.backend.session_header
        if session_header and not any(_is_session_header(h) for h in headers):
            headers[session_header] = conversation_id(body)

        upstream_url = f"{self.upstream_url}/chat/completions"
        usage: dict[str, int] = {}
        retrieve_round = 0
        retrievals = 0
        start_fwd = time.time()

        while True:
            try:
                async with upstream.post(
                    upstream_url,
                    json=mod_body,
                    headers=headers,
                    timeout=CHAT_TIMEOUT,
                ) as resp:
                    # Streaming: forward SSE events as-is. An upstream error
                    # arrives as a plain JSON body even for stream=true, so it
                    # falls through and is relayed as JSON, not relabelled SSE.
                    is_sse = "text/event-stream" in resp.content_type
                    if is_sse or (is_stream and resp.status < 400):
                        if request is None:
                            # aiohttp needs the originating client request to
                            # prepare a response on. Without one (direct calls to
                            # _process_and_forward) we can't relay the SSE stream,
                            # so fail loudly instead of raising TypeError inside
                            # StreamResponse.prepare().
                            return web.json_response(
                                {"error": "streaming requires a client request"},
                                status=500,
                            )
                        return await self._forward_stream(resp, request)

                    # Non-streaming: parse JSON response. Upstream errors are
                    # not always JSON (an HTML 502 page from a load balancer,
                    # a plain-text 429). Those are relayed as they came instead
                    # of being replaced with a generic 502.
                    try:
                        response_data = await resp.json(content_type=None)
                    except ValueError:
                        response_data = None
                    if not isinstance(response_data, dict):
                        return web.Response(
                            body=await resp.read(),
                            status=resp.status,
                            content_type=resp.content_type or "text/plain",
                        )
                    status = resp.status
            except asyncio.TimeoutError:
                logger.error("Upstream request timed out")
                return web.json_response({"error": "Upstream request timed out"}, status=504)
            except aiohttp.ClientError as e:
                logger.error("Upstream request failed: %s", e)
                return web.json_response(
                    {"error": f"Upstream request failed: {str(e)}"}, status=502
                )

            if not serve_retrieve or status >= 400:
                break
            followup = pending_retrieve_calls(response_data)
            if followup is None or retrieve_round >= MAX_RETRIEVE_ROUNDS:
                response_data = strip_retrieve_calls(response_data)
                break

            # The model asked for original content. Answer from the cache and
            # ask again. The client never sees this exchange.
            retrieve_round += 1
            _add_usage(usage, response_data)
            assistant, results = followup
            mod_body = {**mod_body, "messages": [*mod_body["messages"], assistant, *results]}
            self._stats["retrievals_served"] += len(results)
            retrievals += len(results)

        fwd_time = time.time() - start_fwd
        if usage:
            # Report what the whole exchange cost, not just the last round.
            _add_usage(usage, response_data)
            response_data["usage"] = {**(response_data.get("usage") or {}), **usage}

        # Log if configured
        if self.log_file:
            self._write_log_entry({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": "POST",
                "path": "/v1/chat/completions",
                "status": status,
                "transform_ms": round(transform_time * 1000),
                "forward_ms": round(fwd_time * 1000),
                "compressed": cstats["compressed"],
                "total_saved": cstats["total_saved"],
                "tool_calls": cstats.get("tool_calls_count", 0),
                "retrieve_rounds": retrieve_round,
            })

        # Apply backend-specific response transformations
        response_data = self.backend.transform_response(response_data)
        # How many proteus_retrieve calls the proxy answered for this request.
        return web.json_response(response_data, status=status,
                                 headers={"X-Proteus-Retrievals": str(retrievals)})

    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        """Handle POST /v1/chat/completions — compress + forward."""
        self._stats["requests_total"] += 1
        self.maybe_reload_config()

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400
            )

        return await self._process_and_forward(body, request.headers, request)

    async def handle_livez(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "service": "proteus-proxy",
            "status": "healthy",
            "alive": True,
            "uptime_seconds": round(time.time() - self._stats["start_time"]),
            "backend": self.backend.name,
            "upstream": self.backend.upstream_url,
        })

    async def handle_readyz(self, request: web.Request) -> web.Response:
        """Readiness check endpoint."""
        upstream_ok = bool(self.backend.upstream_url and self.backend.api_key)
        return web.json_response({
            "service": "proteus-proxy",
            "status": "healthy",
            "ready": True,
            "backend": self.backend.name,
            "checks": {
                "upstream": {
                    "url": self.backend.upstream_url,
                    "status": "ok" if upstream_ok else "not_configured",
                    "api_key_set": bool(self.backend.api_key),
                },
            },
            "stats": {
                "requests_total": self._stats["requests_total"],
                "requests_compressed": self._stats["requests_compressed"],
                "chars_saved": self._stats["chars_saved"],
                "tokens_saved_estimate": self._stats["tokens_saved"],
                "retrievals_served": self._stats["retrievals_served"],
            },
        })

    async def handle_unknown(self, request: web.Request) -> web.Response:
        """Handle unknown paths by proxying to upstream."""
        upstream = await self._get_upstream_session()
        api_key = self.backend.api_key

        headers = {"Authorization": f"Bearer {api_key}", **client_headers(request.headers)}
        for h in ("Content-Type", "Accept"):
            if h in request.headers:
                headers[h] = request.headers[h]

        # Clients are told to use http://host:port/v1 as their base URL, and
        # every backend's upstream_url already ends in its version (…/v1), so
        # a request for /v1/models must go to {upstream_url}/models, not
        # {upstream_url}/v1/models.
        path = request.path
        if path == "/v1" or path.startswith("/v1/"):
            path = path[len("/v1"):]
        upstream_url = f"{self.upstream_url}{path}"
        if request.query_string:
            upstream_url += f"?{request.query_string}"

        try:
            body = await request.read() if request.can_read_body else None
            async with upstream.request(
                request.method, upstream_url, headers=headers, data=body,
                timeout=PASSTHROUGH_TIMEOUT,
            ) as resp:
                data = await resp.read()
                return web.Response(body=data, status=resp.status,
                                    content_type=resp.content_type)
        except asyncio.TimeoutError:
            return web.json_response({"error": "Upstream request timed out"}, status=504)
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


def _add_usage(total: dict[str, int], response_data: dict) -> None:
    """Add a response's integer token counts into a running total."""
    for key, value in (response_data.get("usage") or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def create_app(
    backend: str | Backend = "openrouter",
    upstream_url: str | None = None,
    api_key_env: str | None = None,
    config_path: str | None = None,
    log_file: str | None = None,
    profile: str | None = None,
) -> web.Application:
    """Create the aiohttp application with routes."""
    proxy = ProteusProxy(
        backend=backend,
        upstream_url=upstream_url,
        api_key_env=api_key_env,
        config_path=config_path,
        log_file=log_file,
        profile=profile,
    )

    app = web.Application()
    app["proxy"] = proxy

    async def _close_upstream(_app: web.Application) -> None:
        await proxy.close()

    app.on_cleanup.append(_close_upstream)

    app.router.add_post("/v1/chat/completions", proxy.handle_chat_completions)
    app.router.add_get("/livez", proxy.handle_livez)
    app.router.add_get("/readyz", proxy.handle_readyz)
    app.router.add_get("/health", proxy.handle_livez)

    app.router.add_route("*", "/{path:.*}", proxy.handle_unknown)

    return app


def start_proxy(
    host: str = "127.0.0.1",
    port: int = 8787,
    backend: str = "openrouter",
    upstream_url: str | None = None,
    api_key_env: str | None = None,
    config_path: str | None = None,
    log_file: str | None = None,
    profile: str | None = None,
):
    """Start the Proteus proxy server (blocking).

    Compression settings come from config_path and/or profile if given;
    those should already be applied (the CLI does), and the file is
    re-read whenever it changes.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = create_app(
        backend=backend,
        upstream_url=upstream_url,
        api_key_env=api_key_env,
        config_path=config_path,
        log_file=log_file,
        profile=profile,
    )

    proxy = app["proxy"]
    bk = proxy.backend
    print(f"🌊 Proteus proxy running on http://{host}:{port}")
    print(f"   Backend: {bk.name} -> {bk.upstream_url}")
    print(f"   API key: {bk.api_key_env}{' (fallback: ' + bk.api_key_env_fallback + ')' if bk.api_key_env_fallback else ''}")
    print(f"   API key set: {bool(bk.api_key)}")
    print(f"   Configure your client to use http://{host}:{port}/v1")

    web.run_app(app, host=host, port=port, print=lambda *a, **kw: None)


if __name__ == "__main__":
    start_proxy()

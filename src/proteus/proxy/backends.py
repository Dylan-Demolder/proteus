"""Proteus backends — abstraction layer for different LLM API providers.

Inspired by HeadRoom's backend architecture, adapted for Proteus's simpler
needs (OpenAI-compatible chat completions only).

Each backend knows:
  - The upstream base URL
  - Which environment variable holds the API key
  - Any request/response transformations needed

Usage:
    from proteus.proxy.backends import get_backend, Backend

    backend = get_backend("openrouter")
    print(backend.name)       # "openrouter"
    print(backend.api_key)    # reads from env
    print(backend.upstream_url)  # "https://openrouter.ai/api/v1"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Backend:
    """A configured API backend with its URL, API key, and transformations.

    Attributes:
        name: Human-readable backend name (e.g., "openrouter", "opencode-go").
        upstream_url: Base URL for the API (without /v1/chat/completions suffix).
        api_key_env: Name of the environment variable containing the API key.
        api_key_env_fallback: Optional fallback env var if primary is not set.
        extra_headers: Additional headers to send with every request.
        strip_request_fields: Fields to remove from request body before forwarding
            (e.g., fields that upstream doesn't support).
        description: Human-readable description for --help.
    """

    name: str
    upstream_url: str
    api_key_env: str
    api_key_env_fallback: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    strip_request_fields: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def api_key(self) -> str:
        """Get the API key from environment, checking fallback if primary is empty."""
        key = os.environ.get(self.api_key_env, "")
        if not key and self.api_key_env_fallback:
            key = os.environ.get(self.api_key_env_fallback, "")
        return key

    @property
    def is_configured(self) -> bool:
        """Whether this backend has an API key set."""
        return bool(self.api_key)

    def transform_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply backend-specific request transformations.

        Removes fields the upstream doesn't support, adds required fields.

        Args:
            body: The parsed JSON request body.

        Returns:
            Modified request body.
        """
        if not self.strip_request_fields:
            return body

        modified = dict(body)
        for field in self.strip_request_fields:
            modified.pop(field, None)
        return modified

    def transform_response(self, response_data: dict[str, Any]) -> dict[str, Any]:
        """Apply backend-specific response transformations.

        Some backends return fields that confuse certain clients.
        Override in subclasses or via factory.

        Args:
            response_data: The parsed JSON response from the upstream.

        Returns:
            Modified response data.
        """
        return response_data


# ── Built-in backend definitions ──

BUILTIN_BACKENDS: dict[str, Backend] = {
    "openrouter": Backend(
        name="openrouter",
        upstream_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        description="OpenRouter — multi-model router with per-token billing",
    ),
    "opencode-go": Backend(
        name="opencode-go",
        upstream_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        description="OpenCode Go — flat-rate $10/month open model access",
        strip_request_fields=["reasoning_effort", "stream_options"],
    ),
    "openai": Backend(
        name="openai",
        upstream_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        description="OpenAI — GPT-4, GPT-4o, and other OpenAI models",
    ),
    "generic": Backend(
        name="generic",
        upstream_url="",
        api_key_env="OPENROUTER_API_KEY",
        api_key_env_fallback="OPENAI_API_KEY",
        description="Custom OpenAI-compatible endpoint (set via --upstream-url and --api-key-env)",
    ),
}


def get_backend(name: str, upstream_url: str | None = None, api_key_env: str | None = None) -> Backend:
    """Get a backend by name, optionally overriding URL and API key env var.

    Args:
        name: Backend name ("openrouter", "opencode-go", "openai", "generic").
        upstream_url: Override the default upstream URL.
        api_key_env: Override the default API key env var name.

    Returns:
        A configured Backend instance.

    Raises:
        ValueError: If backend name is unknown.
    """
    name = name.lower().replace("_", "-")

    if name not in BUILTIN_BACKENDS:
        valid = ", ".join(BUILTIN_BACKENDS.keys())
        raise ValueError(f"Unknown backend '{name}'. Valid backends: {valid}")

    backend = BUILTIN_BACKENDS[name]

    # Create a copy with overrides
    overrides: dict[str, Any] = {}
    if upstream_url is not None:
        overrides["upstream_url"] = upstream_url
    if api_key_env is not None:
        overrides["api_key_env"] = api_key_env

    if overrides:
        return Backend(
            name=name,
            upstream_url=overrides.get("upstream_url", backend.upstream_url),
            api_key_env=overrides.get("api_key_env", backend.api_key_env),
            api_key_env_fallback=backend.api_key_env_fallback,
            extra_headers=dict(backend.extra_headers),
            strip_request_fields=list(backend.strip_request_fields),
            description=backend.description,
        )

    return backend


def auto_detect_backend(prefer: str | None = None) -> Backend:
    """Auto-detect which backend to use based on available env vars.

    Checks env vars in priority order: OPENROUTER_API_KEY, OPENCODE_GO_API_KEY,
    OPENAI_API_KEY. If none are set, falls back to the preferred backend name
    (defaults to openrouter).

    Args:
        prefer: Backend to prefer if multiple keys are set (or if none set).

    Returns:
        A configured Backend instance.
    """
    # If a preference is given and it's configured, use it
    if prefer and prefer in BUILTIN_BACKENDS:
        preferred = get_backend(prefer)
        if preferred.is_configured:
            return preferred

    # Auto-detect in priority order
    priority = ["openrouter", "opencode-go", "openai", "generic"]
    for name in priority:
        if name in BUILTIN_BACKENDS:
            backend = get_backend(name)
            if backend.is_configured:
                return backend

    # Fallback to preferred or openrouter
    fallback_name = prefer if prefer in BUILTIN_BACKENDS else "openrouter"
    return get_backend(fallback_name)


def list_backends() -> dict[str, str]:
    """List all available backends with descriptions.

    Returns:
        Dict mapping backend name -> description.
    """
    return {name: b.description for name, b in BUILTIN_BACKENDS.items()}

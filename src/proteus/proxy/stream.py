"""Serving proteus_retrieve inside a streamed (SSE) response.

A streamed reply arrives as chunks, so the proxy can't wait for the whole
message before deciding whether the model asked for proteus_retrieve. Instead
each upstream round is relayed as it arrives, with the retrieve calls cut out:

- content and reasoning deltas go to the client immediately;
- tool-call deltas for proteus_retrieve are held back, the client's own tool
  calls are passed on (renumbered from 0 if a retrieve call came first);
- the finish chunk is held until the round ends. A round that asked only for
  proteus_retrieve is not finished for the client: the proxy answers the
  calls and streams the next round into the same response.

Usage is summed over the rounds and sent once, before ``[DONE]``, followed by
an SSE comment (``: proteus retrievals=N``) that clients ignore.
"""

from __future__ import annotations

import json
from typing import Any

from proteus.proxy.handler import pending_retrieve_calls
from proteus.proxy.inject import RETRIEVE_TOOL_NAME


def split_events(buffer: str) -> tuple[list[str], str]:
    """Split complete SSE events off the front of ``buffer``.

    Returns (events, rest). Each event is its lines without the blank line
    that ends it; ``rest`` is an incomplete event still to be continued.
    """
    buffer = buffer.replace("\r\n", "\n")
    *events, rest = buffer.split("\n\n")
    return [e for e in events if e], rest


def _event_data(event: str) -> str | None:
    """The joined ``data:`` payload of an event, or None if it has none."""
    data = [line[5:].lstrip() if line.startswith("data:") else None for line in event.split("\n")]
    parts = [d for d in data if d is not None]
    return "\n".join(parts) if parts else None


def add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    """Add a usage block into ``total``, including nested token details."""
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict):
            add_usage(total.setdefault(key, {}), value)


class StreamRound:
    """Rewrites one upstream round of a streamed chat completion.

    Args:
        first: Whether this is the first round (later rounds drop the
            repeated ``role`` delta).
        can_retrieve: Whether the proxy may answer retrieve calls with
            another round (false once the round limit is reached).
    """

    def __init__(self, first: bool = True, can_retrieve: bool = True):
        self.first = first
        self.can_retrieve = can_retrieve
        self.content: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}  # upstream index -> call
        self.client_index: dict[int, int] = {}      # upstream index -> index the client sees
        self.usage: dict[str, Any] = {}
        self.finish: dict[str, Any] | None = None   # held finish chunk
        self.last_chunk: dict[str, Any] = {}        # for id/model/created of our own chunks
        self.done = False

    # ── per-event rewriting ──

    def feed(self, event: str) -> list[str]:
        """Take one upstream event; return the events to send the client now."""
        data = _event_data(event)
        if data is None:
            return [event]  # comment, keep-alive, event:/id: lines
        if data.strip() == "[DONE]":
            self.done = True
            return []
        try:
            chunk = json.loads(data)
        except ValueError:
            return [event]
        if not isinstance(chunk, dict):
            return [event]
        self.last_chunk = chunk

        if isinstance(chunk.get("usage"), dict):
            add_usage(self.usage, chunk["usage"])
            chunk = {k: v for k, v in chunk.items() if k != "usage"}
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return []  # usage-only chunk: sent once, at the very end
        if len(choices) != 1 or not isinstance(choices[0], dict):
            return [_encode(chunk)]  # n > 1: not ours to rewrite

        choice = dict(choices[0])
        delta = dict(choice.get("delta") or {})
        if not self.first:
            delta.pop("role", None)
        if isinstance(delta.get("content"), str):
            self.content.append(delta["content"])
        if isinstance(delta.get("tool_calls"), list):
            kept = [c for tc in delta["tool_calls"] for c in self._tool_delta(tc)]
            if kept:
                delta["tool_calls"] = kept
            else:
                del delta["tool_calls"]
        choice["delta"] = delta

        if choice.get("finish_reason"):
            # Hold the finish itself, but not what rides with it: some
            # providers put the last content token in the finish chunk, and
            # a held chunk is dropped when another round follows.
            self.finish = {**chunk, "choices": [{**choice, "delta": {}}]}
            if not delta:
                return []
            return [_encode({**chunk, "choices": [{**choice, "delta": delta, "finish_reason": None}]})]
        if not delta:
            return []  # it only carried retrieve deltas (or a repeated role)
        return [_encode({**chunk, "choices": [choice]})]

    def _tool_delta(self, tc: Any) -> list[Any]:
        """The client's share of one tool-call delta: nothing for a retrieve call.

        Deltas for a call whose name hasn't arrived yet are held until it
        does, so a retrieve call never leaks to the client half-formed.
        """
        if not isinstance(tc, dict) or not isinstance(tc.get("index"), int):
            return [tc]
        index = tc["index"]
        fn = tc.get("function") or {}
        call = self.calls.setdefault(index, {"id": "", "name": "", "arguments": "", "pending": []})
        call["id"] = call["id"] or tc.get("id") or ""
        call["name"] = call["name"] or fn.get("name") or ""
        call["arguments"] += fn.get("arguments") or ""
        if not call["name"]:
            call["pending"].append(tc)
            return []
        held, call["pending"] = call["pending"], []
        if call["name"] == RETRIEVE_TOOL_NAME:
            return []
        if index not in self.client_index:
            self.client_index[index] = len(self.client_index)
        return [{**d, "index": self.client_index[index]} for d in (*held, tc)]

    # ── end of round ──

    def _retrieve_calls(self) -> list[dict[str, Any]]:
        return [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _, c in sorted(self.calls.items())
            if c["name"] == RETRIEVE_TOOL_NAME
        ]

    def followup(self) -> tuple[dict, list[dict]] | None:
        """The messages for another round, if this one asked only for proteus_retrieve."""
        calls = self._retrieve_calls()
        unnamed = any(c["pending"] for c in self.calls.values())
        if not calls or self.client_index or unnamed or not self.can_retrieve:
            return None
        message = {"role": "assistant", "content": "".join(self.content) or None, "tool_calls": calls}
        return pending_retrieve_calls({"choices": [{"message": message}]})

    def closing(self) -> list[str]:
        """Events that end the reply for the client when no round follows."""
        events = []
        for index, call in sorted(self.calls.items()):
            if call["pending"]:
                # The name never came: not a retrieve call, so it's the client's.
                self.client_index.setdefault(index, len(self.client_index))
                deltas = [{**d, "index": self.client_index[index]} for d in call["pending"]]
                call["pending"] = []
                like: dict[str, Any] = {k: self.last_chunk[k] for k in ("id", "object", "created", "model")
                                        if k in self.last_chunk}
                events.append(_encode({**like, "choices": [{"index": 0, "delta": {"tool_calls": deltas}}]}))
        if self.finish is None:
            return events
        finish = json.loads(json.dumps(self.finish))
        choice = finish["choices"][0]
        if self._retrieve_calls() and not self.client_index:
            # Only retrieve calls, and no more rounds allowed: the client
            # never defined that tool, so this turn simply ends.
            choice["finish_reason"] = "stop"
        return [*events, _encode(finish)]


def usage_event(usage: dict[str, Any], like: dict[str, Any]) -> str:
    """A usage-only chunk shaped like the upstream's own chunks."""
    chunk: dict[str, Any] = {k: like[k] for k in ("id", "object", "created", "model") if k in like}
    chunk.setdefault("object", "chat.completion.chunk")
    return _encode({**chunk, "choices": [], "usage": usage})


def error_event(message: str) -> str:
    """An in-stream error, in the shape OpenAI uses for stream errors."""
    return _encode({"error": {"message": message, "type": "proteus_proxy_error"}})


def _encode(chunk: dict[str, Any]) -> str:
    return "data: " + json.dumps(chunk, separators=(",", ":"))

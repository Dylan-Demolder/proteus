"""Proteus proxy — request handler that compresses tool results inline."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from proteus import ccr, compress_tool_output, config
from proteus.proxy.inject import RETRIEVE_TOOL_NAME, inject_retrieve_tool
from proteus.router import should_compress

logger = logging.getLogger(__name__)

# Deprecated: the threshold is config.MIN_COMPRESS_CHARS. Kept for importers.
MIN_COMPRESS_CHARS = 3000

# Roles whose string content is a tool's output in the OpenAI chat format.
# ("function" is the legacy name for "tool".)
_TOOL_ROLES = ("tool", "function")


def _function_name(tool_call: dict) -> str:
    function = tool_call.get("function")
    return function.get("name", "") if isinstance(function, dict) else ""


def _retrieve_call_ids(messages: list[dict]) -> set[str]:
    """IDs of earlier proteus_retrieve calls in the conversation.

    Their results *are* the uncompressed originals: compressing them again
    would hand the model the same summary it just asked to see past.
    """
    ids: set[str] = set()
    for message in messages:
        for tc in message.get("tool_calls") or []:
            if isinstance(tc, dict) and _function_name(tc) == RETRIEVE_TOOL_NAME:
                ids.add(tc.get("id", ""))
    return ids


def _what_changed(cstats: dict) -> str:
    """What compression removed, in a few words.

    Without it, a careful model can't tell whether its answer might be in the
    part it can't see, and retrieves the original just to check.
    """
    compressor = cstats.get("compressor", "")
    if compressor == "json_crusher":
        dropped = cstats.get("dropped_rows", 0)
        if cstats.get("mode") == "columnar" and not dropped:
            return f"all {cstats.get('original_rows', 0):,} rows kept in columnar form, nothing dropped"
        if dropped:
            return f"{dropped:,} of {cstats.get('original_rows', 0):,} rows not shown"
    elif compressor.startswith("code"):
        return "comments and docstrings removed, code unchanged"
    elif compressor == "log_deduper":
        if not cstats.get("errors_dropped"):
            return "repeated and routine lines trimmed, every error line kept"
        return f"repeated and routine lines trimmed, {cstats['errors_dropped']:,} error lines not shown"
    elif compressor == "search":
        shown = (f"{cstats.get('compressed_matches', 0):,} of {cstats.get('original_matches', 0):,} matches "
                 f"shown, from {cstats.get('compressed_files', 0):,} of {cstats.get('original_files', 0):,} files")
        if cstats.get("rare_hidden") == 0:
            shown += "; every match unlike the others is among those shown"
        return shown
    elif cstats.get("mode") == "text_summary":
        return f"start and end kept, {cstats.get('dropped_chars', 0):,} chars from the middle not shown"
    return ""


def _marker(cstats: dict, retrievable: bool) -> str:
    """Trailer telling the model this output was compressed and how to get it back."""
    size = f"{cstats['original_chars']:,}→{cstats['compressed_chars']:,} chars"
    changed = _what_changed(cstats)
    if changed:
        size += f": {changed}"
    if retrievable:
        # Mentioning query steers models to a cheap filtered retrieve instead
        # of re-running their own command to double-check (seen live).
        return (
            f"\n[proteus: compressed {size}. For the full original call "
            f'{RETRIEVE_TOOL_NAME}(hash="{cstats["hash"]}"), or add query="..." '
            f"(text or regex) to get just the matching lines]"
        )
    return f"\n[proteus: compressed {size}; original cached as {cstats['hash']}]"


def _compress(text: str, stats: dict[str, Any], retrievable: bool) -> str | None:
    """Compress one tool output. Returns the replacement text, or None to leave it alone."""
    if not should_compress(text):  # honours config.MIN_COMPRESS_CHARS / profiles
        return None
    compressed, cstats = compress_tool_output(text)
    if not cstats["was_compressed"]:
        return None
    compressed += _marker(cstats, retrievable)
    if len(compressed) > len(text) * (1 - config.MIN_SAVINGS_PCT / 100):
        # Too little saved to be worth it: if the model then needs the
        # original, the retrieve round costs more than compression saved.
        return None
    saved = len(text) - len(compressed)
    stats["compressed"] += 1
    stats["total_saved"] += saved
    stats["total_tokens_saved"] += saved // 4
    logger.debug("Compressed tool result: %s chars saved (%.1f%%)", saved, cstats["compression_pct"])
    return compressed


def _process_messages(messages: list[dict], retrievable: bool = True) -> dict[str, Any]:
    """Process all messages in a request, compressing large tool results.

    Handles every place a tool's output can appear:
      - OpenAI format: ``{"role": "tool", "content": "..."}``, where content is
        a string or a list of ``{"type": "text", "text": ...}`` parts.
      - ``{"type": "tool_result", "content": "..."}`` blocks in a content list.
      - Large plain-text ``user`` messages (agents that paste tool output there).

    Args:
        messages: The messages array from the request body. Modified in place.
        retrievable: Whether the model will be able to call proteus_retrieve.
            This controls the wording of the marker appended to compressed output.

    Returns:
        Stats dict: {
            "compressed": int,  # number of tool results compressed
            "total_saved": int,  # total chars saved
            "total_tokens_saved": int,
            "injected_tool": bool,  # whether proteus_retrieve was injected
            "original_tools": int,  # original tools array length
        }
    """
    stats = {
        "compressed": 0,
        "total_saved": 0,
        "total_tokens_saved": 0,
        "injected_tool": False,
        "original_tools": 0,
        "tool_calls_count": 0,
    }
    skip_ids = _retrieve_call_ids(messages)

    for message in messages:
        content = message.get("content", "")
        role = message.get("role", "")

        if role in _TOOL_ROLES:
            stats["tool_calls_count"] += 1
            if message.get("tool_call_id") in skip_ids:
                continue
            if isinstance(content, str):
                new = _compress(content, stats, retrievable)
                if new is not None:
                    message["content"] = new
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        new = _compress(part["text"], stats, retrievable)
                        if new is not None:
                            part["text"] = new

        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    stats["tool_calls_count"] += 1
                    tool_content = item.get("content", "")
                    if isinstance(tool_content, str):
                        new = _compress(tool_content, stats, retrievable)
                        if new is not None:
                            item["content"] = new

        elif role == "user" and isinstance(content, str):
            new = _compress(content, stats, retrievable)
            if new is not None:
                message["content"] = new

    return stats


def run_retrieve(tool_call: dict) -> str:
    """Execute one proteus_retrieve call against the local CCR cache.

    Returns the text to send back to the model as the tool result. Errors are
    returned as text too: the model can read them and recover, whereas an
    exception here would fail the user's whole request.
    """
    try:
        args = json.loads(tool_call["function"].get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        return "Error: arguments must be a JSON object with a \"hash\" field."

    content_hash = str(args.get("hash", "")).strip()
    original = ccr.retrieve(content_hash)
    if original is None:
        return f"Error: no cached content for hash {content_hash!r}."

    query = str(args.get("query") or "").strip()
    if not query:
        return original
    return _search(original, content_hash, query)


# Lines shown on each side of a query match, like grep -C.
RETRIEVE_CONTEXT_LINES = 2


def _search(original: str, content_hash: str, query: str) -> str:
    """The lines of ``original`` that match ``query``, with context.

    Models write queries like "staging database port", "mod_24.py VERSION"
    or "timeout = [0-9]", which rarely occur verbatim. Tried in order: the
    whole query as text, as a regular expression, lines containing every
    word, lines containing any word. A matching line on its own often lacks
    what the model needs (a JSON field one line up, a diff's file header),
    so neighbouring lines come along.
    """
    lines = original.split("\n")
    folded = [line.casefold() for line in lines]
    needle = query.casefold()
    words = needle.split()
    hits = [n for n, line in enumerate(folded) if needle in line]
    how = "containing"
    if not hits:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = None
        if pattern is not None:
            hits = [n for n, line in enumerate(lines) if pattern.search(line)]
            how = "matching the regex"
    if not hits and len(words) > 1:
        hits = [n for n, line in enumerate(folded) if all(w in line for w in words)]
        how = "containing every word of"
        if not hits:
            hits = [n for n, line in enumerate(folded) if any(w in line for w in words)]
            how = "containing any word of"
    if not hits:
        return f"No lines in {content_hash} match {query!r}; nothing else in the original matches either."

    shown: set[int] = set()
    for n in hits:
        shown.update(range(max(0, n - RETRIEVE_CONTEXT_LINES), min(len(lines), n + RETRIEVE_CONTEXT_LINES + 1)))
    hit_set = set(hits)
    out = [
        f"[All {len(hits)} lines of the full original {how} {query!r} (of {len(lines)} lines), "
        f"with {RETRIEVE_CONTEXT_LINES} lines of context. Matches as line: text, context as line- text]"
    ]
    prev = None
    for n in sorted(shown):
        if prev is not None and n != prev + 1:
            out.append("--")
        out.append(f"{n + 1}{':' if n in hit_set else '-'} {lines[n]}")
        prev = n
    result = "\n".join(out)
    # The query matched most of it: the original is no longer and easier to read.
    return original if len(result) >= len(original) else result


def pending_retrieve_calls(response_data: dict) -> tuple[dict, list[dict]] | None:
    """If the model's turn consists only of proteus_retrieve calls, answer them.

    Returns (assistant_message, tool_result_messages) to append to the
    conversation before asking the model again, or None if this response
    should go back to the client as-is.

    A turn that mixes proteus_retrieve with the client's own tools is left
    alone. The client runs its tools, and ``strip_retrieve_calls`` removes the
    call it cannot run.
    """
    choices = response_data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return None
    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls or any(_function_name(tc) != RETRIEVE_TOOL_NAME for tc in calls):
        return None

    assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
    results = [
        {"role": "tool", "tool_call_id": tc.get("id", ""), "content": run_retrieve(tc)}
        for tc in calls
    ]
    return assistant, results


def strip_retrieve_calls(response_data: dict) -> dict:
    """Remove proteus_retrieve calls from a response before it reaches the client.

    The client never defined that tool. Passing a call to it through would
    leave an agent holding a tool call it has no way to execute.
    """
    for choice in response_data.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or not message.get("tool_calls"):
            continue
        kept = [tc for tc in message["tool_calls"] if _function_name(tc) != RETRIEVE_TOOL_NAME]
        if len(kept) == len(message["tool_calls"]):
            continue
        if kept:
            message["tool_calls"] = kept
        else:
            del message["tool_calls"]
            if message.get("content") is None:
                message["content"] = ""
            if choice.get("finish_reason") == "tool_calls":
                choice["finish_reason"] = "stop"
    return response_data


def handle_tool_calls(response_data: dict, ccr_lookup: dict[str, str]) -> dict:
    """Handle a response that contains tool calls to proteus_retrieve.

    Args:
        response_data: The parsed response JSON from the upstream API.
        ccr_lookup: Mapping of hashes to original content (populated during request processing).

    Returns:
        Modified response with tool call results substituted.
    """
    choices = response_data.get("choices", [])
    for choice in choices:
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            continue

        for tc in tool_calls:
            if tc.get("function", {}).get("name") == "proteus_retrieve":
                try:
                    args = json.loads(tc["function"]["arguments"])
                    hash_key = args.get("hash", "")
                    original = ccr_lookup.get(hash_key)
                    if original:
                        tc["function"]["proteus_original"] = original
                except (json.JSONDecodeError, KeyError):
                    pass

    return response_data


def transform_request_body(
    body: dict, inject_tool: bool = True
) -> tuple[dict, dict[str, str], dict[str, Any]]:
    """Transform a /v1/chat/completions request body by compressing tool results.

    Args:
        body: The parsed JSON request body.
        inject_tool: Add the proteus_retrieve tool when something was compressed.
            Only do this when the caller will answer the model's calls to it
            (the proxy can for non-streaming responses). Otherwise the client
            would receive a tool call it has no way to execute.

    Returns:
        (modified_body, ccr_lookup, stats)

        modified_body has compressed tool results
        ccr_lookup maps hashes to original content for retrieval
        stats has compression statistics
    """
    messages = body.get("messages", [])
    client_tools = body.get("tools") or []
    client_serves_retrieve = any(
        isinstance(t, dict) and _function_name(t) == RETRIEVE_TOOL_NAME for t in client_tools
    )
    stats = _process_messages(messages, retrievable=inject_tool or client_serves_retrieve)

    # Count (and strip) proteus bookkeeping keys a client may have left in the
    # messages. They are not part of the chat format and strict upstreams
    # reject unknown fields.
    ccr_lookup: dict[str, str] = {}
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str) and "_proteus_original_length" in message:
            stats["original_tools"] += 1
            del message["_proteus_original_length"]
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "_proteus_original_length" in item:
                    stats["original_tools"] += 1
                    del item["_proteus_original_length"]

    # Inject retrieve tool if any compression happened
    if stats["compressed"] > 0 and inject_tool:
        stats["original_tools"] = len(client_tools)
        body["tools"] = inject_retrieve_tool(client_tools)
        stats["injected_tool"] = len(body["tools"]) > len(client_tools)

    body["messages"] = messages
    return body, ccr_lookup, stats

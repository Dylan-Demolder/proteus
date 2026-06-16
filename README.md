# Proteus

**Shape-shifting compression for LLM tool outputs.**

Proteus sits between your LLM agent and the API provider, compressing large tool outputs before they reach the model. Same answers, fraction of the tokens.

```
Tool output (terminal, file read, search results)
    │
    │  >3K chars?  ─── No ──→ Pass through
    │  Yes
    ▼
    ContentRouter ─→ [JSON | Logs | Code | Search | Diff | Text]
         │                    │
         │          Compress + store original in CCR cache
         ▼
    Compressed output + hash marker → LLM
```

## Quick start

```bash
pip install proteus
```

### Proxy mode (transparent, no code changes)

```bash
proteus proxy --port 8787 --backend openrouter
```

Then configure your agent to use `http://localhost:8787/v1` as the API base URL. Large tool results are compressed automatically.

### Library mode (inline in your code)

```python
from proteus import compress_tool_output
from proteus.ccr import retrieve

# Compress large tool output before sending to LLM
result = open("/path/to/big.json").read()
compressed, stats = compress_tool_output(result)
print(f"Saved {stats['compression_pct']:.1f}% — {stats['estimated_token_savings']} tokens")

# Retrieve original when needed:
original = retrieve(stats["hash"])
```

### CLI mode

```bash
proteus file path/to/big.json    # Compress and show stats
proteus cache path/to/cache.json  # Pre-compress a cache file on disk
proteus stats                     # Show CCR cache statistics
proteus retrieve <hash>           # Retrieve original from cache
proteus clear                     # Clear the cache
```

## Compressors

| Type | Compressor | Savings | Quality |
|------|-----------|:-------:|:-------:|
| JSON arrays | SmartCrusher | 60-95% | 0% (columnar) |
| JSON arrays (200+ rows) | SmartCrusher | 90%+ | <5% (row-drop) |
| Repetitive logs | LogDeduper | 60-99% | 0% |
| Source code | CodeCompressor | 20-40% | 0% |
| File listings (`ls -la`) | FileLister | 40-50% | 0% |
| Search results | SearchCompressor | 50-80% | 0% |
| Git diffs | DiffCompressor | 40-60% | 0% |
| Long text (>10K) | TextSummarizer | 60-80% | <5% |

## How it works

Proteus uses **deterministic, rule-based algorithms** — no ML, no models, zero added latency:

1. **ContentRouter** detects the type of content (JSON, code, logs, search results, diffs, listings)
2. Routes to a **specialized compressor** that understands the structure
3. Compresses with structure-aware techniques (columnar format, line dedup, AST-aware stripping)
4. Stores the **original in a local CCR cache** indexed by hash
5. Injects a `proteus_retrieve` tool so the LLM can fetch original data if needed

All compression is **reversible** — originals are never lost.

## Why not HeadRoom?

HeadRoom (the upstream project) is excellent but built for Anthropic's API. Proteus is a fork that:

- **Supports OpenRouter and OpenAI** — works with any OpenAI-compatible endpoint
- **Pure Python, no Rust** — installs in seconds, no compilation
- **Deterministic only** — no ML compressors on the hot path
- **Multi-turn compression** — compresses accumulated history, not just live output
- **3 compression profiles** — conservative (lossless), balanced (default), aggressive (max savings)
- **Live dashboard** — `/proteus` page showing cache stats, per-compressor breakdown
- **~90% less code** — same core value, dramatically simpler

## Requirements

- Python 3.10+
- aiohttp (for proxy mode)
- Works with any OpenAI-compatible API (OpenRouter, OpenAI, Groq, Together, etc.)

## Tests

```bash
pip install proteus[dev]
cd test && python run_all.py   # 106 existing tests
python test_new.py              # 57 new tests (Phase 2 compressors)
python test_history.py          # 21 history compression tests
python test_profiles.py         # 91 per-session profile tests
# Combined: 275 tests, all passing
```

## License

Apache 2.0 — same as upstream HeadRoom.

## Status

Alpha. Built for Hermes Agent but works with any LLM client that speaks OpenAI-compatible API. Proxy mode is functional but still has rough edges — expect improvements.

---

*Named after Proteus, the shape-shifting sea god of Greek myth who could change form while keeping his true nature — exactly what this does to your tool outputs.*
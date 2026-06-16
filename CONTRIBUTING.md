# Contributing to Proteus

Thanks for your interest! This is a focused fork of HeadRoom — deterministic compressors for OpenAI-compatible LLM backends. We keep it lean.

## Core philosophy

- **No ML on the hot path** — all compressors are rule-based
- **No Rust** — pure Python, zero compilation
- **No Anthropic-specific features** — every path works with OpenAI-compatible APIs
- **All compression is reversible** — CCR cache must always store originals
- **0% quality loss on lossless modes** — compact JSON, columnar, stripped code must parse identically
- **<5% quality loss on lossy modes** — row-dropping and text summarization must be validated

## Adding a new compressor

1. Create `proteus/compressors/your_compressor.py`
2. Implement a function with signature: `def compress_your_type(content: str, **kwargs) -> tuple[str, dict]`
3. The returned dict must include `mode` (string) and `original_chars`/`compressed_chars` (ints)
4. Register it in `proteus/router.py`'s `detect_content_type()` and `proteus/__init__.py`'s `compress_tool_output()`
5. Write tests in `tests/test_your_compressor.py`
6. Run all tests: `python tests/run_all.py && python tests/test_new.py`

### Compressor template

```python
def compress_my_type(content: str, param1: int = 10) -> tuple[str, dict]:
    stats = {"original_chars": len(content), "mode": "my_type"}
    
    # ... compression logic ...
    compressed = do_compression(content, param1)
    
    stats["compressed_chars"] = len(compressed)
    return compressed, stats
```

## Code standards

- Python 3.10+, type hints on all public functions
- pydoc on every public function
- No external dependencies beyond stdlib + aiohttp + click + pyyaml
- Preserve the CCR cache API — all compressed content must be retrievable

## Proxy development

The proxy (`proteus/proxy/server.py`) is an aiohttp server that:

1. Listens on a configurable port (default 8787)
2. Intercepts POST `/v1/chat/completions`
3. Compresses large tool results in the messages array
4. Forwards to upstream (OpenRouter / OpenAI / custom)
5. Injects `proteus_retrieve` tool for reversible compression

To test the proxy locally:

```bash
export OPENROUTER_API_KEY="sk-or-..."
proteus proxy --port 8787 --backend openrouter
# In another terminal:
curl http://localhost:8787/v1/chat/completions -d '{"model":"...","messages":[{"role":"user","content":"hi"}]}'
```

## Running tests

```bash
# Existing 106 tests
python tests/run_all.py

# New Phase 1+2 tests (57)
python tests/test_new.py

# Combined
python tests/run_all.py && python tests/test_new.py
# Expected: 163 passed, 0 failed
```

## Pull request guidelines

- One feature per PR
- Include tests (new + existing must pass)
- Update the README if adding a compressor
- Conventional commit message: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- No silent fallbacks — if a compressor can't handle content, it should return the content unchanged, not crash
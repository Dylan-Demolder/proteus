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

## Showcase

### 🌤 Weather Dashboard (demos/weather-dashboard)

A real HTML/CSS/JS weather app that fetches live data from wttr.in — served as an end-to-end compression demo.

**Real-world test on 9 files + live API response (300KB → 111KB, 63%):**

| File | Type | Size | Saved |
|---|---|---|---|
| `app.js` | code_javascript | 5.1KB → 4.7KB | 8.4% |
| `weather-api-response.json` | json | 38.7KB → 23.1KB | 40.3% |
| `LIVE-wttr.in-Tokyo.json` | json | 38.7KB → 23.1KB | 40.2% |
| `combined-project.txt` | text | 130.9KB → **4.6KB** | **96.5%** |
| **Total** | | **300KB → 111KB** | **63.0%** |

**Verification:**
- ✅ All 9 files: byte-for-byte match after CCR roundtrip
- ✅ All 7 weather fields identical on 4 API responses (temp, humidity, pressure, UV, wind direction/speed)
- ✅ Live API fetch from wttr.in compressed and decompressed successfully
- ✅ Cost savings: $0.15 → $0.06 per run • **$3,530/year at 100 runs/day**

The combined-project.txt shows the killer use case: an LLM seeing `cat` output of a whole project goes from 131KB (33K tokens) → 4.6KB (1.1K tokens) — identical information, 96% less context cost.

### 📊 Log Analyzer (demos/log-analyzer)

A multi-service log analysis pipeline demonstrating Proteus on server output.

**8 files, 256KB → 79KB (69%):**

| File | Size | Saved | Compressor |
|---|---|---|---|
| combined.log (3 services, 800 entries) | 84.6KB → 21.2KB | 74.9% | log_deduper |
| nginx.log (500 lines) | 55.2KB → 1.9KB | **96.5%** | search |
| app.log (200 lines) | 19.7KB → 19.6KB | 0.6% | log_deduper |
| metadata.json | 87.5KB → 28.2KB | 67.8% | json_crusher |

**Verification:** All 70 analysis metrics (IP counts, status codes, error rates, timing, durations) identical across 5 files after roundtrip — 14 metrics × 5 files, zero failures.

## Benchmarks

### 48-scenario benchmark suite (test/benchmark_all.py)

```
                 ──────────── Before ───── After ─────  Δ ────
Overall savings     50.5%        68.1%    +17.6pp
Latency             2.5ms         1.6ms    -0.9ms

Key fixes driving improvement:
  Log timestamp regex (space handling)       0.0% → 98.1%
  JSON large-string CCR hashing              0.1% → 99.2%
  Search context format (rg --context=N)     0.0% → 82.5%
```

### Per-compressor breakdown (48 tests, 7 compressor categories)

| Compressor | Tests | Avg Savings | Best | Worst | Latency |
|---|---|---|---|---|---|
| json_crusher | 8 | 67.4% | 99.2% | 50.1% | 1.6ms |
| search | 16 | 92.4% | 99.7% | 0.0% | 5.4ms |
| code | 6 | 20.9% | 48.9% | 10.3% | 1.0ms |
| diff | 6 | 21.7% | 38.2% | 0.0% | 0.9ms |
| log_deduper | 9 | ~89% | 99.8% | 0.0% | 2.5ms |
| text | 7 | 0-94% | 93.9% | 0.0% | 0.4ms |

**Average across all compressors: 68.1% savings at 1.6ms latency, 100% reversible.**

Link: `python test/benchmark_all.py`

## Compressors

| Type | Compressor | Savings | Quality |
|---|---|---|---|
| JSON arrays | SmartCrusher | 60-99% | 0% (columnar / large-string CCR) |
| JSON arrays (200+ rows) | SmartCrusher | 90%+ | <5% (row-drop) |
| Single large JSON object | JSON Crusher | ~99% | 0% (CCR field hashing) |
| Repetitive logs | LogDeduper | 60-99% | 0% |
| Timestamp-varying logs | LogDeduper | ~98% | 0% (timestamp normalization) |
| Source code | CodeCompressor | 20-50% | 0% |
| File listings | FileLister | 40-50% | 0% |
| Search results (grep) | SearchCompressor | 50-98% | 0% |
| Git diffs | DiffCompressor | 20-40% | 0% |
| Long text (>10K) | TextSummarizer | 60-94% | <5% |
| ripgrep context output | SearchCompressor | 80%+ | 0% |

## How it works

Proteus uses **deterministic, rule-based algorithms** — no ML, no models, zero added latency:

1. **ContentRouter** detects the type of content (JSON, code, logs, search results, diffs, listings)
2. Routes to a **specialized compressor** that understands the structure
3. Compresses with structure-aware techniques (columnar format, line dedup, timestamp normalization, CCR field hashing)
4. Stores the **original in a local CCR cache** indexed by hash
5. Injects a `proteus_retrieve` tool so the LLM can fetch original data if needed

All compression is **reversible** — originals are never lost.

## Key features

- **LLM → CCR bridge**: Compressed output includes a `[proteus_retrieve:<hash>]` marker. The LLM can call `proteus_retrieve(hash=...)` to restore any piece of original data on demand.
- **Multi-turn history compression** (`history.py`): When a conversation exceeds threshold, old turns are compressed and originals stored in CCR — keeps the active window small without losing information.
- **Per-session profiles** (`profiles.py`): Conservative (lossless), Balanced (default, <5% quality loss), Aggressive (max savings). `apply_profile()` merges into any config.
- **Dashboard page** (`/proteus`): Real-time cache stats, per-compressor breakdown, profile selector.

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
pip install -e ".[dev]"

cd test && python run_all.py        # All 428 tests
python run_all.py -v                # Verbose mode
python benchmark_all.py             # 48-scenario benchmarks
```

Coverage: **88%** across 8 test suites (CCR, CLI, compressors, proxy, history, profiles, integration).

## Project structure

```
proteus/
├── src/proteus/               # Package source
│   ├── compressors/           # Specialized compressors
│   │   ├── json_crusher.py    # JSON columnar + large-string CCR
│   │   ├── log_deduper.py     # Log dedup with timestamp normalization
│   │   ├── code.py            # Code stripping (Python, JS, TS, Go, Rust)
│   │   ├── search.py          # Search/grep results + context lines
│   │   ├── diff.py            # Git diff compression
│   │   └── text.py            # Text summarization
│   ├── proxy/                 # Transparent proxy server
│   ├── ccr.py                 # Compress-Cache-Retrieve store
│   ├── router.py              # Content type detection + routing
│   ├── history.py             # Multi-turn conversation compression
│   ├── profiles.py            # Per-session compression profiles
│   └── cli/                   # CLI commands
├── test/                      # 428 tests
├── benchmarks/                # CI benchmarks
├── demos/
│   ├── weather-dashboard/     # 🌤 HTML/CSS/JS weather app demo
│   └── log-analyzer/          # 📊 Multi-service log analysis demo
└── config.yaml                # Example config
```

## Running the demos

```bash
# Weather Dashboard
cd demos/weather-dashboard
python run_proteus_test.py     # Compress + verify + cost report

# Log Analyzer
cd demos/log-analyzer
python run_demo.py             # Generate, compress, verify, analyze
```

## License

Apache 2.0 — same as upstream HeadRoom.

## Status

Alpha. Built for Hermes Agent but works with any LLM client that speaks OpenAI-compatible API. Proxy mode is functional but still has rough edges — expect improvements.

---

*Named after Proteus, the shape-shifting sea god of Greek myth who could change form while keeping his true nature — exactly what this does to your tool outputs.*
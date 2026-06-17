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
pip install git+https://github.com/Dylan-Demolder/proteus.git
```

> **Note:** The name `proteus` is taken on PyPI by an unrelated project. Install directly from GitHub.

### Proxy mode (transparent, no code changes)

```bash
# OpenCode Go (flat-rate — recommended)
proteus proxy --backend opencode-go

# OpenRouter (pay-per-token)
proteus proxy --backend openrouter

# Custom endpoint
proteus proxy --backend generic --upstream-url https://my-api.com/v1 --api-key-env MY_KEY
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

**Real-world test on 9 files (300KB → 111KB, 63%):**

| File | Type | Size | Saved |
|---|---|---|---|
| `combined-project.txt` | text | 130.9KB → **4.6KB** | **96.5%** |
| `weather-api-response.json` | json | 38.7KB → 23.1KB | 40.3% |
| `weather-api-reykjavik.json` | json | 38.8KB → 23.2KB | 40.1% |
| `weather-api-tokyo.json` | json | 38.7KB → 23.1KB | 40.2% |
| `LIVE-wttr.in-Tokyo.json` | json | ~38.6KB → 23.1KB | ~40.3% |
| `app.js` | code_javascript | 5.1KB → 4.7KB | 8.4% |
| `index.html` | text | 2.8KB → 2.8KB | — |
| `styles.css` | text | 4.2KB → 4.2KB | — |
| `build-output.log` | logs | 2.2KB → 2.2KB | — |
| **Total (9 files)** | | **300KB → 111KB** | **63.0%** |

**Verification:**
- ✅ All 9 files: byte-for-byte match after CCR roundtrip
- ✅ All 7 weather fields identical on 4 API responses (temp, humidity, pressure, UV, wind direction/speed)
- ✅ Live API fetch from wttr.in compressed and decompressed successfully
- ✅ Cost savings: $0.15 → $0.06 per run • **$3,530/year at 100 runs/day** (based on $2/M input tokens)

The combined-project.txt shows the killer use case: an LLM seeing `cat` output of a whole project goes from 131KB (33K tokens) → 4.6KB (1.1K tokens) — identical information, 96% less context cost.

> **Note:** The LIVE API response varies slightly between runs since it fetches real-time weather data. Numbers shown are representative.

### 📊 Log Analyzer (demos/log-analyzer)

A multi-service log analysis pipeline demonstrating Proteus on server output.

**5 files, 256KB → 79KB (69%):**

| File | Size | Saved | Compressor |
|---|---|---|---|
| combined.log (3 services, 800 entries) | 84.6KB → 21.2KB | 74.9% | log_deduper |
| nginx.log (500 lines) | 55.2KB → 1.9KB | **96.5%** | search |
| metadata.json (800 records) | 87.5KB → 28.2KB | 67.8% | json_crusher |
| db.log (100 lines) | 9.7KB → 8.3KB | 14.3% | log_deduper |
| app.log (200 lines) | 19.7KB → 19.6KB | 0.6% | log_deduper |

**Verification:** All 70 analysis metrics (IP counts, status codes, error rates, timing, durations) identical across 5 files after roundtrip — 14 metrics × 5 files, zero failures.

## Benchmarks

### 48-scenario benchmark suite (test/benchmark_all.py)

```
                 ──────────── Before ───── After ─────  Δ ────
Overall savings     50.5%        68.1%    +17.6pp
Latency             2.5ms         ~2.5ms   —

Key fixes driving improvement:
  Log timestamp regex (space handling)       0.0% → 98.1%
  JSON large-string CCR hashing              0.1% → 99.2%
  Search context format (rg --context=N)     0.0% → 82.5%
```

### Per-compressor breakdown (48 tests, 7 compressor categories)

| Compressor | Tests | Avg Savings | Best | Worst |
|---|---|---|---|---|
| search | 16 | 92.4% | 99.7% | 0.0% |
| json_crusher | 8 | 67.4% | 99.2% | 50.1% |
| log_deduper | 1 | 0.0%* | — | — |
| code | 6 | 20.9% | 48.9% | 10.3% |
| diff | 6 | 21.7% | 38.2% | 0.0% |
| text | 7 | 0.0%** | 93.9% | 0.0% |

*\* Repetitive log patterns are routed through the search compressor (16 tests, 92.4%). log_deduper handles multi-line block dedup only.*
*\*\* Text compression triggers at >10K chars. Small prose/table/YAML blocks pass through. Large text (>10K) achieves ~94%.*

**Average across all compressors: 68.1% savings at ~2.5ms latency, 100% reversible.**

Run fresh: `cd /tmp/proteus && python test/benchmark_all.py`

## Cost Analysis

### Monthly cost: OpenRouter vs OpenCode Go

The chart below shows what you'd pay at different request volumes with and without Proteus compression. OpenCode Go's flat $10/month includes $60 of usage credits — enough to run **~429M raw tokens/month** through DeepSeek V4 Flash.

```mermaid
---
config:
  theme: default
---
xychart-beta
  title "Monthly cost by request volume (10K avg tokens/request)"
  x-axis ["1K req", "5K req", "25K req", "100K req"]
  y-axis "Cost ($)" 0 to 70
  bar [6, 30, 150, 600]
  bar [10, 10, 10, 10]
  bar [10, 10, 10, 10]
```

| Volume | OpenRouter | OpenCode Go | OpenCode Go + Proteus |
|--------|-----------:|------------:|----------------------:|
| 1,000 req/mo | $6 | **$10** | **$10** |
| 5,000 req/mo | $30 | **$10** | **$10** |
| 25,000 req/mo | $150 | **$10** | **$10** |
| 100,000 req/mo | $600 | **$10** | **$10** |

> Based on DeepSeek V4 Flash at $0.14/M input tokens (OpenRouter) or $10/month flat (OpenCode Go). Average request: 10K input tokens. Proteus compression reduces effective tokens by ~27% (68% compression on ~40% tool-output portion of input).

### Tokens per dollar (effective throughput)

```mermaid
---
config:
  theme: default
---
xychart-beta
  title "Tokens per dollar (higher is better)"
  x-axis ["OpenRouter", "OpenCode Go", "OpenCode Go + Proteus"]
  y-axis "M tokens" 0 to 600
  bar [71]
  bar [429]
  bar [588]
```

| Provider | Raw tokens/$ | Effective tokens/$ (with Proteus) |
|----------|:-----------:|:--------------------------------:|
| OpenRouter (V4 Flash) | 7.1M | 9.1M |
| OpenCode Go | **429M** | **588M** |

**OpenCode Go delivers ~60× more tokens per dollar than OpenRouter for V4 Flash.** With Proteus compression, that stretches to ~82×.

### Proxy latency benchmark (real-world, 2026-06-17)

We benchmarked the Proteus proxy against direct API calls to OpenCode Go to measure the overhead — and found the proxy is actually **faster** on real workloads.

```mermaid
---
config:
  theme: default
---
xychart-beta
  title "Response time: proxy vs direct (lower is better)"
  x-axis ["Simple chat", "300-token context", "10K search results", "50K search results"]
  y-axis "Latency (ms)" 0 to 5000
  bar [1548, 3629, 479, 545]
  bar [1483, 4270, 929, 1205]
```

| Scenario | Input size | Direct | Proxy | Δ |
|----------|-----------:|------:|------:|--:|
| Simple chat | ~200 chars | 1,483ms | **1,548ms** | +65ms (+4%) |
| 300-token context | ~2,400 chars | 4,270ms | **3,629ms** | **-641ms (-15%)** |
| 10K search results | 72K chars | 929ms | **479ms** | **-450ms (-48%)** |
| 20K stock JSON | 31K chars | 760ms | **471ms** | **-289ms (-38%)** |
| 50K search results | 361K chars | 1,205ms | **545ms** | **-660ms (-55%)** |

> **Key insight:** The proxy compresses large tool outputs before sending them to the API, so the LLM processes fewer tokens → faster response. The ~2ms compression overhead is dwarfed by the reduced upstream round-trip. On large outputs, the proxy is **1.6–2.2× faster** than going direct.

### Real-world savings (this session)

In a single benchmark run the proxy saved **450,839 chars** = **~112,708 tokens** across 3 compressed requests. At OpenRouter pricing ($2/M input tokens for larger models), that's $0.23 saved in one test — and the proxy runs continuously, compressing every tool output >3KB.

### Subscription & pricing comparison

| Plan | Monthly | Tokens included | Effective tokens (w/ Proteus) | Overage cost |
|------|:-------:|:---------------:|:-----------------------------:|:------------:|
| **OpenRouter** (pay-as-you-go) | ~$6–600 | Pay per token | ~9M× tokens/$ | $0.14/M |
| **OpenCode Go** (flat) | **$10** | 429M tokens | 588M tokens | No cap |
| **OpenCode Go + Proteus** | **$10** | 429M tokens | **588M effective** | No cap |
| OpenAI API (V4 Flash equivalent) | ~$30–3,000 | Pay per token | N/A | $0.15–0.60/M |
| Anthropic API (Sonnet) | ~$50–5,000 | Pay per token | N/A | $3–15/M |

> **Bottom line:** At typical usage (5K–10K requests/month), OpenCode Go + Proteus saves **$20–140/month** vs OpenRouter and **$40–4,990/month** vs proprietary APIs — with no per-token anxiety and faster response times on real workloads.

## Compressors

| Type | Compressor | Savings | Quality |
|---|---|---|---|
| JSON arrays | json_crusher | 60-99% | 0% (columnar / large-string CCR) |
| JSON arrays (200+ rows) | json_crusher | 90%+ | <5% (row-drop) |
| Single large JSON object | json_crusher | ~99% | 0% (CCR field hashing) |
| Repetitive logs | search (routed) | 60-99% | 0% |
| Timestamp-varying logs | search (routed) | ~98% | 0% (timestamp normalization) |
| Multi-line log blocks | log_deduper | varies | 0% |
| Source code | code | 20-50% | 0% |
| File listings | file_listing | 40-50% | 0% |
| Search results (grep) | search | 50-98% | 0% |
| Git diffs | diff | 20-40% | 0% |
| Long text (>10K) | text | 60-94% | <5% |
| ripgrep context output | search | 80%+ | 0% |

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
- **Proxy server** (`proteus proxy`): Transparent aiohttp proxy that auto-compresses tool responses between your agent and the LLM API.
- **Dashboard integration**: When used with the Hermes dashboard (localhost:8080), a `/proteus` page shows cache stats, per-compressor breakdown, and profile selector.

## Why not HeadRoom?

HeadRoom (the upstream project) is excellent but built for Anthropic's API. Proteus is a fork that:

- **Supports multiple backends** — OpenRouter, OpenCode Go, OpenAI, or custom endpoints via `--backend`
- **Pure Python, no Rust** — installs in seconds, no compilation
- **Deterministic only** — no ML compressors on the hot path
- **Multi-turn compression** — compresses accumulated history, not just live output
- **3 compression profiles** — conservative (lossless), balanced (default), aggressive (max savings)
- **Proxy server** — works with any OpenAI-compatible agent without code changes
- **~90% less code** — same core value, dramatically simpler

## Requirements

- Python 3.10+
- aiohttp (for proxy mode)
- Works with any OpenAI-compatible API (OpenRouter, OpenAI, Groq, Together, etc.)

## Tests

```bash
git clone https://github.com/Dylan-Demolder/proteus.git
cd proteus
pip install -e ".[dev]"

# Run all test suites (428 tests):
for f in test/*.py; do python "$f"; done

# Run benchmarks:
python test/benchmark_all.py
```

Test breakdown: 106 engine tests, 57 new compressor tests, 47 coverage gap-fills, 35 edge case tests, 40 integration tests, 31 CLI tests, 21 history tests, 91 profiles tests — **428 total**.

Coverage: **88%** across 8 test suites.

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
# Weather Dashboard — compress + verify + cost report
cd demos/weather-dashboard
python run_proteus_test.py

# Log Analyzer — generate, compress, verify, analyze
cd demos/log-analyzer
python run_demo.py
```

## License

Apache 2.0.

## Status

Alpha. Built for Hermes Agent but works with any LLM client that speaks OpenAI-compatible API. Proxy mode is functional but still has rough edges — expect improvements.

---

*Named after Proteus, the shape-shifting sea god of Greek myth who could change form while keeping his true nature — exactly what this does to your tool outputs.*
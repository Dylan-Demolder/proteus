# Proteus Dogfood Demo

A practical demonstration of Proteus compression on real-world LLM tool output.

## What it does

1. Generates ~800 lines of realistic multi-service log data (nginx + app server + database)
2. Compresses each file with Proteus using the best-fit compressor
3. Runs the same log analysis on both original and compressed data
4. **Proves the analyses are identical** — all metrics match exactly
5. Reports compression ratios and cost savings

## Why this matters

LLMs process tool output token-by-token. If you can shrink output 60-80% without losing information, you save:
- **Context window** — more room for reasoning, less truncation
- **Latency** — fewer tokens to generate the next response
- **Cost** — fewer tokens → lower API bills

Proteus preserves **every byte** via CCR cache. No approximations, no summaries — 100% reversible.

## Quick start

```bash
cd /tmp/proteus-demo
pip install proteus  # from GitHub or PyPI
python run_demo.py
```

## Example output

```
  combined.log        152.0KB →  41.5KB  (72.7%)
  nginx.log            83.4KB →  17.5KB  (79.1%)
  app.log              62.7KB →   4.1KB  (93.5%)
  db.log                8.5KB →   1.9KB  (77.7%)
  metadata.json         5.9KB →   2.0KB  (65.4%)
  ──────────────────────────────────────────────
  TOTAL               312.5KB →  67.0KB  (78.6%)

  ✅ combined.log     — ALL METRICS MATCH (0 diffs)
  ✅ nginx.log        — ALL METRICS MATCH (0 diffs)
  ✅ app.log          — ALL METRICS MATCH (0 diffs)
  ✅ db.log           — ALL METRICS MATCH (0 diffs)
  ✅ metadata.json    — ALL METRICS MATCH (0 diffs)

  🟢 ALL ANALYSES IDENTICAL — Proteus compression is lossless in practice
```

## Files

| File | Purpose |
|---|---|
| `run_demo.py` | Orchestrator — generate, compress, analyze, verify |
| `generate_logs.py` | Realistic multi-service log generator |
| `analyze_logs.py` | Log analysis (error counts, IPs, paths, durations) |
| `data/` | Generated logs (gitignored) |

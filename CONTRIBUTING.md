# Contributing to Proteus

Proteus compresses tool output for OpenAI-compatible LLM backends with deterministic, rule-based compressors. Keep it lean.

## Principles

- **No ML on the hot path.** Compression is rule-based and takes milliseconds.
- **Pure Python.** Dependencies are the stdlib plus aiohttp, click and pyyaml.
- **OpenAI-compatible APIs only.** Nothing provider-specific in the core.
- **Every original is recoverable.** The CCR cache stores it under the hash in the marker.
- **Say what was left out.** A compressor that drops content must report it, so the marker can tell the model what's missing.
- **Never make it worse.** If a compressor can't handle its input, it returns it unchanged rather than crashing or producing something larger.

## Setup and checks

```bash
pip install -e ".[dev]" pillow

for f in test/run_all.py test/test_*.py; do python "$f" || exit 1; done   # every suite, as CI runs them
ruff check src/ test/ benchmarks/
mypy
coverage erase && for f in test/run_all.py test/test_*.py; do coverage run --append "$f" >/dev/null; done && coverage report   # CI fails under 80%

python test/benchmark_all.py                  # 48 compression scenarios
python benchmarks/live_eval.py --dry-run      # which answers survive compression, no API calls
```

The suites are flat scripts with a `check()` counter, not pytest classes. Each exits non-zero on failure, and CI runs the list in `.github/workflows/ci.yml` (`SUITES`). When you add a suite, add it there too.

## Layout

```
src/proteus/
  __init__.py          compress_tool_output(): detect type → compress → cache
  router.py            content-type detection
  compressors/         json_crusher, log_deduper, code, search, diff, text
  ccr.py               the cache of originals (~/.proteus/cache)
  history.py           compress_history() for older conversation turns
  config.py            settings, config files; profiles.py bundles them
  proxy/
    server.py          aiohttp proxy: forwarding, retrieve rounds, sessions
    handler.py         request transform, markers, proteus_retrieve answers
    stream.py          retrieve rounds inside streamed replies
    backends.py        openrouter, opencode-go, openai, generic
  cli/commands.py      the proteus command
test/                  test suites (see above)
benchmarks/            live_eval.py, agent_eval.py, media generators
docs/                  eval findings, README images
```

## Adding a compressor

1. Add `src/proteus/compressors/<name>.py` with `def compress_<type>(content: str, ...) -> tuple[str, dict]`. The stats dict needs `mode`, `original_chars` and `compressed_chars`, plus whatever the marker needs to say what was dropped.
2. Detect the type in `router.py` and dispatch to it in `compress_tool_output()` in `__init__.py`.
3. Describe what it drops in `_what_changed()` in `proxy/handler.py`. That text is what the model reads.
4. Add tests, including one that fails without your change, and run every suite.
5. Add a row to the README's content table.

## Measuring against real models

Compression ratios don't show whether the model still answers correctly. For any change to what the model sees, run the evals before and after (they need `OPENCODE_GO_API_KEY`):

```bash
python benchmarks/live_eval.py --model deepseek-v4.1-flash --repeat 10 --concurrency 10
python benchmarks/agent_eval.py --model deepseek-v4.1-flash --repeat 6 --concurrency 12
```

Record the results in [`docs/live-eval-results.md`](docs/live-eval-results.md).

## README images

The screenshots and GIFs are generated from real output, so regenerate them after any change to a compressor or the marker:

```bash
python benchmarks/make_readme_media.py --model deepseek-v4.1-flash   # screenshots + proxy GIF; omit --model to use a local stand-in model
python benchmarks/make_demo_gif.py                                   # the CLI GIF
```

## Pull requests

- One change per PR, with tests, and the README and `CHANGELOG.md` updated if users would notice.
- Conventional commit messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.

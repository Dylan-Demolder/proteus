# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The proxy now compresses standard OpenAI tool results.** It only looked
  for `{"type": "tool_result"}` blocks, which the OpenAI chat format doesn't
  use. `{"role": "tool", "content": ...}` messages, which is how every
  OpenAI-compatible agent sends tool output, went upstream untouched. Both
  string content and lists of text parts are now compressed.
- **The proxy now compresses streaming requests.** Compression was skipped
  whenever `stream: true`. Most agents stream, so for them the proxy did
  nothing. Compression applies to the request, so whether the response streams
  is irrelevant.
- **`proteus_retrieve` works.** The proxy offered the model this tool but never
  answered calls to it, so the agent received a call to a tool it didn't
  define. Compressed output also carried no hash to pass it. Compressed tool
  results now end with a marker naming the hash. For non-streaming requests,
  the proxy answers the model's retrieve calls from the cache and asks again,
  so the client never sees the tool, and token usage is summed across rounds.
  Streaming requests don't get the tool, because the proxy can't intercept a
  stream. The optional `query` argument (filter to matching lines) is now
  implemented.
- **The code compressor no longer deletes code.** In JS/TS/Go/Rust, a line
  with an inline `/* comment */` made every following line disappear up to the
  next `*/`. So did a string containing `/*`, such as the glob
  `"src/**/*.ts"`. In Python, any line starting with `"""`, for example SQL in
  a triple-quoted string, was dropped as a "docstring", along with `#` lines
  inside strings. The strippers now track string literals (Python uses the
  tokenizer), keep build directives (`//go:build`, `/// <reference>`), and
  replace a docstring that is a block's only statement with `...` so the code
  still compiles.
- **Columnar JSON is lossless, as documented.** Values were written with
  `str()`, which produced Python reprs (`True`, `{'x': 1}`), broke rows on
  embedded newlines and quotes, and couldn't tell `null` from `""`. Cells are
  now bare strings or JSON, and the format decodes back to exactly the input.
- **JSON arrays of strings or numbers crashed** with `AttributeError` (for
  example, a list of file paths).
- **Row-drop `hash=` markers pointed at nothing.** The hash was computed from
  re-serialized JSON rather than the input text, so `retrieve()` missed
  whenever the input was pretty-printed.
- **`ccr.retrieve()` followed path traversal.** A hash like `../x` read a file
  outside the cache directory, and the model now supplies hashes. Only hex
  hashes are accepted. Cache entries are also written atomically.
- **Proxy pass-through doubled `/v1`.** `GET /v1/models` was forwarded as
  `.../v1/v1/models`. Content-Type is now forwarded on pass-through requests.
- **Non-JSON upstream errors** (an HTML 502 page, a plain-text 429) are relayed
  with their status instead of being replaced by a generic 502. Errors on
  streaming requests are relayed as JSON instead of being labelled SSE.
- **Long responses were cut off after 120s.** The timeout now bounds connect
  time and time between bytes, not total duration. Timeouts return 504.
- The proxy's upstream connection pool is closed on shutdown ("Unclosed client
  session" warning).

### Added

- `test/test_proxy_fidelity.py`: 68 regression tests covering the above,
  including end-to-end proxy tests against a local mock upstream. Each one was
  confirmed to fail on the previous code.

## [0.2.0] - 2026-09-24

Distribution renamed to `proteus-compress` in preparation for PyPI.
**Not yet published** — install from GitHub (see the README quick start);
the `publish.yml` workflow and the `v0.2.0` tag are ready for when it is.

### Added

- **CI runs the full test suite.** All 8 suites (428 tests) now execute on every
  push and pull request across Python 3.10–3.14. Previously only 2 of 8 suites
  ran, and neither returned a non-zero exit code on failure — so the build
  badge could not actually go red.
- **Coverage gate.** Coverage is measured in CI and fails the build below 80%.
  Actual coverage is 83% (the README previously claimed 68%).
- **Linting (`ruff`) and type checking (`mypy`)** as CI jobs. Both are clean.
- **`py.typed`** marker — the package now ships inline type information and
  declares the `Typing :: Typed` classifier.
- **Issue templates, pull request template, `SECURITY.md`, and Dependabot**
  configuration for GitHub Actions and pip dependencies.
- **`CHANGELOG.md`** (this file).
- Python 3.13 and 3.14 to the test matrix and trove classifiers.
- **Hermetic proxy integration tests.** `test_chat_completions` and
  `test_unknown_route` made *live* calls to `openrouter.ai`, so CI depended on
  a third party's uptime and status codes — `2d8dbfb` had already weakened the
  assertions to "non-2xx"/"non-5xx" because the real upstream kept moving them.
  They now run against a local mock upstream on `127.0.0.1`, which allows
  **exact** assertions: `200` + echoed body for pass-through, `200` + relayed
  body for route forwarding, `401` relayed rather than `502`. Two tests added
  (`test_upstream_error_is_relayed`, `test_no_live_network_calls`):
  6 → 8 integration tests. Verified passing with a deliberately broken TLS
  trust store and with all network proxied to a dead port.

### Changed

- **Package renamed `proteus` → `proteus-compress`** for PyPI. The `proteus`
  name was already taken by an unrelated project. The import name and CLI
  entry point (`proteus`) are unchanged — only the distribution name differs:
  `pip install proteus-compress`.
- **`stats` dict from `compress_tool_output()` always includes**
  `compression_pct`, `estimated_token_savings`, `chars_saved`, `compressed_chars`,
  and `hash`. Previously these keys were absent when input fell below the
  compression threshold, which made the README's own example raise `KeyError`.
- Version bumped to 0.2.0.
- Replaced the 14-line LICENSE stub with the full canonical Apache-2.0 text,
  so GitHub now recognises the license as Apache-2.0 rather than "Other".
- Removed dead code: unreachable `first_nonempty` in `router.py`, unused
  `hunk_count`/`current_file` in `diff.py`, and a no-op `get_backend()` call
  in `start_proxy()` (`ProteusProxy.__init__` resolves the backend itself).
- 107 lint fixes across the codebase (unsorted imports, unused imports,
  whitespace, f-strings, pyupgrade idioms).

### Fixed

- **Unrecoverable output when a compressor produced a larger result than its
  input.** `compress_tool_output()` ran the compressor, found the output was
  *bigger* than the input, skipped CCR storage (the `len(compressed) <
  len(content)` guard) — but still returned the mutated payload. Callers saw
  `was_compressed=False, hash=""` on bytes that had silently changed, with no
  way to recover the original. This violated the "originals are never lost"
  guarantee on 2 of the 48 benchmark scenarios (misdetected content type on
  deeply nested JSON, and multi-line log entries with no redundancy). Now the
  original is returned untouched whenever compression doesn't pay off.
  Benchmark: **15 → 0 non-reversible cases; `rev: True` across all 48.**
- **`proteus clear` could crash under concurrency.** `ccr.clear()` called
  `unlink()` on paths from a prior `glob()`; if another process removed a file
  first, `FileNotFoundError` propagated out of the Click command as a
  non-zero exit. Same class of bug as the `_maybe_evict()` race above —
  this was missed the first time round. Now skips vanished entries.
  Verified with 8 concurrent workers × 20 clear+store cycles: 0 crashes.
- **`FileNotFoundError` race in the CCR cache under concurrency.**
  `_maybe_evict()` called `os.path.getmtime()` on paths from a prior `glob()`,
  and `stats()` did the same with `stat()`. If another process evicted a file
  in between — which the concurrent proxy makes routine — the call raised and
  took the request down. Both now skip vanished entries. Verified with 6
  concurrent workers × 40 store+stats cycles: 0 crashes.
- **Benchmark reported `rev: False` for passthrough inputs.** Inputs below the
  compression threshold are never stored, so there was no hash to retrieve;
  the benchmark counted that as non-reversible even though output == input.
  Now distinguishes "not compressed" from "compressed and unrecoverable".
- **`test/run_all.py` shared the global CCR cache**, so running suites
  concurrently raced on `clear()`/`store()` and its absolute entry-count
  assertions (`== 0`, `== 10`) failed intermittently. It now redirects
  `config.CCR_CACHE_DIR` to a private temp dir for the life of the process —
  the same technique `test_coverage.py` already used — and cleans up on exit.
  Running the suite no longer pollutes `~/.proteus/cache` either.
  Verified: 3 concurrent loops × 8 suites = 24 runs, all exit 0.
- **`proteus retrieve <hash>` could never work.** The Click command function
  was named `retrieve`, shadowing the imported `ccr.retrieve`. The callback
  therefore invoked the Click `Command` object with the hash string as argv,
  exploding it into individual characters:

  ```
  $ proteus retrieve f7f0415a1dbc
  Error: Got unexpected extra arguments (7 f 0 4 1 5 a 1 d b c)
  ```

  Now imports as `ccr_retrieve` and returns the original content. Verified
  end-to-end: compress → retrieve → byte-identical original.
- **Latent `TypeError` in SSE streaming.** `StreamResponse.prepare()` requires
  a `BaseRequest`, but `_forward_stream()` was typed `web.Request | None` and
  defaulted to `None`. A streaming response without a request object would
  crash inside aiohttp. `_forward_stream()` now requires the request, and the
  call site returns an explicit 500 instead of raising. (No streaming test
  previously existed; the SSE path was only reachable from `handle_chat_completions`.)
- **Test scripts could not fail the build.** `test/run_all.py` and
  `test/test_new.py` printed `❌ N failures` but always exited 0.

## [0.1.1] - 2026-06-17

### Added

- Multi-backend proxy: `openrouter`, `opencode-go`, `openai`, `generic`
  (`--backend`, `--upstream-url`, `--api-key-env`).
- Multi-turn history compression (`history.py`) and per-session compression
  profiles (`profiles.py`).
- Proxy health watchdog for graceful failover.
- Demo showcases: weather dashboard and log-analyzer.
- Cost analysis documentation (4-route comparison).

### Fixed

- SSE streaming: removed a premature `break` on `end_of_chunk` that dropped all
  but the first event.
- SSE streaming: pass `request` to `StreamResponse.prepare()`.
- Proxy now handles `text/event-stream` responses instead of crashing.
- Ported cache paths from `~/.hermes/` to `~/.proteus/`.
- Guarded `sys.exit` in test scripts.

## [0.1.0] - 2026-06-16

Initial release.

- ContentRouter with 7 specialized compressors: `json_crusher`, `log_deduper`,
  `code`, `search`, `diff`, `text`, file listings.
- CCR (Compress-Cache-Retrieve) store — all compression reversible by hash.
- CLI: `proteus file`, `proteus cache`, `proteus stats`, `proteus retrieve`,
  `proteus clear`.
- Library API: `compress_tool_output()`, `compress_summary_line()`.
- `src/` layout for PyPI packaging.

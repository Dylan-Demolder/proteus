# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-24

First release published to PyPI as `proteus-compress`.

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

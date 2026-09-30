# Live evaluation: findings and fixes (2026-09-30)

Two models on [OpenCode Go](https://opencode.ai/docs/go/): `deepseek-v4.1-flash`
and `mimo-v2.6-flash`. [`benchmarks/live_eval.py`](../benchmarks/live_eval.py)
ran each scenario once direct to the API and once through an in-process Proteus
proxy, repeated for many trials. This page records what those runs showed, what
was changed in response, and what is still open.

Reproduce:

```bash
export OPENCODE_GO_API_KEY=...
python benchmarks/live_eval.py --model deepseek-v4.1-flash --repeat 20 --concurrency 10 --json live.json
python benchmarks/live_eval.py --model mimo-v2.6-flash --repeat 20 --concurrency 10
```

## How to read the results

Each scenario is one agent turn: the model called a tool, got a large result,
and is asked a question whose answer is somewhere in it. There are four
possible outcomes:

- **correct**: every expected fact is in the answer.
- **wrong**: the model answered, and the answer is wrong.
- **tool call**: instead of answering, the model re-ran its own tool to
  double-check (`rg ...`, `kubectl logs ... | grep ...`). That isn't wrong,
  but the harness can't run the command, so it isn't counted as correct.
- **HTTP error**: the request failed.

Prompt tokens are the ones the API billed, summed over the proxy's
`proteus_retrieve` rounds. Results vary between runs even at temperature 0.
One trial per scenario is anecdote, so the tables below use 20.

## Final results (20 trials per scenario and mode)

**deepseek-v4.1-flash**

| scenario | direct correct | Proteus correct | direct tokens | Proteus tokens | retrieves |
|---|---|---|---|---|---|
| json_columnar | 20/20 | 20/20 | 14,990 | 6,084 | 0.0 |
| json_dropped_rows | 20/20 | 20/20 | 16,690 | 2,243 | 1.0 |
| log_errors | 18/20 (2 tool call) | 18/20 (2 tool call) | 28,207 | 13,725 | 1.1 |
| text_middle_fact | 20/20 | 20/20 | 4,691 | 3,174 | 1.1 |
| grep_capped | 7/20 (13 tool call) | 15/20 (5 tool call) | 8,071 | 1,773 | 0.3 |
| code_function | 20/20 | 20/20 | 5,949 | 2,630 | 0.1 |
| diff_many_files | 17/20 (3 tool call) | 18/20 (2 tool call) | 2,329 | 2,329 | 0.0 |
| **total** | **122/140** | **131/140** | **1,618,540** | **639,155 (−61%)** | |

**mimo-v2.6-flash**

| scenario | direct correct | Proteus correct | direct tokens | Proteus tokens | retrieves |
|---|---|---|---|---|---|
| json_columnar | 20/20 | 20/20 | 16,342 | 7,759 | 0.0 |
| json_dropped_rows | 20/20 | 20/20 | 18,844 | 2,092 | 1.1 |
| log_errors | 17/20 (3 tool call) | 13/20 (7 tool call) | 39,750 | 14,570 | 0.7 |
| text_middle_fact | 20/20 | 20/20 | 4,742 | 2,972 | 1.1 |
| grep_capped | 20/20 | 14/20 (6 tool call) | 8,090 | 1,337 | 0.1 |
| code_function | 20/20 | 20/20 | 6,469 | 2,827 | 0.0 |
| diff_many_files | 15/20 (5 tool call) | 15/20 (5 tool call) | 2,258 | 2,258 | 0.0 |
| **total** | **132/140** | **122/140** | **1,929,900** | **676,307 (−65%)** | |

In 560 requests, neither model gave a wrong answer in either mode. Every miss
is a tool call. `diff_many_files` is sent through Proteus byte-for-byte unchanged
(see finding 5), so the gap between its two columns is only run-to-run noise.

## Findings and fixes

### 1. Every request failed: OpenCode Go requires a session header

The first run returned `400 MissingSessionID` for all 14 requests, direct and
through Proteus. OpenCode Go now requires an `x-opencode-session` header, a
stable ID per conversation, and asks clients to send their own User-Agent
rather than a generic HTTP library's. Its docs also ask proxies to *preserve
the session header when forwarding requests*. The proxy forwarded only
`X-Title` and `HTTP-Referer`, so even a client that sent the header lost it on
the way through.

**Fix.** The proxy forwards `User-Agent` and any session header
(`x-opencode-*`, Codex's `session_id`, `x-…-session-id`). Without a client
User-Agent it sends `proteus/<version>`. The `opencode-go` backend declares
`session_header="x-opencode-session"`. If the client sent no session header, the
proxy derives one from the model plus the messages up to and including the first user
message, so every turn of a conversation gets the same ID and different
conversations get different ones. The eval now sends a session ID and
User-Agent on every request.

### 2. `proteus_retrieve` queries almost never matched

A query had to appear verbatim in a single line. Queries models actually sent:

| query | old result |
|---|---|
| `"staging database port"` | no match, so a second round |
| `"mod_24.py VERSION"` | no match, then fetched everything |
| `"timeout = [0-9]"` (a regex) | fell through to the full original |
| `"refunded"` | one line, `"status": "refunded",`, with no `order_id`, so the model asked again |

**Fix.** The query is tried as literal text, then as a regex, then as lines
containing every word, then as lines containing any word. Matches come back
with two lines of context on each side, like `grep -C 2`, and a header saying
these are *all* the matching lines in the original. When the filtered result
would be no shorter than the original, the original is returned instead. The
tool description now says the query accepts text or a regex.

### 3. Models re-fetched content that was complete

The marker said only `compressed 23,138→6,609 chars`. A careful model can't tell
from that whether its answer might be in the removed part. `code_function`
retrieved in 3 of 10 trials, which made it cost *more* than going direct
(6,623 vs 5,949 tokens), even though comment stripping leaves every line of
code.

**Fix.** The marker now says what changed, per compressor:

- `all 300 rows kept in columnar form, nothing dropped`
- `480 of 500 rows not shown`
- `comments and docstrings removed, code unchanged`
- `repeated and routine lines trimmed, every error line kept`. The log deduper
  now reports `errors_dropped`, and its `... N lines omitted ...` lines say
  when no errors were among them.
- `32 of 321 matches shown, from 8 of 40 files; every match unlike the others is among those shown`
- `start and end kept, 16,430 chars from the middle not shown`

It also mentions the query option. We compared three marker versions over 40
trials each on `log_errors` + `grep_capped`:

| marker variant | MiMo | DeepSeek | total |
|---|---|---|---|
| A: says what changed | 23/40 | 28/40 | 51/80 |
| **B: A + `or add query="..." (text or regex)`** | 24/40 | 37/40 | **61/80** |
| C: B, with the marker moved to the top of the output | 24/40 | 33/40 | 57/80 |

B shipped. `code_function` now retrieves in about 1 trial in 20.

### 4. Search results miscounted and could hide the one odd match

`grep_capped` is 321 matches of `timeout = get_setting(...)` and one
`timeout = 4711  # FIXME hard-coded`. Two problems:

- **Counts were wrong.** The compressor picked the top 15 files and stopped
  at the match cap after 8 of them, then said `... 7 more files with matches ...`.
  The other 25 files went unmentioned, and the stats said 15 files were shown.
- **Scoring couldn't find the outlier.** Matches were scored by keyword, and
  `timeout` is a high-signal keyword, so all 322 scored the same. The FIXME line
  survived only because it happened to be the last match in its file.

MiMo, seeing an obviously partial list, re-ran `rg` in 10 of 15 trials.

**Fix.** Each match is reduced to a shape (numbers become `N`, strings `'…'`).
A match whose shape occurs at most twice gets a large score boost, and files
are ranked by their best match first. The hidden matches are then summarized
by shape:

```
... 289 more matches not shown, 32 more files with matches ...
[not shown, by shape (numbers as N, strings as '…'): 289× `timeout = get_setting('…')`]
```

MiMo `grep_capped` through Proteus went from 5/15 to 14–16/20. It is still
below direct (see open issues).

### 5. Compressing for small savings hid answers

The 4,619-char diff was cut to 3,662 chars by dropping files past the cap,
including the one the question asked about. A 20% saving (about 240 tokens)
cost a retrieve round that re-sent everything. Through Proteus the scenario
cost 9,204 prompt tokens against 2,329 direct.

**Fix.** A new setting, `min_savings_pct` (default 25). The proxy sends the
original when compression would save less than that share.

### 6. Eval harness fixes

- `max_tokens: 300` was too small for reasoning models. Some answers came back
  empty with `finish_reason=length`, both direct and after a retrieve. It is
  now 2000.
- Added `--repeat` and `--concurrency`, per-scenario results over all trials,
  retries on 429/5xx, and the tool call outcome.
- Added an `X-Proteus-Retrievals` response header on proxied replies, so the
  retrieve count is per request even with requests in flight concurrently.
- `--dry-run` now goes through the proxy's own request transform, so it shows
  what the model is actually sent (including the `min_savings_pct` skip).

## Progression (deepseek-v4.1-flash)

| stage | trials | direct | Proteus | Proteus tokens vs direct |
|---|---|---|---|---|
| before any fix | 1 | 0/7 (HTTP 400) | 0/7 (HTTP 400) | — |
| session header fixed | 1 | 5/7 | 7/7 | −45% (diff scenario at 4× direct) |
| + retrieve queries, `min_savings_pct` | 10 | 59/70 | 61/70 | −56% |
| + markers, search shapes (final) | 20 | 122/140 | 131/140 | −61% |

## Still open

- **MiMo double-checks compressed logs and search results.** With every error
  line kept and the marker saying so, MiMo still re-runs `kubectl logs | grep`
  in about a third of `log_errors` trials ("Let me verify I have every failure
  by grepping the full log directly"). In a real agent loop that's one extra
  tool round rather than a wrong answer, but it cancels part of the saving.
  Direct is still ahead on `log_errors` (17/20 vs 13/20) and `grep_capped`
  (20/20 vs 14/20) for MiMo.
- **Tool calls aren't executed.** The harness scores a model that re-runs its
  tool as not correct. Running a fake of the tool (returning the same output,
  compressed again through the proxy) would measure what an agent actually
  pays.
- **Seven synthetic scenarios.** Real agent traffic has longer conversations,
  several tool results per turn, and streaming. Streaming requests are
  compressed but can't use `proteus_retrieve`.

# Live evaluation: findings and fixes (2026-09-30)

Two models on [OpenCode Go](https://opencode.ai/docs/go/): `deepseek-v4.1-flash`
and `mimo-v2.6-flash`. [`benchmarks/live_eval.py`](../benchmarks/live_eval.py)
ran each scenario once direct to the API and once through an in-process Proteus
proxy, repeated for many trials. [`benchmarks/agent_eval.py`](../benchmarks/agent_eval.py)
then ran multi-turn, streamed agent loops with prompt caching (see
[Agent evaluation](#agent-evaluation)). This page records what those runs
showed, what was changed in response, and what is still open.

Reproduce:

```bash
export OPENCODE_GO_API_KEY=...
python benchmarks/live_eval.py --model deepseek-v4.1-flash --repeat 20 --concurrency 10 --json live.json
python benchmarks/live_eval.py --model mimo-v2.6-flash --repeat 20 --concurrency 10
python benchmarks/agent_eval.py --model deepseek-v4.1-flash --repeat 8 --concurrency 10   # see "Agent evaluation"
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

## Agent evaluation

The runs above are single turns: one question, one tool result, non-streamed.
Real agents differ in three ways that affect whether Proteus pays off. They
**stream**. They run **many turns**, re-sending the whole conversation each
time. And providers **cache** that repeated prefix and bill it at a fraction
of the price. On OpenCode Go, cached input costs $0.003 per 1M tokens for
DeepSeek v4.1 flash (off-peak) against $0.15 uncached, and $0.0028 against
$0.14 for MiMo v2.6 flash. So a token Proteus saves on a re-sent, cached turn
is worth about 2% of one saved the first time an output enters the
conversation.

[`benchmarks/agent_eval.py`](../benchmarks/agent_eval.py) measures that. The
model gets a task and four tools (`list_files`, `read_file`, `search`,
`http_get`) over a synthetic repository of 46 files (165K chars) and an orders
API (41K chars). The harness executes each call and returns the full-size
result, looping until the model answers (up to 15 calls). Every request
streams, with one session ID per conversation. The four tasks each need
several tool calls:

- `gateway_error`: find a gateway error code in the payment log, then the
  function that raises it.
- `refunds`: list the refunded orders in the API and total them.
- `staging_port`: find where the runbook and the settings file disagree.
- `hardcoded_timeout`: find the hard-coded timeout, then the value of the
  setting it should use.

### Streaming needed its own retrieve support

Until this change, a streamed request was compressed but not offered
`proteus_retrieve`, so content the compressor dropped was out of reach. The
proxy now relays each streamed round as it arrives and holds back the retrieve
calls. When a round asked only for retrieves, the proxy answers them from the
cache and streams the next round into the same response
([`proxy/stream.py`](../src/proteus/proxy/stream.py)). In the runs below, the
proxy answered 3.6 retrieves per `refunds` run on DeepSeek and 1.5 on MiMo,
all inside streamed replies.

### Adding the tool mid-conversation broke caching

The first agent runs showed Proteus with a lower cache hit rate than direct on
DeepSeek: 50% against 62% of prompt tokens served from cache. This is why
`staging_port` cost more through Proteus despite fewer tokens.

The cause: the proxy added `proteus_retrieve` only once something had been
compressed. An agent's first turn has no tool output yet, so the tools list
changed between turns one and two. The tools list comes first in the prompt,
so that change invalidated the whole cached prefix. The proxy now offers the
tool on every request that carries tools. After the fix, the hit rate was 57%
through Proteus against 61% direct on DeepSeek, and 56% against 39% on MiMo.

### Results (8 runs per task and mode, after both fixes)

**deepseek-v4.1-flash**

| task | mode | correct | model calls | prompt tokens | cached | est. cost/run |
|---|---|---|---|---|---|---|
| gateway_error | direct | 8/8 | 3.6 | 13,403 | 67% | $0.00129 |
| | Proteus | 8/8 | 3.6 | 16,450 | 66% | $0.00145 |
| refunds | direct | 8/8 | 5.8 | 363,776 | 61% | $0.02414 |
| | Proteus | 7/8 | 3.8 | 82,304 | 53% | $0.00892 |
| staging_port | direct | 8/8 | 3.4 | 25,671 | 56% | $0.00205 |
| | Proteus | 8/8 | 3.6 | 27,104 | 55% | $0.00223 |
| hardcoded_timeout | direct | 8/8 | 4.2 | 91,626 | 55% | $0.00668 |
| | Proteus | 8/8 | 3.9 | 24,732 | 60% | $0.00199 |
| **total** | direct | **32/32** | | 3,955,808 | 61% | **$0.2732** |
| | Proteus | **31/32** | | 1,204,724 | 57% | **$0.1168 (−57%)** |

**mimo-v2.6-flash**

| task | mode | correct | model calls | prompt tokens | cached | est. cost/run |
|---|---|---|---|---|---|---|
| gateway_error | direct | 8/8 | 4.0 | 6,230 | 63% | $0.00045 |
| | Proteus | 8/8 | 4.6 | 8,840 | 67% | $0.00055 |
| refunds | direct | 8/8 | 2.2 | 26,148 | 24% | $0.00242 |
| | Proteus | 8/8 | 2.0 | 6,324 | 49% | $0.00072 |
| staging_port | direct | 8/8 | 3.9 | 8,345 | 22% | $0.00106 |
| | Proteus | 8/8 | 3.8 | 8,937 | 45% | $0.00080 |
| hardcoded_timeout | direct | 8/8 | 3.4 | 22,487 | 35% | $0.00193 |
| | Proteus | 8/8 | 3.8 | 8,086 | 73% | $0.00041 |
| **total** | direct | **32/32** | | 505,681 | 39% | **$0.0468** |
| | Proteus | **32/32** | | 257,498 | 56% | **$0.0199 (−57%)** |

What this shows:

- **Big outputs are where the money is.** `refunds` pulls in a 41K-char API
  response and `hardcoded_timeout` reads large files. On these tasks, cost
  drops 63–79%. On direct `refunds`, DeepSeek also re-fetched the API with
  made-up pagination parameters, 12 tool calls on average, because it
  couldn't be sure it had everything. Through Proteus, the marker says
  "480 of 500 rows not shown" and a query gets exactly the refunded rows.
- **Small tasks are a wash.** `gateway_error` and `staging_port` mostly use
  `search`, whose results are already small. There the difference is within
  run-to-run noise, from about 25% cheaper to 22% dearer.
- **The cost saving is smaller than the token saving** (−57% cost against
  −70% and −49% prompt tokens), because cached re-sends were already cheap.
- **One wrong answer.** In one DeepSeek `refunds` run through Proteus, the
  model gave the correct total of the four refunded orders but the wrong four
  order ids. The retrieve result it had asked for listed each id two lines
  above its `refunded` status, so this is a misread. The line-number
  prefixes in retrieve results (`465-  "order_id": 5088,`) may make that
  easier to get wrong. No wrong answers in the other 63 Proteus runs or in
  64 direct runs.

## Harder agent tasks

Four more agent tasks, each with its answer where a compressor drops or
splits content:

- `coupon_orders`: list the orders with one coupon and total them. The IDs
  are three lines above the coupon in the pretty-printed JSON.
- `slowest_request`: the slowest request in a 1,500-line log, with one 2,950 ms
  request among ones under 100 ms.
- `runbook_owner`: a fact in the middle of the runbook.
- `retry_settings`: values from two places in the settings file, plus code.

First run (6 runs per task and mode): DeepSeek through Proteus 22/24 against
24/24 direct, and MiMo 23/24 against 24/24. One DeepSeek miss was the scorer
not recognising "2,950 ms" (fixed in the harness). The other three were
Proteus problems:

1. **Retrieve results split JSON records.** A query for `SAVE8` returned the
   coupon lines with two lines of context, which doesn't reach the
   `order_id`. DeepSeek said so ("I need the order_ids, which aren't within
   the 2-line context"), asked again, and ran out of rounds. **Fix:** for a
   JSON array of objects, a query returns the matching records whole.
2. **Running out of rounds produced a non-answer.** The turn ended with
   "Now I need the order_id values..." or with nothing at all. **Fix:**
   after the last retrieve the proxy asks once more with `tool_choice: "none"`
   (both models accept it), and tells the model to say what it couldn't check.
3. **The log compressor never deduplicated access-log lines.** Lines differing
   only in numbers were separate patterns, so it kept head and tail, and
   the slow request in the middle was hidden. The marker still said "every
   error line kept", which was true but beside the point. **Fix:** routine
   lines group with numbers ignored, and values at least 5× the group median
   are kept as `[outlier]` lines. The payment log went from 11,182 to 1,895
   chars with the slow request visible.

### Results after the fixes (8 tasks × 6 runs per mode)

| model | direct correct | Proteus correct | prompt tokens | cache hit rate | est. cost |
|---|---|---|---|---|---|
| deepseek-v4.1-flash | 48/48 | 48/48 | 4.48M → 1.21M | 53% → 62% | $0.366 → $0.107 (−71%) |
| mimo-v2.6-flash | 48/48 | 47/48 | 1.53M → 0.54M | 47% → 56% | $0.121 → $0.040 (−67%) |

The one MiMo miss (`refunds`) listed invented order IDs and amounts. It did
not reproduce in 12 traced reruns, all correct with one retrieve each. Since
a forced final round is where a model is most tempted to guess, the proxy's
note on that round now asks it to say what it couldn't check.

The single-turn suite, re-run after these changes (10 trials per mode):
DeepSeek 69/70 through Proteus against 58/70 direct, and MiMo 65/70 against
63/70. MiMo was behind direct before, at 122/140 against 132/140. Its
`log_errors` misses went away once the log compressed to 1,252 chars with
every error line in view. One trade-off from the caching fix: requests with
nothing to compress still carry the retrieve tool's definition, about 160
prompt tokens (`diff_many_files`: 2,329 → 2,492).

## Still open

- **MiMo double-checks compressed logs and search results** in single-turn
  runs. It re-runs `kubectl logs | grep` in about a third of `log_errors`
  trials even when the marker says every error line was kept. In the agent
  loop, where the harness does run its tool calls, MiMo was 32/32 correct.
- **One synthetic repository.** The agent tasks are hand-written and the tools
  return deterministic output. A replay of recorded sessions from a real agent
  would be the stronger test.
- **Line numbers in retrieve results.** Worth testing whether dropping the
  `N:`/`N-` prefixes (or moving them to the end) avoids the one misread above.

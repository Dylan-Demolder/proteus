"""Proteus multi-scenario benchmarks — each compressor tested across varied data patterns."""

import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from proteus import compress_tool_output
from proteus.compressors.json_crusher import crush_json
from proteus.compressors.log_deduper import dedup_logs
from proteus.compressors.code import strip_code
from proteus.compressors.search import compress_search
from proteus.compressors.diff import compress_diff
from proteus.compressors.text import summarize_text
from proteus.ccr import retrieve


def bench(label, content, compressor_fn=None):
    """Run a benchmark. If compressor_fn is given, call it directly (avoids router)."""
    start = time.perf_counter()

    if compressor_fn:
        result, stats = compressor_fn(content)
    else:
        result, stats = compress_tool_output(content)

    elapsed = (time.perf_counter() - start) * 1000
    savings = stats.get("compression_pct", 0)
    hash_val = stats.get("hash", "")
    ct = stats.get("content_type", "?")
    orig = stats.get("original_chars", len(content))
    comp = len(result)

    # Verify reversibility
    restored = None
    if hash_val:
        restored = retrieve(hash_val)
    reversible = restored is not None and restored == content

    print(f"  {label:40s}  {ct:18s}  {orig:>8,} -> {comp:<8,}  {savings:>6.1f}%  {elapsed:>7.1f}ms  {'Y' if reversible else 'N'}")

    return {
        "category": ct.split("_")[0] if "_" in ct else ct,
        "label": label.strip(),
        "content_type": ct,
        "original_chars": orig,
        "compressed_chars": comp,
        "savings_pct": savings,
        "time_ms": round(elapsed, 1),
        "reversible": reversible,
    }


def heading(text):
    print(f"\n{'=' * 130}")
    print(f"  {text}")
    print(f"{'=' * 130}")


all_results = []


# =====================================================================
# 1. JSON COMPRESSOR - 8 scenarios
# =====================================================================
heading("JSON CRUSHER — varying row counts, column counts, nesting, sparsity")

# 1a. Columnar: repeated objects, few columns, many rows
j1 = json.dumps([{"ticker": "AAPL", "px": 185.5, "vol": 52e6, "chg": 1.2} for _ in range(200)])
all_results.append(bench("200 identical stock rows", j1))

# 1b. Mixed values across rows (realistic portfolio)
portfolio = []
for t, p, v, c, s in [
    ("AAPL", 185.5, 52e6, 1.2, "tech"),
    ("MSFT", 420.3, 28e6, -0.5, "tech"),
    ("GOOG", 175.2, 18e6, 0.8, "tech"),
    ("AMZN", 198.0, 35e6, 0.3, "consumer"),
    ("TSLA", 245.0, 45e6, -2.1, "auto"),
    ("JPM",  162.8, 12e6, 0.5, "finance"),
    ("V",    275.0, 8e6,  0.1, "finance"),
    ("NVDA", 880.0, 60e6, 3.4, "tech"),
]:
    portfolio.append({"ticker": t, "px": p, "vol": v, "chg": c, "sector": s})
j2 = json.dumps(portfolio * 30)
all_results.append(bench("8 tickers × 30 days = 240 rows", j2))

# 1c. Tall/skinny: many rows, 2 columns
j3 = json.dumps([{"id": i, "val": i * 1.5} for i in range(500)])
all_results.append(bench("500 rows, 2 columns (tall/skinny)", j3))

# 1d. Wide: many rows, many columns (big enough for threshold)
j4 = json.dumps([{
    "id": i, "name": f"item_{i}", "status": "active" if i % 2 == 0 else "inactive",
    "created": "2024-01-15", "tags": ["alpha", "beta", "gamma"],
    "score": round(85.5 + i * 0.5, 1), "count": i * 10,
    "ratio": round(i * 0.01, 2), "enabled": True,
    "priority": "high" if i < 5 else "medium", "owner": "team-a",
    "notes": "This is a sample note for the data row",
} for i in range(50)])
all_results.append(bench("50 rows, 12 columns (short/wide)", j4))

# 1e. Deeply nested JSON
j5_base = {
    "project": {
        "name": "Proteus",
        "version": "0.1.0",
        "team": [
            {"name": "Alice", "role": "lead", "tasks": [f"task_{i}" for i in range(20)]},
            {"name": "Bob", "role": "eng", "tasks": [f"task_{i}" for i in range(15)]},
            {"name": "Carol", "role": "qa", "tasks": [f"task_{i}" for i in range(10)]},
        ],
        "milestones": {
            "alpha": {"date": "2024-03", "done": True, "bugs": 12},
            "beta": {"date": "2024-06", "done": False, "bugs": 5},
            "ga": {"date": "2024-09", "done": False, "bugs": 0},
        },
        "config": {"lint": True, "strict": True, "coverage": 90, "timeout": 300},
    }
}
b5 = json.dumps(j5_base) + "\n" + json.dumps({"version2": {f"k_{i}": f"v_{i}" for i in range(200)}})
j5 = b5 * 3
all_results.append(bench("Deeply nested (5× project JSON)", j5))

# 1f. Sparse JSON (many null/empty fields)
j6 = json.dumps([{"a": 1, "b": None, "c": None, "d": "", "e": None, "f": 0, "g": None} for _ in range(100)])
all_results.append(bench("100 rows with sparse null fields", j6))

# 1g. Single huge object (compact mode)
j7 = json.dumps({"id": 1, "log": "X" * 10000, "meta": {"source": "test", "level": "info"}})
all_results.append(bench("Single large object (10K char field)", j7))

# 1h. Repeated values across fields (high entropy reduction)
vals = ["apple", "banana", "cherry", "date", "elderberry"]
j8 = json.dumps([{"fruit": v, "color": v[:3], "count": len(v)} for v in vals] * 50)
all_results.append(bench("50× repeated enum values across fields", j8))


# =====================================================================
# 2. LOG COMPRESSOR - 8 scenarios
# =====================================================================
heading("LOG DEDUPER — varying repetition, tracebacks, mixed unique/duplicate")

# 2a. 100% identical lines
l1 = "\n".join([
    "2024-06-15 10:00:00,123 [ERROR] Connection pool exhausted - retry 1/3"
] * 1000)
all_results.append(bench("1000× identical error line", l1))

# 2b. Timestamps only differ (same message)
l2 = "\n".join([
    f"2024-06-15 10:00:{i:02d},123 [ERROR] Disk space low on /dev/sda1 - 5% remaining"
    for i in range(200)
])
all_results.append(bench("200 lines, different timestamps", l2))

# 2c. Alternating lines (2 patterns)
l3 = "\n".join(
    ["INFO: Health check passed - service=api status=200"] * 300
    + ["ERROR: Health check failed - service=db status=503"] * 300
)
all_results.append(bench("600 lines, 2 alternating patterns", l3))

# 2d. Unique lines (no dedup possible)
l4 = "\n".join([f"INFO: Request #{i} from 10.0.0.{i % 256} took {i % 100}ms" for i in range(500)])
all_results.append(bench("500 unique request log lines", l4))

# 2e. Stack traces with unique paths each time
l5 = "\n".join([
    f"ERROR: Crash in module_{i}\nTraceback:\n"
    f"  File \"/src/module_{i}.py\", line {i}, in func_{i}\n"
    f"    result = process(data)\n"
    f"RuntimeError: failure #{i}"
    for i in range(50)
])
all_results.append(bench("50 unique stack traces", l5))

# 2f. Mixed: 1 rare error + 999 common warnings
l6 = "\n".join(
    ["WARNING: Retry attempt 1/3 for request - service=api"] * 999
    + ["ERROR: Unhandled exception - service=api - status=500"]
)
all_results.append(bench("999 repeated + 1 unique error line", l6))

# 2g. JSON-format logs (structured logging)
l7 = "\n".join([
    json.dumps({"ts": f"2024-06-15T10:00:0{i}Z", "level": "ERROR",
                "logger": "app.db", "msg": "Query timeout",
                "query_ms": 30000, "rows": 0})
    for i in range(100)
])
all_results.append(bench("100 JSON-formatted log lines", l7))

# 2h. Multi-line log entries (each spans 3-5 lines)
l8 = "\n".join([
    f"2024-06-15 10:00:0{i} [ERROR] Batch job failed - job_id={i}\n"
    f"  Job type: data_sync\n"
    f"  Started: 2024-06-15 09:00:00\n"
    f"  Records processed: {i * 1000}\n"
    f"  Failure reason: timeout"
    for i in range(30)
])
all_results.append(bench("30× multi-line log entries", l8))


# =====================================================================
# 3. CODE COMPRESSOR - 8 scenarios
# =====================================================================
heading("CODE COMPRESSOR — Python, JS, TS, Go, Rust, mixed")

def py_func(name, has_doc=True, has_comment=True):
    lines = [f"def {name}():"]
    if has_doc:
        lines.append(f'    """Do something with {name} and return a result."""')
    if has_comment:
        lines.append(f"    # Initialize the {name} processor")
    lines.append(f"    result = []")
    lines.append(f"    for i in range(10):")
    lines.append(f"        result.append(i * 2)")
    lines.append(f"    return result")
    return "\n".join(lines)

def js_func(name):
    return f"function {name}() {{\n    // Process {name}\n    let data = [];\n    for (let i = 0; i < 10; i++) {{\n        data.push(i * 2);\n    }}\n    return data;\n}}"

def go_func(name):
    return f"func {name}() []int {{\n    // Process {name}\n    var data []int\n    for i := 0; i < 10; i++ {{\n        data = append(data, i*2)\n    }}\n    return data\n}}"

def rs_func(name):
    return f"fn {name}() -> Vec<i32> {{\n    // Process {name}\n    let mut data = Vec::new();\n    for i in 0..10 {{\n        data.push(i * 2);\n    }}\n    data\n}}"

c1 = "\n\n".join([py_func(f"process_{i}", True, True) for i in range(20)])
all_results.append(bench("20 Python funcs w/ docstrings + comments", c1))

c2 = "\n\n".join([py_func(f"process_{i}", False, False) for i in range(20)])
all_results.append(bench("20 Python funcs, no docstrings/comments", c2))

c3 = "\n\n".join([js_func(f"process{i}") for i in range(20)])
all_results.append(bench("20 JavaScript functions", c3))

c4 = "\n\n".join([
    "# TypeScript interface\ninterface Config {\n"
    "  readonly host: string;\n"
    "  port: number;\n"
    "  timeout?: number;\n"
    "}\n"
] * 20)
all_results.append(bench("20× TypeScript interfaces", c4))

c5 = "\n\n".join([go_func(f"Process{i}") for i in range(20)])
all_results.append(bench("20 Go functions", c5))

c6 = "\n\n".join([rs_func(f"process_{i}") for i in range(20)])
all_results.append(bench("20 Rust functions", c6))

# Code with only comments and whitespace
c7 = "\n".join(["# TODO: implement this later"] * 100 + ["# FIXME: urgent bug here"] * 100)
all_results.append(bench("200 comment-only lines (no code)", c7))

# Mixed language file
c8 = "\n".join([
    "#!/usr/bin/env python3",
    "# Proteus CLI entry point",
    py_func("main"),
    js_func("helper"),
    go_func("validate"),
    rs_func("transform"),
] * 15)
all_results.append(bench("15× multi-language mixed stubs", c8))


# =====================================================================
# 4. SEARCH COMPRESSOR - 7 scenarios
# =====================================================================
heading("SEARCH COMPRESSOR — varying hit counts, line lengths, error mixes")

# 4a. Many short hits
s1 = "\n".join([f"src/file_{i}.py:{i}:    var = process(x)" for i in range(1, 301)])
all_results.append(bench("300 short grep hits", s1))

# 4b. Few long hits
s2 = "\n".join([f"src/module_{i // 20}/handler_{i % 20}.py:{i * 10}:    "
                f"result = await self._process_with_retry(data, timeout={i * 5}, "
                f"max_attempts=3, backoff_strategy='exponential')"
                for i in range(1, 31)])
all_results.append(bench("30 long-line grep hits", s2))

# 4c. Mixed errors and matches
s3 = "\n".join(
    [f"src/main.py:{i}:    info = process(data, debug=False)" for i in range(1, 50)]
    + [f"src/main.py:{i}:    name 'process' is not defined" for i in range(101, 120)]
    + [f"src/main.py:{i}:    result = process(data, debug={i % 2 == 0})" for i in range(201, 300)]
)
all_results.append(bench("49 ok + 19 errors + 99 ok mixed grep", s3))

# 4d. Binary paths in search
s4 = "\n".join([f"dist/proteus-0.1.{i}-py3-none-any.whl: line {i}:   (binary matches)" for i in range(100)])
all_results.append(bench("100 binary match results", s4))

# 4e. Search with file paths only (no content)
s5 = "\n".join([f"src/lib/module_{i}/__init__.py" for i in range(200)])
all_results.append(bench("200 path-only results (like `find`)", s5))

# 4f. Ripgrep-style context lines
s6 = "\n".join(
    [f"src/app.py-{i}-    processed_count += 1" for i in range(1, 60)]
    + [f"src/app.py-{i}:    result = process(data, debug=True)" for i in range(1, 40)]
    + [f"src/app.py-{i}-    return result" for i in range(1, 60)]
)
all_results.append(bench("Context lines (rg --context=2)", s6))

# 4g. Single massive hit line
s7 = "src/giant_file.py:1:    " + "data_field_" * 500
all_results.append(bench("Single gigantic hit line (40K chars)", s7))


# =====================================================================
# 5. DIFF COMPRESSOR - 7 scenarios
# =====================================================================
heading("DIFF COMPRESSOR — single file, multi-file, additions, deletions, renames")

# Helper
def make_diff(files_content):
    return "\n".join(files_content)

d1 = "\n".join([
    f"diff --git a/src/main.py b/src/main.py\n"
    f"index abc{i}..def{i} 100644\n"
    f"--- a/src/main.py\n"
    f"+++ b/src/main.py\n"
    f"@@ -5,7 +5,9 @@\n"
    f" \n"
    f"+    parser.add_argument('--verbose', action='store_true')\n"
    f"+    parser.add_argument('--output', type=str)\n"
    f"     result = process(args)\n"
    f"-\n"
    f"+    logger.info(f'Processed {{result}}')\n"
    f"     return result"
    for i in range(30)
])
all_results.append(bench("30 identical hunks to same file", d1))

# Multi-file diff
d2 = "\n".join([
    f"diff --git a/src/module_{i}.py b/src/module_{i}.py\n"
    f"index a{i:04x}..b{i:04x} 100644\n"
    f"--- a/src/module_{i}.py\n"
    f"+++ b/src/module_{i}.py\n"
    f"@@ -1,3 +1,4 @@\n"
    f" def {chr(97 + i % 26)}():\n"
    f"-    pass\n"
    f"+    return True\n"
    f"+    logger.debug('processed')\n"
    for i in range(30)
])
all_results.append(bench("30 files, 1 hunk each", d2))

# Large additions (mostly + lines)
d3 = "\n".join([
    f"diff --git a/src/new.py b/src/new.py\n"
    f"new file mode 100644\n"
    f"index 0000000..abc{i}def\n"
    f"--- /dev/null\n"
    f"+++ b/src/new.py\n"
    f"@@ -0,0 +1,30 @@\n"
    f"+import sys\n"
    f"+import os\n"
    f"+def init():\n"
    f"+    # Initialize the system\n"
    f"+    config = load_config()\n"
    f"+    if config.debug:\n"
    f"+        logger.setLevel(logging.DEBUG)\n"
    f"+    return config\n"
    for i in range(10)
])
all_results.append(bench("10 new files (large additions)", d3))

# Large deletions (mostly - lines)
d4 = "\n".join([
    f"diff --git a/src/old_{i}.py b/src/old_{i}.py\n"
    f"deleted file mode 100644\n"
    f"index abc{i}..0000000\n"
    f"--- a/src/old_{i}.py\n"
    f"+++ /dev/null\n"
    f"@@ -1,30 +0,0 @@\n"
    f"-def legacy_{i}():\n"
    f"-    # Deprecated function\n"
    f"-    old_way = process(data)\n"
    f"-    return old_way\n"
    for i in range(10)
])
all_results.append(bench("10 deleted files (large deletions)", d4))

# Rename only
d5 = "\n".join([
    f"diff --git a/src/old_name_{i}.py b/src/new_name_{i}.py\n"
    f"similarity index 100%\n"
    f"rename from src/old_name_{i}.py\n"
    f"rename to src/new_name_{i}.py\n"
    for i in range(30)
])
all_results.append(bench("30 file renames (no content changes)", d5))

# Mixed add/delete with lots of context
d6 = "\n".join([
    f"diff --git a/src/big.py b/src/big.py\n"
    f"index abc{i}..def{i} 100644\n"
    f"--- a/src/big.py\n"
    f"+++ b/src/big.py\n"
    f"@@ -10,15 +10,18 @@\n"
    f" def worker():\n"
    f"     setup_logging()\n"
    f"     load_config()\n"
    f"+    validate_config()\n"
    f"     connect_db()\n"
    f"     fetch_data()\n"
    f"+    transform_data()\n"
    f"+    validate_schema()\n"
    f"     process_results()\n"
    f"-\n"
    f"+    save_output()\n"
    f"     return True"
    for i in range(20)
])
all_results.append(bench("20 hunks with add/del + context", d6))

# Binary file diff
d7 = "\n".join([
    f"diff --git a/assets/image_{i}.png b/assets/image_{i}.png\n"
    f"index abc{i}..def{i} 100644\n"
    f"Binary files a/assets/image_{i}.png and b/assets/image_{i}.png differ"
    for i in range(50)
])
all_results.append(bench("50 binary file diffs (no text content)", d7))


# =====================================================================
# 6. TEXT COMPRESSOR - 7 scenarios
# =====================================================================
heading("TEXT SUMMARIZER — paragraphs, lists, tables, markdown, structured")

# 6a. Long prose paragraphs
t1 = ("The Proteus compression system is designed specifically for LLM tool "
      "outputs. It exploits structural patterns like repetitive JSON, redundant "
      "log lines, boilerplate code, and deterministic file listings. Each "
      "compressor is optimized for its content type using a content router. "
      "Compression is lossless at the semantic level.\n\n") * 30
all_results.append(bench("30× prose paragraph blocks", t1))

# 6b. Markdown document
t2 = ("# Proteus Documentation\n\n"
      "## Installation\n\n"
      "```bash\npip install proteus\n```\n\n"
      "## Usage\n\n"
      "### Basic\n\n"
      "```python\nfrom proteus import compress_tool_output\n"
      "compressed, stats = compress_tool_output(large_content)\n```\n\n"
      "### Advanced\n\n"
      "See the [docs](https://proteus.dev) for API reference.\n\n") * 20
all_results.append(bench("20× Markdown document blocks", t2))

# 6c. ASCII tables
t3 = ("+---------+-------+-------+-------+-------+\n"
      "| Name    | Test1 | Test2 | Test3 | Avg   |\n"
      "+---------+-------+-------+-------+-------+\n"
      "| Alice   |    85 |    92 |    78 | 85.0  |\n"
      "| Bob     |    70 |    65 |    72 | 69.0  |\n"
      "| Carol   |    95 |    88 |    91 | 91.3  |\n"
      "+---------+-------+-------+-------+-------+\n") * 20
all_results.append(bench("20× ASCII table blocks", t3))

# 6d. YAML config
t4 = ("server:\n"
      "  host: 0.0.0.0\n"
      "  port: 8080\n"
      "  workers: 4\n"
      "  timeout: 30\n"
      "  cors:\n"
      "    enabled: true\n"
      "    origins:\n"
      "      - https://app.example.com\n"
      "      - https://api.example.com\n"
      "database:\n"
      "  host: db-primary-01\n"
      "  port: 5432\n"
      "  pool_size: 10\n"
      "  ssl: true\n"
      "logging:\n"
      "  level: INFO\n"
      "  format: json\n") * 15
all_results.append(bench("15× YAML config blocks", t4))

# 6e. Bulleted/numbered lists
t5 = ("Steps to deploy:\n"
      "1. Check out the latest release\n"
      "2. Run database migrations\n"
      "3. Build the application\n"
      "4. Run integration tests\n"
      "5. Deploy to staging\n"
      "6. Health check\n"
      "7. Promote to production\n"
      "8. Monitor for 15 minutes\n\n"
      "Prerequisites:\n"
      "- Python 3.11+\n"
      "- PostgreSQL 15+\n"
      "- Redis 7+\n"
      "- Docker 24+\n") * 20
all_results.append(bench("20× structured lists", t5))

# 6f. CSV/TSV data
t6 = "\n".join([
    "symbol,date,open,high,low,close,volume,macd,rsi,sma_50,sma_200,bb_upper,bb_lower,pe_ratio,mkt_cap"
] + [
    f"AAPL,2024-06-{i:02d},{185 + i * 0.1:.1f},{186 + i * 0.1:.1f},"
    f"{184 + i * 0.1:.1f},{185.5 + i * 0.1:.1f},{52000000 + i * 1000},"
    f"{0.5 + i * 0.01:.2f},{55 + i * 0.2:.1f},{180 + i * 0.05:.1f},"
    f"{175 + i * 0.02:.1f},{190 + i * 0.1:.1f},{170 + i * 0.1:.1f},"
    f"{28.5 + i * 0.01:.1f},{2800000000000 + i * 10000000000}"
    for i in range(1, 60)
])
all_results.append(bench("59-line CSV (stock data, 15 columns)", t6))

# 6g. Error/help message blocks
t7 = ("Usage: proteus [OPTIONS] COMMAND [ARGS]...\n\n"
      "Options:\n"
      "  --help          Show this message and exit.\n"
      "  --version       Show the version and exit.\n"
      "  --verbose       Enable verbose output.\n\n"
      "Commands:\n"
      "  proxy           Start the compression proxy.\n"
      "  file            Compress a single file.\n"
      "  stats           Show cache statistics.\n"
      "  clear           Clear all cached content.\n"
      "  retrieve        Retrieve original from cache.\n\n"
      "Error: No such command 'xxxxx'.\n") * 20
all_results.append(bench("20× CLI help/error messages", t7))


# =====================================================================
# 7. COMPRESS_TOOL_OUTPUT — E2E router dispatch tests
# =====================================================================
heading("END-TO-END ROUTER — bypassing direct compressor calls")

# These go through compress_tool_output which routes via ContentType detection
e1 = json.dumps([{"a": i, "b": i * 2} for i in range(300)])
r1, s1 = compress_tool_output(e1)
all_results.append(bench("E2E: 300-row JSON via router", e1))

e2 = "\n".join(["2024-06-15 ERROR: OOM killer invoked - pid=" + str(i) for i in range(200)])
r2, s2 = compress_tool_output(e2)
all_results.append(bench("E2E: 200 error lines via router", e2))

e3 = ("\n".join([
    "-- a/src/file.py\n+++ b/src/file.py\n@@ -1,5 +1,8 @@\n"
    " def old():\n-    pass\n+    return True\n+    log.debug('done')\n"
    for _ in range(15)
]))
r3, s3 = compress_tool_output(e3)
all_results.append(bench("E2E: 15 diff hunks via router", e3))


# =====================================================================
# SUMMARY
# =====================================================================
print()
print("=" * 130)
print(f"  BENCHMARK SUMMARY — {len(all_results)} scenarios across 7 compressors")
print("=" * 130)
print()

# Group by category
from collections import defaultdict
by_cat = defaultdict(list)
for r in all_results:
    by_cat[r["category"]].append(r)

total_orig = 0
total_comp = 0
total_time = 0

for cat in sorted(by_cat.keys()):
    items = by_cat[cat]
    cat_orig = sum(i["original_chars"] for i in items)
    cat_comp = sum(i["original_chars"] - i["original_chars"] * i["savings_pct"] / 100 for i in items)
    cat_savings = (cat_orig - cat_comp) / cat_orig * 100 if cat_orig > 0 else 0
    cat_time = sum(i["time_ms"] for i in items) / len(items)
    reversible = all(i["reversible"] for i in items)
    best = max(items, key=lambda x: x["savings_pct"])
    worst = min(items, key=lambda x: x["savings_pct"])

    print(f"  {cat:15s}  {len(items):2d} tests  "
          f"  {cat_orig:>8,} -> {int(cat_comp):>8,}  "
          f"  {cat_savings:>5.1f}% avg  "
          f"  {cat_time:>5.1f}ms avg  "
          f"  best: {best['savings_pct']:>5.1f}%  worst: {worst['savings_pct']:>5.1f}%  "
          f"  rev: {'all' if reversible else 'partial'}")

    total_orig += cat_orig
    total_comp += cat_comp
    total_time += cat_time

overall_savings = (total_orig - total_comp) / total_orig * 100 if total_orig > 0 else 0
overall_time = total_time / len(by_cat)
token_ratio = 4
tokens_saved = int((total_orig - total_comp) // token_ratio)

print()
print(f"{'─' * 130}")
print(f"  TOTAL        {len(all_results)} tests  "
      f"  {total_orig:>8,} -> {int(total_comp):>8,}  "
      f"  {overall_savings:>5.1f}% avg  "
      f"  {overall_time:>5.1f}ms avg  "
      f"  rev: {all(r['reversible'] for r in all_results)}")
print(f"  TOKENS: {tokens_saved:,} saved from {total_orig // token_ratio:,} input  "
      f"(@ {token_ratio}:1 char:token ratio)")
print(f"  COST:  ${tokens_saved * 2 / 1_000_000:.4f} saved  "
      f"(@ $2/M input tokens)")
print()
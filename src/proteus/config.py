"""Tunable thresholds for the Hermes Compression Engine."""

# ── Routing ──
MIN_COMPRESS_CHARS = 3000  # Skip content smaller than this (no point compressing short strings)

# ── JSON Crusher ──
JSON_MAX_ROWS_BEFORE_DROP = 200  # Start row-dropping when array exceeds this
JSON_DROP_HEAD = 10              # Rows to keep from start
JSON_DROP_TAIL = 10              # Rows to keep from end
JSON_COLUMNAR_MIN_ROWS = 5       # Use columnar format for arrays >= this size
JSON_AUTO_COLUMNAR = True        # Auto-detect repeated-key arrays vs heterogeneous

# ── Log Deduper ──
LOG_MIN_REPETITIONS = 3           # Dedup lines seen at least this many times
LOG_KEEP_FIRST_LAST = True        # Always show the first and last occurrence
LOG_MAX_ERRORS = 30               # Max individual error lines before grouping
LOG_MAX_LINES_TOTAL = 200         # Total max output lines after compression

# ── Code Compressor ──
CODE_MAX_FUNCTION_LINES = 15      # Compress function bodies longer than this
CODE_STRIP_COMMENTS = True        # Remove comments and docstrings
CODE_STRIP_BLANK_LINES = True     # Collapse consecutive blank lines
CODE_MAX_FILE_LINES = 200         # Max lines of code to show in full

# ── File Lister ──
LS_STRIP_PERMS = True             # Remove -rw-r--r-- columns
LS_STRIP_OWNER = True             # Remove root root columns
LS_STRIP_MONTH = False            # Keep date (month/day is useful for comparison)

# ── Text ──
TEXT_MAX_CHARS = 10000            # Summarize text longer than this
TEXT_HEAD_CHARS = 2000            # Chars to keep from start
TEXT_TAIL_CHARS = 2000            # Chars to keep from end

# ── CCR Cache ──
CCR_CACHE_DIR = "~/.hermes/cache/compress/"
CCR_MAX_ENTRIES = 500
CCR_HASH_LENGTH = 12              # Short hash for readability in markers
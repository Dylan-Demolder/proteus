"""Tunable thresholds for the Proteus compression engine.

The module-level constants below are the live settings: compressors read
them at call time, so changing them changes behaviour immediately. Change
them with ``update()``, ``configure()`` (a YAML file and/or a profile), or
``proteus.profiles.use_profile()``. ``reset()`` restores the defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ── Routing ──
MIN_COMPRESS_CHARS = 3000  # Skip content smaller than this (no point compressing short strings)
MIN_SAVINGS_PCT = 25       # Proxy: send the original if compression saves less than this

# ── JSON Crusher ──
JSON_MAX_ROWS_BEFORE_DROP = 200  # Start row-dropping when array exceeds this
JSON_DROP_HEAD = 10              # Rows to keep from start
JSON_DROP_TAIL = 10              # Rows to keep from end
JSON_COLUMNAR_MIN_ROWS = 5       # Use columnar format for arrays >= this size
JSON_AUTO_COLUMNAR = True        # Auto-detect repeated-key arrays vs heterogeneous

# ── Log Deduper ──
LOG_MIN_REPETITIONS = 3           # Dedup lines seen at least this many times
LOG_KEEP_FIRST_LAST = True        # Always show the first and last occurrence
LOG_MAX_ERRORS = 30               # Reserved — not used by the log deduper yet
LOG_MAX_LINES_TOTAL = 200         # Total max output lines after compression

# ── Code Compressor ──
# Reserved — the code compressor always strips comments/docstrings and
# collapses blank lines; these knobs are not read by anything yet.
CODE_MAX_FUNCTION_LINES = 15
CODE_STRIP_COMMENTS = True
CODE_STRIP_BLANK_LINES = True
CODE_MAX_FILE_LINES = 200

# ── File Lister ──
LS_STRIP_PERMS = True             # Remove -rw-r--r-- columns
LS_STRIP_OWNER = True             # Remove root root columns
LS_STRIP_MONTH = False            # Reserved — dates are always kept

# ── Text ──
TEXT_MAX_CHARS = 10000            # Summarize text longer than this
TEXT_HEAD_CHARS = 2000            # Chars to keep from start
TEXT_TAIL_CHARS = 2000            # Chars to keep from end

# ── Search Results ──
SEARCH_MAX_PER_FILE = 5           # Matches shown per file
SEARCH_MAX_TOTAL = 30             # Matches shown across all files
SEARCH_MAX_FILES = 15             # Files shown

# ── Diff ──
DIFF_MAX_CONTEXT_LINES = 2        # Context lines kept on each side of a change
DIFF_MAX_HUNKS_PER_FILE = 10      # Hunks shown per file
DIFF_MAX_FILES = 20               # Files shown

# ── CCR Cache ──
CCR_CACHE_DIR = "~/.proteus/cache/"
CCR_MAX_ENTRIES = 500
CCR_HASH_LENGTH = 12              # Short hash for readability in markers


# ── Loading settings ──

# Snapshot of every setting above, taken before anything can change them.
DEFAULTS: dict[str, Any] = {k: v for k, v in dict(globals()).items() if k.isupper() and k != "DEFAULTS"}

# YAML section name -> setting prefix. Top-level scalar keys map directly
# (min_compress_chars -> MIN_COMPRESS_CHARS).
_SECTIONS = {
    "json": "JSON_",
    "logs": "LOG_",
    "code": "CODE_",
    "file_listing": "LS_",
    "text": "TEXT_",
    "search": "SEARCH_",
    "diff": "DIFF_",
    "ccr": "CCR_",
}


def _check(key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"Unknown setting {key!r}")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ValueError(
            f"Setting {key!r} must be {type(default).__name__}, got {type(value).__name__} ({value!r})"
        )


def update(overrides: dict[str, Any]) -> None:
    """Change settings. Keys are setting names (e.g. "JSON_DROP_HEAD").

    Validates everything before changing anything, so a bad key or value
    raises ValueError and leaves the current settings untouched.
    """
    for key, value in overrides.items():
        _check(key, value)
    globals().update(overrides)


def reset() -> None:
    """Restore every setting to its default."""
    globals().update(DEFAULTS)


def current() -> dict[str, Any]:
    """The live value of every setting."""
    return {k: globals()[k] for k in DEFAULTS}


def read_file(path: str | Path) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """Parse a YAML config file (see config.yaml at the repo root).

    Returns:
        (settings, profile, proxy): setting overrides keyed by setting name,
        the ``profile:`` value if present, and the ``proxy:`` section.

    Raises:
        ValueError: unknown keys or wrongly typed values, naming the key.
            A config file that is silently ignored is worse than one that fails.
    """
    import yaml

    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    settings: dict[str, Any] = {}
    profile: str | None = None
    proxy: dict[str, Any] = {}
    for key, value in data.items():
        if key == "profile":
            if not isinstance(value, str):
                raise ValueError(f"{path}: 'profile' must be a string")
            profile = value
        elif key == "proxy":
            if not isinstance(value, dict):
                raise ValueError(f"{path}: 'proxy' must be a mapping")
            unknown = set(value) - {"host", "port", "backend", "upstream_url", "api_key_env", "log_file"}
            if unknown:
                raise ValueError(f"{path}: unknown proxy setting(s): {', '.join(sorted(unknown))}")
            proxy = dict(value)
        elif key in _SECTIONS:
            if not isinstance(value, dict):
                raise ValueError(f"{path}: section {key!r} must be a mapping")
            for sub, sub_value in value.items():
                settings[_SECTIONS[key] + str(sub).upper()] = sub_value
        elif isinstance(key, str):
            settings[key.upper()] = value
        else:
            raise ValueError(f"{path}: unexpected key {key!r}")

    for key, value in settings.items():
        try:
            _check(key, value)
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from None
    return settings, profile, proxy


def configure(path: str | Path | None = None, profile: str | None = None) -> dict[str, Any]:
    """Reset to defaults, then apply a profile and/or a YAML file.

    Precedence, lowest to highest: defaults, the profile (the ``profile``
    argument, else the file's ``profile:`` key), the file's own settings.

    Returns:
        The file's ``proxy:`` section (empty without a file).
    """
    from .profiles import get_profile

    settings: dict[str, Any] = {}
    proxy: dict[str, Any] = {}
    if path is not None:
        settings, file_profile, proxy = read_file(path)
        profile = profile or file_profile

    merged = get_profile(profile) if profile else {}
    merged.update(settings)
    for key, value in merged.items():
        _check(key, value)
    reset()
    update(merged)
    return proxy

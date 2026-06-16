"""Per-session compression profiles for Proteus.

Defines three compression modes (conservative, balanced, aggressive)
that override default config thresholds for different use cases.
"""

from __future__ import annotations

from typing import Any

# ── Profile definitions ──────────────────────────────────────────────────

PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {
        # Only compress JSON arrays and repetitive logs
        # No row-dropping, no text summarization
        "MIN_COMPRESS_CHARS": 5000,
        "JSON_MAX_ROWS_BEFORE_DROP": 999_999,
        "JSON_DROP_HEAD": 50,
        "JSON_DROP_TAIL": 50,
        "JSON_COLUMNAR_MIN_ROWS": 999_999,
        "JSON_AUTO_COLUMNAR": True,
        "LOG_MIN_REPETITIONS": 5,
        "LOG_KEEP_FIRST_LAST": True,
        "LOG_MAX_ERRORS": 50,
        "LOG_MAX_LINES_TOTAL": 500,
        "CODE_MAX_FUNCTION_LINES": 999_999,
        "CODE_STRIP_COMMENTS": True,
        "CODE_STRIP_BLANK_LINES": False,
        "CODE_MAX_FILE_LINES": 999_999,
        "LS_STRIP_PERMS": True,
        "LS_STRIP_OWNER": True,
        "LS_STRIP_MONTH": False,
        "TEXT_MAX_CHARS": 999_999,
        "TEXT_HEAD_CHARS": 5000,
        "TEXT_TAIL_CHARS": 5000,
    },
    "balanced": {
        # All compressors active, moderate thresholds
        # This is the default — matches config.py defaults
        "MIN_COMPRESS_CHARS": 3000,
        "JSON_MAX_ROWS_BEFORE_DROP": 200,
        "JSON_DROP_HEAD": 10,
        "JSON_DROP_TAIL": 10,
        "JSON_COLUMNAR_MIN_ROWS": 5,
        "JSON_AUTO_COLUMNAR": True,
        "LOG_MIN_REPETITIONS": 3,
        "LOG_KEEP_FIRST_LAST": True,
        "LOG_MAX_ERRORS": 30,
        "LOG_MAX_LINES_TOTAL": 200,
        "CODE_MAX_FUNCTION_LINES": 15,
        "CODE_STRIP_COMMENTS": True,
        "CODE_STRIP_BLANK_LINES": True,
        "CODE_MAX_FILE_LINES": 200,
        "LS_STRIP_PERMS": True,
        "LS_STRIP_OWNER": True,
        "LS_STRIP_MONTH": False,
        "TEXT_MAX_CHARS": 10000,
        "TEXT_HEAD_CHARS": 2000,
        "TEXT_TAIL_CHARS": 2000,
    },
    "aggressive": {
        # All compressors active, lower thresholds
        "MIN_COMPRESS_CHARS": 2000,
        "JSON_MAX_ROWS_BEFORE_DROP": 50,
        "JSON_DROP_HEAD": 5,
        "JSON_DROP_TAIL": 5,
        "JSON_COLUMNAR_MIN_ROWS": 3,
        "JSON_AUTO_COLUMNAR": True,
        "LOG_MIN_REPETITIONS": 2,
        "LOG_KEEP_FIRST_LAST": True,
        "LOG_MAX_ERRORS": 15,
        "LOG_MAX_LINES_TOTAL": 100,
        "CODE_MAX_FUNCTION_LINES": 8,
        "CODE_STRIP_COMMENTS": True,
        "CODE_STRIP_BLANK_LINES": True,
        "CODE_MAX_FILE_LINES": 100,
        "LS_STRIP_PERMS": True,
        "LS_STRIP_OWNER": True,
        "LS_STRIP_MONTH": False,
        "TEXT_MAX_CHARS": 5000,
        "TEXT_HEAD_CHARS": 1000,
        "TEXT_TAIL_CHARS": 1000,
    },
}


def get_profile(name: str) -> dict[str, Any]:
    """Get the config overrides for a named profile.

    Args:
        name: Profile name ("conservative", "balanced", "aggressive").

    Returns:
        Dict of config key → value overrides.

    Raises:
        ValueError: If the profile name is unknown.
    """
    name = name.lower()
    if name not in PROFILES:
        raise ValueError(
            f"Unknown profile '{name}'. "
            f"Available profiles: {', '.join(list_profiles())}"
        )
    return dict(PROFILES[name])


def apply_profile(config_dict: dict[str, Any], profile: str) -> dict[str, Any]:
    """Merge profile overrides into a config dictionary.

    Args:
        config_dict: Existing config dict (e.g. from proteus.config module).
        profile: Profile name to apply.

    Returns:
        New config dict with profile values merged on top.
    """
    overrides = get_profile(profile)
    merged = dict(config_dict)
    merged.update(overrides)
    return merged


def list_profiles() -> list[str]:
    """Return list of available profile names."""
    return sorted(PROFILES.keys())


def detect_profile_from_config(config_dict: dict[str, Any]) -> str:
    """Detect which profile best matches a given config dict.

    Compares the config values against each profile definition and
    returns the name of the closest match (most keys in common).

    Args:
        config_dict: A config dict (e.g. from proteus.config module).

    Returns:
        The closest profile name, or "balanced" if no clear match.
    """
    best_match = "balanced"
    best_score = 0

    for name, profile in PROFILES.items():
        score = 0
        for key, expected_value in profile.items():
            actual = config_dict.get(key)
            if actual == expected_value:
                score += 1
        if score > best_score:
            best_score = score
            best_match = name

    return best_match
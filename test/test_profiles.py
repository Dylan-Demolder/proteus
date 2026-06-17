#!/usr/bin/env python3
"""Tests for proteus.profiles — per-session compression profiles."""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from proteus.profiles import (
    get_profile, apply_profile, list_profiles,
    detect_profile_from_config, PROFILES,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ── Tests ─────────────────────────────────────────────────────────────────

section("1. Profile Listing")

profiles = list_profiles()
check("list_profiles returns list", isinstance(profiles, list))
check("three profiles available", len(profiles) == 3)
for name in ["conservative", "balanced", "aggressive"]:
    check(f"profile '{name}' exists", name in profiles)


section("2. Get Profile")

for name in ["conservative", "balanced", "aggressive"]:
    profile = get_profile(name)
    check(f"{name}: returns dict", isinstance(profile, dict))
    check(f"{name}: has MIN_COMPRESS_CHARS", "MIN_COMPRESS_CHARS" in profile)
    check(f"{name}: has TEXT_MAX_CHARS", "TEXT_MAX_CHARS" in profile)

# Unknown profile
try:
    get_profile("nonexistent")
    check("unknown profile raises error", False)
except ValueError as e:
    check("unknown profile raises ValueError", "Unknown profile" in str(e))


section("3. Profile Threshold Levels")

conservative = get_profile("conservative")
balanced = get_profile("balanced")
aggressive = get_profile("aggressive")

# Assert ordering: conservative < balanced < aggressive (higher MIN_COMPRESS = less aggressive)
check("conservative has highest MIN_COMPRESS_CHARS",
      conservative["MIN_COMPRESS_CHARS"] >= balanced["MIN_COMPRESS_CHARS"])
check("aggressive has lowest MIN_COMPRESS_CHARS",
      aggressive["MIN_COMPRESS_CHARS"] <= balanced["MIN_COMPRESS_CHARS"])

# Conservative should never summarize text
check("conservative TEXT_MAX_CHARS is very high",
      conservative["TEXT_MAX_CHARS"] >= 100000)
# Aggressive summarizes sooner
check("aggressive TEXT_MAX_CHARS is lower than balanced",
      aggressive["TEXT_MAX_CHARS"] <= balanced["TEXT_MAX_CHARS"])


section("4. Apply Profile")

base_config = {
    "MIN_COMPRESS_CHARS": 3000,
    "TEXT_MAX_CHARS": 10000,
    "JSON_MAX_ROWS_BEFORE_DROP": 200,
}

merged = apply_profile(base_config, "aggressive")
check("merged dict returned", isinstance(merged, dict))
check("aggressive MIN_COMPRESS applied",
      merged["MIN_COMPRESS_CHARS"] == aggressive["MIN_COMPRESS_CHARS"])
check("base config not mutated",
      base_config["MIN_COMPRESS_CHARS"] == 3000)
check("non-overridden keys preserved",
      merged["TEXT_MAX_CHARS"] == aggressive["TEXT_MAX_CHARS"])

# Apply conservative
merged_con = apply_profile(base_config, "conservative")
check("conservative profile applied",
      merged_con["MIN_COMPRESS_CHARS"] == conservative["MIN_COMPRESS_CHARS"])


section("5. Profile Detection")

# None of these will match exactly since we pass partial configs
detected = detect_profile_from_config({"MIN_COMPRESS_CHARS": 3000})
check("detection returns a string",
      isinstance(detected, str) and detected in PROFILES)

# With full config dict, detection should find the match
detected_exact = detect_profile_from_config(PROFILES["balanced"])
check("exact balanced match detected",
      detected_exact == "balanced")

detected_agg = detect_profile_from_config(PROFILES["aggressive"])
check("exact aggressive match detected",
      detected_agg == "aggressive")

detected_con = detect_profile_from_config(PROFILES["conservative"])
check("exact conservative match detected",
      detected_con == "conservative")


section("6. Profile Data Integrity")

# All profiles should have the same keys
expected_keys = set(PROFILES["balanced"].keys())
for name, profile in PROFILES.items():
    profile_keys = set(profile.keys())
    check(f"{name}: all expected keys present",
          expected_keys.issubset(profile_keys) or profile_keys == expected_keys,
          f"missing: {expected_keys - profile_keys}, extra: {profile_keys - expected_keys}")


section("7. All Profiles Return Dict with Correct Types")

for name in PROFILES:
    profile = get_profile(name)
    for key, value in profile.items():
        if key == "JSON_AUTO_COLUMNAR" or key == "CODE_STRIP_COMMENTS" or \
           key == "CODE_STRIP_BLANK_LINES" or key == "LS_STRIP_PERMS" or \
           key == "LS_STRIP_OWNER" or key == "LOG_KEEP_FIRST_LAST" or \
           key == "LS_STRIP_MONTH" or key == "JSON_AUTO_COLUMNAR":
            check(f"{name}.{key} is bool", isinstance(value, bool))
        else:
            check(f"{name}.{key} is int/float",
                  isinstance(value, (int, float)),
                  f"got {type(value).__name__}")


# ── Results ───────────────────────────────────────────────────────────────

total = PASS + FAIL
print(f"\n{'=' * 60}")
print(f"  PROFILE TESTS: {PASS} passed, {FAIL} failed{' 🎉' if FAIL == 0 else ''}")
print(f"{'=' * 60}")
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
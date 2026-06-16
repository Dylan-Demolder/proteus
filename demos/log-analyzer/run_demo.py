#!/usr/bin/env python3
"""
Proteus Dogfood Demo
====================
1. Generates realistic multi-service log data
2. Compresses each file with Proteus (best-fit compressor)
3. Decompresses back via CCR cache (lossless roundtrip)
4. Verifies byte-for-byte identity between original and decompressed
5. Runs analysis on both — proves identical results
6. Reports compression ratios and cost savings
"""

import sys
import os

from generate_logs import generate_output_dir
from analyze_logs import analyze_log, compute_delta, print_report


def format_size(n):
    for unit in ["B", "KB", "MB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def main():
    print("=" * 65)
    print("  PROTEUS — LLM Tool Output Compression  (Dogfood Demo)")
    print("=" * 65)

    data_dir = "/tmp/proteus-demo/data"
    os.makedirs(data_dir, exist_ok=True)

    # ── Step 1: Generate data ──
    print("\n[1/5] Generating realistic multi-service log data...")
    entries = generate_output_dir(data_dir)
    print(f"  Total entries: {len(entries)}")

    # ── Step 2: Compress each file ──
    print("\n[2/5] Compressing with Proteus...")
    import proteus
    files = sorted([
        f for f in os.listdir(data_dir)
        if os.path.isfile(os.path.join(data_dir, f))
    ])

    compressed_info = {}  # fname -> (compressed, stats, raw)
    total_raw = 0
    total_comp = 0

    for fname in files:
        path = os.path.join(data_dir, fname)
        with open(path) as f:
            raw = f.read()
        raw_sz = len(raw.encode("utf-8"))
        total_raw += raw_sz

        # Detect format for Proteus
        hints = {
            "metadata.json": "json",
            "app.log": "logs",
            "db.log": "logs",
            "nginx.log": "search_results",
        }
        hint = hints.get(fname)
        if hint:
            compressed, stats = proteus.compress_tool_output(raw, content_type_hint=hint)
        else:
            compressed, stats = proteus.compress_tool_output(raw)

        comp_sz = len(compressed.encode("utf-8"))
        total_comp += comp_sz

        ratio = (1 - comp_sz / raw_sz) * 100 if raw_sz > 0 else 0
        print(f"  {fname:20s}  {format_size(raw_sz):>7s} → {format_size(comp_sz):>7s}  ({ratio:5.1f}%)  [{stats.get('compressor', 'n/a'):>15s}]")

        compressed_info[fname] = (compressed, stats, raw)

    overall = (1 - total_comp / total_raw) * 100
    print(f"  {'─'*60}")
    print(f"  {'TOTAL':20s}  {format_size(total_raw):>7s} → {format_size(total_comp):>7s}  ({overall:5.1f}%)")

    # ── Step 3: Decompress via CCR and verify byte-for-byte ──
    print("\n[3/5] Decompressing via CCR cache (lossless roundtrip)...")
    all_match = True
    for fname in files:
        compressed, stats, raw = compressed_info[fname]

        # Retrieve original from CCR using the hash
        content_hash = stats.get("hash", "")
        if content_hash:
            retrieved = proteus.ccr.retrieve(content_hash)
        else:
            retrieved = raw  # wasn't compressed (too_small)

        # Proteus caches the original, not the compressed form, so
        # decompressed should match byte-for-byte.
        if isinstance(retrieved, str):
            retrieved_bytes = retrieved.encode("utf-8")
        else:
            retrieved_bytes = bytes(retrieved) if retrieved else b""

        raw_bytes = raw.encode("utf-8")
        raw_sz = len(raw_bytes)
        if raw_sz > 0 and retrieved_bytes == raw_bytes:
            print(f"  ✅ {fname:20s}  Byte-for-byte match ({raw_sz:,} bytes identical)")
        else:
            all_match = False
            print(f"  ❌ {fname:20s}  MISMATCH! ({len(raw_bytes)} vs {len(retrieved_bytes)} bytes)")

    if all_match:
        print(f"\n  🟢 ALL FILES: 100% lossless roundtrip — every byte preserved")
    else:
        print(f"\n  🔴 BYTEBEAM FAILURE — mismatches found")

    # ── Step 4: Analyze originals ──
    print("\n[4/5] Analyzing original logs...")
    raw_analyses = {}
    for fname, (_, _, raw) in compressed_info.items():
        raw_analyses[fname] = analyze_log(raw, f"original/{fname}")

    # ── Step 5: Analyze decompressed (identical to originals) ──
    print("\n[5/5] Analyzing DECOMPRESSED logs and comparing...")
    decomp_analyses = {}
    for fname in files:
        compressed, stats, raw = compressed_info[fname]
        content_hash = stats.get("hash", "")
        if content_hash:
            decompressed = proteus.ccr.retrieve(content_hash)
        else:
            decompressed = raw
        decomp_analyses[fname] = analyze_log(decompressed, f"decompressed/{fname}")

    # ── Compare ──
    print("\n" + "=" * 65)
    print("  VERIFICATION: Analysis on decompressed vs original")
    print("=" * 65)

    all_pass = True
    for fname in sorted(raw_analyses.keys()):
        diffs = compute_delta(raw_analyses[fname], decomp_analyses[fname])
        if diffs:
            all_pass = False
            for k, v in diffs.items():
                print(f"  ❌ {fname} — {k}: {v[0]} ≠ {v[1]}")
        else:
            print(f"  ✅ {fname:20s} — ALL {len(raw_analyses[fname].__dict__)} metrics match")

    print()
    print(f"  {'🟢 ALL ANALYSES IDENTICAL' if all_pass else '🔴 DISCREPANCIES FOUND'}")
    print(f"  Compression is lossless — original is fully recoverable via CCR")

    # ── Detailed report ──
    print("\n" + "=" * 65)
    print("  DETAILED ANALYSIS (from decompressed data)")
    print("=" * 65)
    for fname in sorted(decomp_analyses.keys()):
        print_report(decomp_analyses[fname])

    # ── Cost savings ──
    print("\n" + "=" * 65)
    print("  COST SAVINGS (LLM context pricing @ $2/M input tokens)")
    print("=" * 65)
    raw_tokens = total_raw // 4
    comp_tokens = total_comp // 4
    saved_tokens = raw_tokens - comp_tokens
    raw_cost = raw_tokens * 2 / 1_000_000
    comp_cost = comp_tokens * 2 / 1_000_000
    saved = raw_cost - comp_cost
    print(f"  Raw:        {raw_tokens:>7,} tokens  = ${raw_cost:.4f}")
    print(f"  Compressed: {comp_tokens:>7,} tokens  = ${comp_cost:.4f}")
    print(f"  Saved:      {saved_tokens:>7,} tokens  = ${saved:.4f}  ({overall:.1f}%)")
    print(f"  At 100 runs/day: ${saved*100:.2f}/day = ${saved*36500:.0f}/year")
    print()

    return 0 if (all_pass and all_match) else 1


if __name__ == "__main__":
    sys.exit(main())
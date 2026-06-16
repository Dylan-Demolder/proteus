#!/usr/bin/env python3
"""Proteus auto-compress — wraps Hermes tool output for compression.

Usage:
    python auto_compress.py <file_path>
    
Reads a file, compresses it via Proteus, and writes the compressed
version alongside. Use for pre-compressing large cache files.
"""
import sys
from pathlib import Path
from proteus import compress_tool_output

def main():
    if len(sys.argv) < 2:
        print("Usage: auto_compress.py <path>")
        sys.exit(1)
    
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    
    content = path.read_text()
    compressed, stats = compress_tool_output(content)
    
    if stats.get("was_compressed"):
        out_path = path.with_suffix(path.suffix + ".compressed")
        out_path.write_text(compressed)
        print(f"Compressed {path.name}: {stats['original_chars']:,} -> {len(compressed):,} chars")
        print(f"  Mode: {stats.get('mode', '?')}, Savings: {stats.get('compression_pct', 0):.1f}%")
    else:
        print(f"No compression applied to {path.name} ({len(content)} chars, below threshold)")

if __name__ == "__main__":
    main()

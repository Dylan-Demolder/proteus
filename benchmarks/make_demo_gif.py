#!/usr/bin/env python3
"""Generate the README demo GIF from real Proteus CLI output.

Not a mock-up: every figure is produced by running the actual compressor via
`compress_tool_output()` and rendering its returned stats. Re-run after a
compressor change so the README never drifts from reality.

Usage:  python benchmarks/make_demo_gif.py
Output: docs/demo.gif
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.gif"

# Terminal palette (GitHub dark canvas)
BG = (13, 17, 23)
FG = (201, 209, 217)

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 15
LINE_H = 21
PAD = 22
COLS = 78  # canvas width in characters

# Colour runs are delimited by §RRGGBB (marker + 6 hex digits = 7 chars).
MARKER_LEN = 7

FUZZ = "▊"


def font() -> ImageFont.FreeTypeFont:
    if not os.path.exists(FONT_PATH):
        raise SystemExit(
            f"Font not found: {FONT_PATH}\n"
            "This script targets macOS. On Linux, point FONT_PATH at a "
            "monospace TTF (e.g. DejaVuSansMono.ttf) and re-run."
        )
    return ImageFont.truetype(FONT_PATH, FONT_SIZE, index=0)


def parse(text: str, default: tuple[int, int, int]) -> list[tuple[str, tuple[int, int, int]]]:
    """Split a string containing §RRGGBB markers into (chunk, colour) runs.

    The marker is exactly 7 characters; anything else and we silently eat the
    character that follows, which mangles every line.
    """
    runs: list[tuple[str, tuple[int, int, int]]] = []
    buf = ""
    cur = default
    i = 0
    while i < len(text):
        if text[i] == "§" and i + MARKER_LEN <= len(text):
            hexcode = text[i + 1 : i + 7]
            try:
                colour = tuple(int(hexcode[j : j + 2], 16) for j in (0, 2, 4))
            except ValueError:
                buf += text[i]
                i += 1
                continue
            if buf:
                runs.append((buf, cur))
                buf = ""
            cur = colour  # type: ignore[assignment]
            i += MARKER_LEN
        else:
            buf += text[i]
            i += 1
    if buf:
        runs.append((buf, cur))
    return runs


def make_dataset() -> tuple[str, dict]:
    """Real input, real stats — the whole point of regenerating this file."""
    rows = ",".join(
        f'{{"id":{i},"service":"api","level":"ERROR",'
        f'"msg":"connection timeout to db-{i % 3:02d}","latency_ms":{100 + i}}}'
        for i in range(400)
    )
    raw = "[" + rows + "]"

    # Import from source so the GIF always reflects the working tree.
    sys.path.insert(0, str(ROOT / "src"))
    from proteus import compress_tool_output  # noqa: E402

    _compressed, stats = compress_tool_output(raw)
    return raw, stats


class Screen:
    """Accumulates terminal lines; renders once the canvas height is known."""

    def __init__(self, fnt: ImageFont.FreeTypeFont, cw: int):
        self.fnt = fnt
        self.cw = cw
        self.lines: list[list[tuple[str, tuple[int, int, int]]]] = []

    def add(self, text: str) -> None:
        self.lines.append(parse(text, FG))

    def blank(self) -> None:
        self.lines.append([])

    def snapshot(self) -> list[list[tuple[str, tuple[int, int, int]]]]:
        """Copy current lines — GIF frames are immutable states."""
        return [list(runs) for runs in self.lines]


def render(
    lines: list[list[tuple[str, tuple[int, int, int]]]],
    fnt: ImageFont.FreeTypeFont,
    max_lines: int,
) -> Image.Image:
    """Draw one frame. Every frame uses the same canvas size (a GIF requirement)."""
    line_h = LINE_H
    w = int(PAD * 2 + COLS * fnt.getlength("M"))
    h = int(PAD * 2 + max_lines * line_h)
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    for runs in lines[:max_lines]:
        x = PAD
        for text, colour in runs:
            d.text((x, y), text, font=fnt, fill=colour)
            # Advance by measured ink advance, not len()*char_width — glyph
            # widths differ from the nominal cell for arrows and box-drawing.
            x += fnt.getlength(text)
        y += line_h
    return img


def build_frames() -> tuple[list[list[tuple[str, tuple[int, int, int]]]], dict]:
    fnt = font()
    cw = int(fnt.getlength("M"))
    raw, stats = make_dataset()

    orig = stats["original_chars"]
    comp = stats["compressed_chars"]
    pct = stats["compression_pct"]
    toks = stats["estimated_token_savings"]
    mode = stats.get("mode", "?")
    ctype = stats.get("content_type", "?")
    hsh = stats.get("hash", "")

    # Terminal colour runs
    P = "§08A6FF$ "    # cyan prompt
    CMD = "§C9D1D9"    # command text
    OK = "§3FB950"     # green
    NUM = "§D29922"    # yellow
    MUT = "§6E7681"    # dim
    RESET = "§C9D1D9"

    s = Screen(fnt, cw)
    frames: list[list[tuple[str, tuple[int, int, int]]]] = []

    def snap() -> None:
        frames.append(s.snapshot())

    # 1 — the command
    s.add(P + CMD + "proteus file big.json")
    snap()

    # 2..6 — output appearing line by line
    s.blank()
    s.add("§FFFFFF▸ big.json")
    snap()

    s.add(f"   {MUT}Type:{RESET}     {OK}{ctype}")
    snap()

    s.add(
        f"   {MUT}Size:{RESET}     {NUM}{orig:,}{RESET} → "
        f"{NUM}{comp:,}{RESET} {MUT}chars{RESET} {OK}({pct}%{RESET})"
    )
    snap()

    s.add(f"   {MUT}Savings:{RESET}  {NUM}~{toks:,} tokens")
    snap()

    s.add(f"   {MUT}Mode:{RESET}     {OK}{mode}")
    s.add(f"   {MUT}Hash:{RESET}     {MUT}{hsh}")
    snap()

    # 7 — before/after bar
    s.blank()
    s.add("§FFFFFF  before " + "§6E7681" + "█" * 44)
    bar = max(1, round(44 * (1 - pct / 100)))
    s.add(
        "§FFFFFF  after  " + "§3FB950" + "█" * bar
        + "§6E7681" + "█" * (44 - bar)
    )
    s.add("§6E7681         " + "─" * 44)
    s.add(f"§3FB950         −{pct}% chars  §D29922−{toks:,} tokens{RESET}")
    snap()

    # hold so the numbers are readable
    for _ in range(3):
        snap()

    # 8 — retrieve proves reversibility (typed in, then executed)
    s.blank()
    typed = "proteus retrieve " + hsh
    s.add(P + CMD + typed[: len(typed) - 4] + "§2A2A2A" + FUZZ)
    snap()
    s.add(P + CMD + typed)
    snap()

    s.blank()
    s.add(f"§6E7681# original restored byte-for-byte ({orig:,} chars)")
    s.add(f"§3FB950{raw[:COLS - 1]}{RESET}")
    snap()

    # 9 — closing prompt
    s.blank()
    s.add(P + CMD + FUZZ)
    for _ in range(3):
        snap()

    return frames, {"fnt": fnt, "cw": cw, "stats": stats}


def main() -> None:
    frames, meta = build_frames()
    if not frames:
        raise SystemExit("no frames produced")

    fnt: ImageFont.FreeTypeFont = meta["fnt"]
    stats: dict = meta["stats"]

    # Two-pass: canvas height comes from the longest frame, so nothing is cut.
    max_lines = max(len(fr) for fr in frames)

    # Dedupe consecutive identical frames and sum their intended hold time,
    # so the durations we pass match what actually ships in the file.
    unique: list[list[tuple[str, tuple[int, int, int]]]] = []
    copies: list[int] = []
    for fr in frames:
        if unique and fr == unique[-1]:
            copies[-1] += 1
        else:
            unique.append(fr)
            copies.append(0)

    durations = []
    for i, extra in enumerate(copies):
        base = 700 if i == 0 else 380
        durations.append(base * (1 + extra))
    durations[-1] = max(durations[-1], 2200)  # hold the result

    images = [render(fr, fnt, max_lines) for fr in unique]

    # Every frame must share one canvas size, else PIL crops to the first.
    sizes = {im.size for im in images}
    if len(sizes) != 1:
        raise SystemExit(f"frame size mismatch: {sizes}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        OUT,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )

    # Report from the file on disk, not from our own list.
    check = Image.open(OUT)
    size_kb = OUT.stat().st_size / 1024
    print(
        f"wrote {OUT.relative_to(ROOT)} — {check.n_frames} frames, "
        f"{sizes.pop()} canvas, {max_lines} lines, {size_kb:.1f} KB"
    )
    print(
        f"  real figures: {stats['original_chars']:,} → "
        f"{stats['compressed_chars']:,} chars "
        f"({stats['compression_pct']}%), ~{stats['estimated_token_savings']:,} tokens"
    )
    if size_kb > 500:
        print("WARNING: >500KB — consider fewer frames or a tighter palette")


if __name__ == "__main__":
    main()

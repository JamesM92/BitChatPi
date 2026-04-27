#!/usr/bin/env python3
"""
Convert an image to terminal ASCII art or full-color block art.

ASCII mode (default):
  Characters represent pixel brightness.  Works on any terminal.

Color mode (--color):
  Uses Unicode half-block ▄ with ANSI 24-bit color.  Each character cell
  shows two pixels (top = background, bottom = foreground), giving twice
  the vertical resolution.  Requires a 24-bit color terminal.

Usage:
    img2ascii.py IMAGE [--width N] [--color] [--invert]

Options:
    IMAGE        path to any Pillow-supported image (PNG, JPEG, GIF, BMP…)
    --width N    output width in characters (default: terminal width or 80)
    --color      24-bit color half-block mode instead of ASCII grayscale
    --invert     invert brightness — use on light-background terminals

Requirements:
    pip install pillow
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not found — run:  pip install pillow")

# Character ramp: index 0 = darkest, last index = brightest.
# Looks correct on a dark terminal background.
_RAMP = r"@%#*+=-:. "
_RAMP_LEN = len(_RAMP)


def _resize(img: Image.Image, width: int, rows_per_cell: int) -> Image.Image:
    """Resize image to (width, height) preserving aspect ratio.
    rows_per_cell=1 for ASCII, 2 for half-block color mode.
    Terminal characters are roughly twice as tall as they are wide,
    so multiply height by 0.45 to compensate.
    """
    aspect = img.height / img.width
    height = max(rows_per_cell, int(width * aspect * 0.45 * rows_per_cell))
    height -= height % rows_per_cell  # keep even for half-block
    return img.resize((width, height), Image.LANCZOS)


def to_ascii(img: Image.Image, width: int, invert: bool) -> str:
    small = _resize(img, width, rows_per_cell=1).convert("L")
    w, h = small.size
    pixels = small.load()
    lines = []
    for row in range(h):
        row_chars = []
        for col in range(w):
            lum = pixels[col, row]
            if invert:
                lum = 255 - lum
            idx = lum * (_RAMP_LEN - 1) // 255
            row_chars.append(_RAMP[idx])
        lines.append("".join(row_chars))
    return "\n".join(lines)


def to_color_blocks(img: Image.Image, width: int) -> str:
    """Half-block mode: each terminal cell holds two vertically-stacked pixels.
    Top pixel → background color, bottom pixel → foreground color, char = ▄.
    """
    small = _resize(img, width, rows_per_cell=2).convert("RGB")
    w, h = small.size
    pixels = small.load()
    lines = []
    for row in range(0, h, 2):
        parts = []
        for col in range(w):
            r1, g1, b1 = pixels[col, row]        # top    → background
            r2, g2, b2 = pixels[col, row + 1]    # bottom → foreground
            parts.append(
                f"\x1b[38;2;{r2};{g2};{b2}m"    # fg
                f"\x1b[48;2;{r1};{g1};{b1}m"    # bg
                "▄"                          # ▄
            )
        parts.append("\x1b[0m")  # reset colors at end of row
        lines.append("".join(parts))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("image", help="input image file")
    ap.add_argument(
        "--width", type=int, default=None,
        help="output width in characters (default: terminal width)",
    )
    ap.add_argument(
        "--color", action="store_true",
        help="ANSI 24-bit color half-block mode",
    )
    ap.add_argument(
        "--invert", action="store_true",
        help="invert brightness (for light-background terminals)",
    )
    args = ap.parse_args()

    if args.width is None:
        try:
            args.width = os.get_terminal_size().columns
        except OSError:
            args.width = 80

    try:
        img = Image.open(args.image)
    except FileNotFoundError:
        sys.exit(f"File not found: {args.image}")
    except Exception as exc:
        sys.exit(f"Cannot open image: {exc}")

    if args.color:
        print(to_color_blocks(img, args.width))
    else:
        print(to_ascii(img, args.width, args.invert))


if __name__ == "__main__":
    main()

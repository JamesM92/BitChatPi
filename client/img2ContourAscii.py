#!/usr/bin/env python3
"""
Python port of Alex Harri's sub-character ASCII rendering.
Reference: https://alexharri.com/blog/ascii-rendering

Algorithm (faithful to the original):
  - Six internal sampling circles per cell → 6D shape vector
  - Ten external circles just outside cell boundaries → directional crunch
  - Global crunch (normalize-power-restore) on top of directional crunch
  - KD-tree nearest-neighbour lookup against pre-computed character vectors

Extras beyond Alex's original design:
  - ANSI 24-bit colour output  (--color)
  - Write to file              (-o)
  - Invert lightness           (--invert)
  - Auto-contrast stretch      (--autocontrast)
  - Character exclusion list   (--exclude)
  - Configurable cell aspect   (--char-ratio)
  - Auto terminal-width cols   (omit --cols)
"""

import argparse
import json
import os
import shutil
import sys
import numpy as np
from PIL import Image
from scipy.spatial import KDTree


# ─────────────────────────────────────────────────────────────────────────────
# Sampling geometry  —  values taken directly from default.json metadata
# ─────────────────────────────────────────────────────────────────────────────

INTERNAL_CIRCLES = [
    (0.30, 0.23), (0.70, 0.18),
    (0.30, 0.50), (0.70, 0.50),
    (0.30, 0.82), (0.70, 0.77),
]

# Circle radius as a fraction of cell_w  (circleRadius=0.28125, width=1.0)
CIRCLE_RADIUS = 0.28125

EXTERNAL_CIRCLES = [
    ( 0.07, -0.21), ( 0.93, -0.21),
    (-0.25,  0.07), ( 1.25,  0.07),
    (-0.25,  0.50), ( 1.25,  0.50),
    (-0.25,  0.93), ( 1.25,  0.93),
    ( 0.07,  1.21), ( 0.93,  1.21),
]

AFFECTING_EXTERNAL = [
    [0, 1, 2, 4],
    [0, 1, 3, 5],
    [2, 4, 6],
    [3, 5, 7],
    [4, 6, 8, 9],
    [5, 7, 8, 9],
]

# Default cell aspect ratio from metadata  (height=4/3, width=1.0)
CELL_ASPECT = 4 / 3


# ─────────────────────────────────────────────────────────────────────────────
# Character vectors
# ─────────────────────────────────────────────────────────────────────────────

def _load_char_vectors(exclude: str):
    path = os.path.join(os.path.dirname(__file__), "default.json")
    with open(path) as f:
        data = json.load(f)

    entries = [e for e in data["characters"] if e["char"] not in exclude]
    chars = [e["char"] for e in entries]
    vecs  = np.array([e["vector"] for e in entries], dtype=np.float64)

    col_max = vecs.max(axis=0)
    col_max[col_max == 0] = 1.0
    vecs /= col_max
    return chars, vecs


# ─────────────────────────────────────────────────────────────────────────────
# Pixel masks
# ─────────────────────────────────────────────────────────────────────────────

def _build_masks(cell_w: float, cell_h: float):
    """
    Pre-compute (dy, dx) integer offset arrays for each circle.
    Returns (internal_masks, external_masks) as lists of (dy_arr, dx_arr) pairs.
    """
    radius = CIRCLE_RADIUS * cell_w
    r_ceil = int(np.ceil(radius))
    r_sq   = radius * radius

    def make(cx_frac, cy_frac):
        cx  = round(cx_frac * cell_w)
        cy  = round(cy_frac * cell_h)
        pts = [
            (cy + dy, cx + dx)
            for dy in range(-r_ceil, r_ceil + 1)
            for dx in range(-r_ceil, r_ceil + 1)
            if dx * dx + dy * dy < r_sq
        ]
        if not pts:
            pts = [(cy, cx)]
        ys, xs = zip(*pts)
        return np.array(ys, dtype=np.int32), np.array(xs, dtype=np.int32)

    internal = [make(cx, cy) for cx, cy in INTERNAL_CIRCLES]
    external = [make(cx, cy) for cx, cy in EXTERNAL_CIRCLES]
    return internal, external


# ─────────────────────────────────────────────────────────────────────────────
# Lightness
# ─────────────────────────────────────────────────────────────────────────────

def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Relative luminance (ITU-R BT.709).  (..., 3) float → (...) [0, 1]."""
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0


# ─────────────────────────────────────────────────────────────────────────────
# Vectorised sampling
# ─────────────────────────────────────────────────────────────────────────────

def _sample_all(light: np.ndarray, rows: int, cols: int,
                cell_w: float, cell_h: float, masks: list) -> np.ndarray:
    """
    Sample every cell for every circle using pure numpy indexing — no Python
    pixel loops.  Returns shape (rows, cols, n_circles) float32.
    """
    h, w = light.shape
    n    = len(masks)
    out  = np.empty((rows, cols, n), dtype=np.float32)

    oy = (np.arange(rows) * cell_h).astype(np.int32)  # (rows,)
    ox = (np.arange(cols) * cell_w).astype(np.int32)  # (cols,)

    for ci, (dy_arr, dx_arr) in enumerate(masks):
        # py: (rows, 1, pts)   px: (1, cols, pts)  →  broadcast to (rows, cols, pts)
        py = np.clip(
            oy[:, np.newaxis, np.newaxis] + dy_arr[np.newaxis, np.newaxis, :],
            0, h - 1,
        )
        px = np.clip(
            ox[np.newaxis, :, np.newaxis] + dx_arr[np.newaxis, np.newaxis, :],
            0, w - 1,
        )
        out[:, :, ci] = light[py, px].mean(axis=2)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Contrast enhancement  (batch versions operating on (rows, cols, 6) arrays)
# ─────────────────────────────────────────────────────────────────────────────

def _global_crunch(sv: np.ndarray, exp: float) -> np.ndarray:
    m = sv.max(axis=2, keepdims=True)
    m = np.where(m == 0, 1.0, m)
    return (sv / m) ** exp * m


def _directional_crunch(sv: np.ndarray, ev: np.ndarray, exp: float) -> np.ndarray:
    out = sv.copy()
    for i, idxs in enumerate(AFFECTING_EXTERNAL):
        ctx  = ev[:, :, idxs].max(axis=2)          # (rows, cols)
        mask = ctx > sv[:, :, i]
        if not mask.any():
            continue
        safe_ctx     = np.where(mask, ctx, 1.0)
        ratio        = sv[:, :, i] / safe_ctx
        out[:, :, i] = np.where(mask, ratio ** exp * ctx, sv[:, :, i])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Quantised lookup cache
# ─────────────────────────────────────────────────────────────────────────────

_Q_RANGE = 8   # 8 levels per dimension  →  8^6 = 262,144 possible keys
_Q_BITS  = 3   # bits per dimension

def _quantise_keys(sv_flat: np.ndarray) -> np.ndarray:
    q    = np.clip((sv_flat * _Q_RANGE).astype(np.int32), 0, _Q_RANGE - 1)
    keys = np.zeros(len(sv_flat), dtype=np.int32)
    for i in range(6):
        keys = (keys << _Q_BITS) | q[:, i]
    return keys


def _cached_query(tree: KDTree, sv_flat: np.ndarray, cache: dict) -> np.ndarray:
    """
    KD-tree query with quantised result cache.  After the cache warms up, most
    lookups become O(1) dict hits — important for live video where adjacent
    frames share many similar cell values.
    """
    keys = _quantise_keys(sv_flat)
    unique_keys, first_idx, inv = np.unique(keys, return_index=True, return_inverse=True)

    missing_mask = np.array([int(k) not in cache for k in unique_keys], dtype=bool)
    if missing_mask.any():
        _, char_idx = tree.query(sv_flat[first_idx[missing_mask]])
        for k, ci in zip(unique_keys[missing_mask], char_idx):
            cache[int(k)] = int(ci)

    return np.array([cache[int(k)] for k in unique_keys])[inv]


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────

class Renderer:
    """
    Stateful renderer.  Build once, call render_frame() for each image/frame.
    Caches circle masks (keyed by cell dimensions) and lookup results
    (quantised KD-tree cache) across calls — makes video rendering efficient.
    """

    def __init__(
        self,
        cols:              int   = None,
        global_exp:        float = 2.2,
        directional_exp:   float = 2.8,
        use_color:         bool  = False,
        invert:            bool  = False,
        char_aspect:       float = CELL_ASPECT,
        exclude:           str   = "",
        autocontrast:      bool  = False,
    ):
        if cols is None:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        self.cols            = cols
        self.global_exp      = global_exp
        self.directional_exp = directional_exp
        self.use_color       = use_color
        self.invert          = invert
        self.char_aspect     = char_aspect
        self.autocontrast    = autocontrast

        chars, vecs  = _load_char_vectors(exclude)
        self.chars   = np.array(chars)
        self.tree    = KDTree(vecs)

        self._mask_cache   = {}   # (cell_w_key, cell_h_key) → (int_masks, ext_masks)
        self._lookup_cache = {}   # quantised key → char index

    # ------------------------------------------------------------------

    def _masks(self, cell_w: float, cell_h: float):
        key = (int(cell_w * 1000), int(cell_h * 1000))
        if key not in self._mask_cache:
            self._mask_cache[key] = _build_masks(cell_w, cell_h)
        return self._mask_cache[key]

    # ------------------------------------------------------------------

    def render_frame(self, img_arr: np.ndarray) -> str:
        """
        Convert a (H, W, 3) uint8 / float32 RGB array to an ASCII string.
        This is the core function used by both the CLI and the video script.
        """
        h, w  = img_arr.shape[:2]
        light = _luminance(img_arr.astype(np.float32))

        if self.autocontrast:
            lo, hi = light.min(), light.max()
            if hi > lo:
                light = (light - lo) / (hi - lo)

        if self.invert:
            light = 1.0 - light

        cell_w = w / self.cols
        cell_h = cell_w * self.char_aspect
        rows   = max(1, round(h / cell_h))

        int_masks, ext_masks = self._masks(cell_w, cell_h)

        sv = _sample_all(light, rows, self.cols, cell_w, cell_h, int_masks)
        ev = _sample_all(light, rows, self.cols, cell_w, cell_h, ext_masks)

        sv = _directional_crunch(sv, ev, self.directional_exp)
        sv = _global_crunch(sv, self.global_exp)

        sv_flat   = sv.reshape(-1, 6).astype(np.float64)
        idx_flat  = _cached_query(self.tree, sv_flat, self._lookup_cache)
        char_grid = self.chars[idx_flat].reshape(rows, self.cols)

        if not self.use_color:
            return "\n".join("".join(row) for row in char_grid)

        # ── Colour output ──────────────────────────────────────────────
        # Vectorised cell-colour averaging: build pixel coordinate grids for
        # all cells at once, sample img_arr in a single indexing op, then
        # mean-pool over the cell's pixel dimensions.
        ch_int = max(1, int(cell_h))
        cw_int = max(1, int(cell_w))

        oy_arr = (np.arange(rows)      * cell_h).astype(np.int32)  # (rows,)
        ox_arr = (np.arange(self.cols) * cell_w).astype(np.int32)  # (cols,)

        # py: (rows, ch_int, 1, 1)   px: (1, 1, cols, cw_int)
        # broadcast → (rows, ch_int, cols, cw_int) pixel indices
        py = np.clip(
            oy_arr[:, np.newaxis, np.newaxis, np.newaxis]
            + np.arange(ch_int)[np.newaxis, :, np.newaxis, np.newaxis],
            0, h - 1,
        ).astype(np.int32)
        px = np.clip(
            ox_arr[np.newaxis, np.newaxis, :, np.newaxis]
            + np.arange(cw_int)[np.newaxis, np.newaxis, np.newaxis, :],
            0, w - 1,
        ).astype(np.int32)

        # img_arr[py, px] → (rows, ch_int, cols, cw_int, 3)
        color_grid = img_arr[py, px].mean(axis=(1, 3))  # (rows, cols, 3)

        # Quantise to 16-level steps then saturate-boost (brightest channel → 255).
        # Only boost cells where the max channel is ≥ 48; darker cells are treated
        # as black so near-black backgrounds with a faint hue don't become vivid.
        rgb_q = (color_grid // 16) * 16
        mx    = rgb_q.max(axis=2, keepdims=True)                    # (rows, cols, 1)
        safe  = np.where(mx >= 48, mx, 1.0)
        rgb_q = np.where(mx >= 48, np.clip(rgb_q * (255.0 / safe), 0, 255), rgb_q)
        rgb_q = rgb_q.astype(np.uint8)                              # (rows, cols, 3)

        # String formatting — unavoidably a Python loop, but the expensive
        # per-cell mean() is gone; this is now just indexing + f-string work.
        lines = []
        for r in range(rows):
            parts = []
            for c in range(self.cols):
                char = char_grid[r, c]
                if char == " ":
                    parts.append(" ")
                else:
                    R, G, B = int(rgb_q[r, c, 0]), int(rgb_q[r, c, 1]), int(rgb_q[r, c, 2])
                    parts.append(f"\033[38;2;{R};{G};{B}m{char}\033[0m")
            lines.append("".join(parts))
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper (file path → string)
# ─────────────────────────────────────────────────────────────────────────────

def convert(image_path: str, **kwargs) -> str:
    renderer = Renderer(**kwargs)
    img_arr  = np.array(Image.open(image_path).convert("RGB"), dtype=np.float32)
    return renderer.render_frame(img_arr)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ASCII renderer — faithful Python port of Alex Harri's design",
    )
    parser.add_argument("image", help="input image file")
    parser.add_argument(
        "-o", "--output", nargs="?", const="",
        help="write to file; omit filename to auto-generate from input name",
    )
    parser.add_argument(
        "--cols", type=int, default=None,
        help="output columns (default: terminal width)",
    )
    parser.add_argument(
        "--global-crunch", dest="global_exp", type=float, default=2.2,
        help="global contrast exponent (default: 2.2)",
    )
    parser.add_argument(
        "--directional-crunch", dest="directional_exp", type=float, default=2.8,
        help="directional contrast exponent (default: 2.8)",
    )
    parser.add_argument("--color",        action="store_true", help="ANSI 24-bit colour output")
    parser.add_argument("--invert",       action="store_true", help="invert lightness (bright→sparse, dark→dense)")
    parser.add_argument("--autocontrast", action="store_true", help="stretch luminance range to [0, 1] before rendering")
    parser.add_argument(
        "--char-ratio", dest="char_aspect", type=float, default=CELL_ASPECT,
        help=f"cell height/width ratio (default: {CELL_ASPECT:.4f}); tune for your terminal font",
    )
    parser.add_argument(
        "--exclude", default="",
        help='characters to never use, e.g. --exclude "|$\\\\"',
    )
    args = parser.parse_args()

    renderer = Renderer(
        cols            = args.cols,
        global_exp      = args.global_exp,
        directional_exp = args.directional_exp,
        use_color       = args.color,
        invert          = args.invert,
        char_aspect     = args.char_aspect,
        exclude         = args.exclude,
        autocontrast    = args.autocontrast,
    )

    img_arr = np.array(Image.open(args.image).convert("RGB"), dtype=np.float32)
    print("Rendering…", end="\r", flush=True)
    result  = renderer.render_frame(img_arr)
    print("           ", end="\r", flush=True)

    if args.output is not None:
        path = args.output or (
            os.path.splitext(os.path.basename(args.image))[0] + ".txt"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Saved to {path}")
    else:
        print(result)


if __name__ == "__main__":
    main()

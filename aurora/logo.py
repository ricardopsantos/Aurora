"""Logo rendering for the startup banner.

Resolves the configured png path (relative to the config directory) and
renders it as ANSI 256-colour half-block art so it can sit on the left of
the text banner with no terminal image-protocol dependencies.  Requires
Pillow (declared in pyproject.toml).
"""

from __future__ import annotations

import re
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\033[0m"

_PALETTE: list[tuple[int, int, int]] | None = None


def visible_width(s: str) -> int:
    """Display width of a string that contains ANSI SGR escape codes."""
    return len(_ANSI_RE.sub("", s))


def resolve_logo(cfg: dict) -> Path | None:
    """Return an existing absolute Path for runtime.logo, if configured."""
    logo = (cfg.get("runtime") or {}).get("logo")
    if not logo:
        return None
    p = Path(logo)
    if not p.is_absolute():
        base = cfg.get("_base_dir")
        if base:
            p = Path(base) / p
    return p if p.exists() else None


def _palette() -> list[tuple[int, int, int]]:
    global _PALETTE
    if _PALETTE is not None:
        return _PALETTE
    colors: list[tuple[int, int, int]] = [
        # 16 system colours (xterm approximations)
        (0, 0, 0),
        (205, 0, 0),
        (0, 205, 0),
        (205, 205, 0),
        (0, 0, 238),
        (205, 0, 205),
        (0, 205, 205),
        (229, 229, 229),
        (127, 127, 127),
        (255, 0, 0),
        (0, 255, 0),
        (255, 255, 0),
        (92, 92, 255),
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 255),
    ]
    for r in range(6):
        for g in range(6):
            for b in range(6):
                colors.append((r * 51, g * 51, b * 51))
    for i in range(24):
        v = 8 + 10 * i
        colors.append((v, v, v))
    _PALETTE = colors
    return colors


def _nearest_color(r: int, g: int, b: int) -> int:
    best, best_d = 0, float("inf")
    for i, (pr, pg, pb) in enumerate(_palette()):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best = d, i
    return best


def render(path: Path | str, max_rows: int = 8, max_cols: int = 18,
           dark_threshold: int = 48) -> list[str]:
    """Render a PNG to a list of ANSI half-block lines.

    Each line is one display row; two image pixels are packed vertically into
    one character (top as foreground, bottom as background) using Unicode
    upper-half block ``▀``.  The returned strings contain ANSI colour codes
    but are padded so that every line has the same visual width.

    Near-black pixels (luminance < ``dark_threshold``) count as background,
    not ink — banner art is drawn on a dark terminal, so an opaque black
    canvas must vanish the same way transparency does.  Thin strokes are
    dilated before downscaling so line art survives the shrink to ~16px.
    """
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    img = Image.open(path).convert("RGBA")
    bbox = img.split()[3].getbbox()
    if bbox:  # waste no cells on transparent margins
        img = img.crop(bbox)
    orig_w, orig_h = img.size
    if orig_w == 0 or orig_h == 0:
        return []

    rgb = Image.new("RGB", img.size, (0, 0, 0))
    alpha = img.split()[3]
    rgb.paste(img.convert("RGB"), mask=alpha)
    alpha_mask = alpha.point(lambda v: 255 if v >= 128 else 0)
    lo, hi = alpha.getextrema()
    if lo < 128:
        # Real transparency — alpha alone is the subject mask; keying out
        # dark pixels would punch holes in dark hair/shadows of a photo.
        mask = alpha_mask
    else:
        # Fully opaque canvas (e.g. line art on black): treat near-black as
        # background so the art floats on the dark terminal.
        mask = ImageChops.multiply(
            alpha_mask,
            rgb.convert("L").point(
                lambda v: 255 if v >= dark_threshold else 0))

    target_h = max(2, max_rows * 2)
    target_w = int(round(orig_w * target_h / orig_h))
    if max_cols and target_w > max_cols:
        target_w = max_cols
        target_h = int(round(orig_h * target_w / orig_w))
    # Don't upscale tiny images; avoid distorting them.
    if target_w > orig_w:
        target_w, target_h = orig_w, orig_h
    # Half-block art needs an even number of pixel rows.
    if target_h % 2:
        target_h = max(2, target_h - 1)
    target_w = max(1, target_w)
    target_h = max(2, target_h)

    # Monochrome line art (one ink colour, e.g. a sketch) renders far cleaner
    # as a solid single-colour glyph than as mottled antialiased greys.
    ink_px = [rgb.getpixel((x, y))
              for y in range(0, orig_h, 4) for x in range(0, orig_w, 4)
              if mask.getpixel((x, y)) >= 128]
    mono = False
    ink: tuple[int, int, int] = (255, 255, 255)
    if ink_px:
        avg = tuple(sum(p[i] for p in ink_px) // len(ink_px) for i in range(3))
        # One ink = every stroke pixel is a darker/lighter shade of the same
        # colour; compare hue direction (channels scaled to max=255), not
        # brightness, so antialiasing doesn't defeat the test.
        def _norm(p: tuple[int, ...]) -> tuple[int, int, int]:
            m = max(p[:3]) or 1
            return tuple(c * 255 // m for c in p[:3])
        n_avg = _norm(avg)
        mono = all(sum(abs(a - b) for a, b in zip(_norm(p), n_avg)) < 90
                   for p in ink_px[:: max(1, len(ink_px) // 256)])
        boost = 235 / max(avg) if max(avg) else 1
        ink = tuple(min(255, int(c * boost)) for c in avg)

    if (target_w, target_h) != (orig_w, orig_h):
        # A 1px stroke shrunk 10x averages into the background; thicken the
        # ink roughly in proportion to the downscale, then area-average so
        # the mask carries per-cell coverage instead of aliased dashes.
        # Photos (real transparency) need no dilation — it would smear them.
        scale = orig_w / target_w
        if scale > 1.5 and lo >= 128:
            k = min(15, 2 * int(scale / 2) + 1)
            if k >= 3:
                mask = mask.filter(ImageFilter.MaxFilter(k))
                rgb = rgb.filter(ImageFilter.MaxFilter(k))
        rgb = rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)
        mask = mask.resize((target_w, target_h), Image.Resampling.BOX)
    rgb = ImageOps.autocontrast(rgb, cutoff=1)

    lines: list[str] = []

    for row in range(0, target_h, 2):
        cur_fg: int | None = None
        cur_bg: int | None = None
        buf: list[str] = []

        for x in range(target_w):
            if mono:
                top = bot = ink
            else:
                top = rgb.getpixel((x, row))
                bot = rgb.getpixel((x, row + 1))
            top_on = mask.getpixel((x, row)) >= 64
            bot_on = mask.getpixel((x, row + 1)) >= 64

            if not top_on and not bot_on:
                if cur_fg is not None or cur_bg is not None:
                    buf.append(RESET)
                    cur_fg = cur_bg = None
                buf.append(" ")
            elif top_on and bot_on:
                fg = _nearest_color(*top[:3])
                bg = _nearest_color(*bot[:3])
                if fg != cur_fg or bg != cur_bg:
                    buf.append(f"\033[38;5;{fg};48;5;{bg}m")
                    cur_fg, cur_bg = fg, bg
                buf.append("▀")
            elif top_on:
                fg = _nearest_color(*top[:3])
                if fg != cur_fg or cur_bg is not None:
                    buf.append(f"\033[38;5;{fg}m")
                    cur_fg, cur_bg = fg, None
                buf.append("▀")
            else:  # bottom pixel only
                fg = _nearest_color(*bot[:3])
                if fg != cur_fg or cur_bg is not None:
                    buf.append(f"\033[38;5;{fg}m")
                    cur_fg, cur_bg = fg, None
                buf.append("▄")

        if cur_fg is not None or cur_bg is not None:
            buf.append(RESET)
        lines.append("".join(buf))

    return lines

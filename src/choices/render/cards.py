"""Pillow-rendered overlays: the end-of-episode choice card and the cover.

Layout respects the TikTok/Reels UI: nothing important in the bottom ~20%
(caption and username) or the right ~15% from mid-height down (like, comment
and share rail). Content sits in a left-weighted column.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..models import EpisodeScript
from .fonts import font_path

WHITE = (255, 255, 255)
AMBER = (255, 196, 92)
A_COL = (229, 72, 77)
B_COL = (18, 165, 148)
INK = (14, 16, 20)


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(weight), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text: str, weight: str, start: int, max_w: int, max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    size = start
    while size > 20:
        f = _font(weight, size)
        lines = _wrap(draw, text, f, max_w)
        if len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= max_w for ln in lines):
            return f, lines
        size -= 4
    f = _font(weight, size)
    return f, _wrap(draw, text, f, max_w)


def _spaced(draw, xy, text, font, fill, tracking: float) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def choice_card(script: EpisodeScript, day: int, width: int, height: int, out: Path, window_hours: float = 24) -> Path:
    """Transparent full-frame overlay drawn on top of the blurred last shot."""
    # Scale to whichever is tighter so 4:5 and 1:1 ad cuts fit too.
    s = min(width / 1080, height / 1350)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    left = int(80 * width / 1080)
    col_w = int(width * 0.80) - left  # stay clear of the right-hand action rail

    y = int(height * (0.17 if height / width > 1.5 else 0.08))
    _spaced(d, (left, y), f"DAY {day}  ·  YOU DECIDE", _font("semibold", int(34 * s)), AMBER, 4 * s)
    y += int(70 * s)

    qf, qlines = _fit(d, script.question, "black", int(78 * s), col_w, 4)
    for ln in qlines:
        d.text((left, y), ln, font=qf, fill=WHITE)
        y += int(qf.size * 1.12)
    y += int(56 * s)

    if script.vote_style == "yes_no":
        options = [("A", "YES", script.choice_a.label, A_COL), ("B", "NO", script.choice_b.label, B_COL)]
    else:
        options = [("A", "A", script.choice_a.label, A_COL), ("B", "B", script.choice_b.label, B_COL)]

    btn_h = int(150 * s)
    for _key, badge, label, colour in options:
        d.rounded_rectangle([left, y, left + col_w, y + btn_h], radius=int(34 * s), fill=colour + (240,))
        r = int(48 * s)
        cx, cy = left + int(30 * s) + r, y + btn_h // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE + (255,))
        bf = _font("black", int((44 if len(badge) > 1 else 58) * s))
        bw = d.textlength(badge, font=bf)
        d.text((cx - bw / 2, cy - bf.size * 0.62), badge, font=bf, fill=colour)
        text_x = cx + r + int(30 * s)
        lf, llines = _fit(d, label, "black", int(56 * s), left + col_w - text_x - int(24 * s), 2)
        total = len(llines) * lf.size * 1.05
        ty = cy - total / 2 - lf.size * 0.12
        for ln in llines:
            d.text((text_x, ty), ln, font=lf, fill=WHITE)
            ty += lf.size * 1.05
        y += btn_h + int(30 * s)

    y += int(26 * s)
    cf = _font("black", int(50 * s))
    cta = "COMMENT YES OR NO" if script.vote_style == "yes_no" else "COMMENT A OR B"
    d.text((left, y), cta, font=cf, fill=WHITE)
    y += int(66 * s)
    hours = int(window_hours) if float(window_hours).is_integer() else window_hours
    d.text((left, y), f"Voting closes in {hours}h. Tomorrow's episode follows the vote.", font=_font("semibold", int(30 * s)), fill=(210, 214, 220))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def cover(image: Path, script: EpisodeScript, day: int, series_title: str, width: int, height: int, out: Path) -> Path:
    """Feed cover: the opening shot, darkened at the top, with day number and hook."""
    base = Image.open(image).convert("RGB")
    scale = max(width / base.width, height / base.height)
    base = base.resize((int(base.width * scale) + 1, int(base.height * scale) + 1), Image.LANCZOS)
    lx, ty = (base.width - width) // 2, (base.height - height) // 2
    base = base.crop((lx, ty, lx + width, ty + height))

    shade = Image.new("L", (1, height))
    for yy in range(height):
        t = yy / height
        shade.putpixel((0, yy), int(215 * max(0.0, 1 - t / 0.55) ** 1.4))
    shade = shade.resize((width, height))
    base.paste(Image.new("RGB", (width, height), INK), (0, 0), shade)

    d = ImageDraw.Draw(base)
    s = width / 1080
    left = int(80 * s)
    y = int(height * 0.11)
    _spaced(d, (left, y), f"{series_title.upper()}  ·  DAY {day}", _font("semibold", int(36 * s)), AMBER, 5 * s)
    y += int(80 * s)
    hf, lines = _fit(d, script.hook.upper(), "black", int(104 * s), int(width * 0.84) - left, 4)
    for ln in lines:
        d.text((left, y), ln, font=hf, fill=WHITE)
        y += int(hf.size * 1.06)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out, quality=90)
    return out


def blurred_backdrop(image: Path, width: int, height: int, out: Path) -> Path:
    """Darkened, blurred version of the last shot to sit behind the choice card."""
    base = Image.open(image).convert("RGB")
    scale = max(width / base.width, height / base.height)
    base = base.resize((int(base.width * scale) + 1, int(base.height * scale) + 1), Image.LANCZOS)
    lx, ty = (base.width - width) // 2, (base.height - height) // 2
    base = base.crop((lx, ty, lx + width, ty + height)).filter(ImageFilter.GaussianBlur(max(8, width // 50)))
    dark = Image.new("RGB", (width, height), INK)
    Image.blend(base, dark, 0.55).save(out)
    return out

"""Bundled fonts (Inter, SIL OFL 1.1, see assets/fonts/OFL.txt)."""

from __future__ import annotations

from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

FILES = {
    "black": "InterDisplay-Black.otf",
    "bold": "InterDisplay-Bold.otf",
    "semibold": "Inter-SemiBold.otf",
}

# Family names as libass sees them (used in the .ass caption styles).
ASS_BLACK = "Inter Display Black"
ASS_BOLD = "Inter Display"


def font_path(weight: str = "bold") -> str:
    p = FONTS_DIR / FILES.get(weight, FILES["bold"])
    if p.exists():
        return str(p)
    for fallback in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"):
        if Path(fallback).exists():
            return fallback
    raise FileNotFoundError(f"font {p} missing and no system fallback found")

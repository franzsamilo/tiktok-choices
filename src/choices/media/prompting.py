"""Compile a scene into image and motion prompts.

The writer only names characters, and this module injects each character's
locked look from the bible. Consistency is the hardest problem in serialized
AI video. Letting the language model re-describe Lola Cora every day means
she'd drift a little each episode (glasses one day, none the next), so the
description is fixed text that no model gets to paraphrase.

Order matters too. Image models weight early tokens most, so the prompt goes
subject and action first, then character locks, then the global style.
"""

from __future__ import annotations

import re

from ..config import SeriesBible
from ..models import Scene


def image_prompt(scene: Scene, bible: SeriesBible, ref_names: list[str] | None = None, fix: str = "") -> tuple[str, str]:
    """Return (prompt, negative).

    ``ref_names`` lists, in order, the characters whose reference sheets are
    attached as images 1..n; the prompt tells the model which is which.
    ``fix`` is the frame judge's correction from a rejected attempt.
    """
    visual = scene.visual.strip().rstrip(".")
    appended: list[str] = []
    for name in scene.characters:
        sheet = bible.character(name)
        if sheet is None:
            continue
        look = " ".join(sheet.look.split())
        pattern = re.compile(rf"\b{re.escape(sheet.name)}\b", re.IGNORECASE)
        if pattern.search(visual):
            visual = pattern.sub(f"{sheet.name} ({look})", visual, count=1)
        else:
            appended.append(f"{sheet.name}: {look}")
    parts = [visual]
    if appended:
        parts.append("In frame: " + "; ".join(appended))
    if ref_names:
        mapping = ", ".join(f"image {i + 1} is {n}" for i, n in enumerate(ref_names))
        parts.append(
            f"Reference images: {mapping}. Keep each character's face, hair, build and clothing exactly as in "
            "their reference; ignore the references' background and pose"
        )
    if fix:
        parts.append(fix.strip().rstrip("."))
    parts.append(" ".join(bible.visual_style.split()))
    return ". ".join(parts), " ".join(bible.negative_prompt.split())


def cast_prompt(name: str, look: str, bible: SeriesBible) -> str:
    """A character reference sheet: generated once, then attached to every shot the character is in."""
    return (
        f"Character reference sheet for {name}: {' '.join(look.split())}. Two views side by side on a plain light grey "
        "studio background: a full-body front view and a head-and-shoulders portrait. Neutral expression, even soft "
        "lighting, sharp focus on the face and clothing, photorealistic, cinematic 35mm look, no text, no watermark"
    )


def motion_prompt(scene: Scene) -> str:
    return f"{scene.motion.strip().rstrip('.')}. {scene.visual.strip()}. Subtle, realistic motion, no cuts, no text."

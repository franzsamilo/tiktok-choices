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


def image_prompt(scene: Scene, bible: SeriesBible) -> tuple[str, str]:
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
    parts.append(" ".join(bible.visual_style.split()))
    return ". ".join(parts), " ".join(bible.negative_prompt.split())


def motion_prompt(scene: Scene) -> str:
    return f"{scene.motion.strip().rstrip('.')}. {scene.visual.strip()}. Subtle, realistic motion, no cuts, no text."

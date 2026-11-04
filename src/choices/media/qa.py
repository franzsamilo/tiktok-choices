"""The frame judge: a vision model that looks at every generated still before it costs more money.

Image models fail in ways a text pipeline can't see: six fingers, a melted
face, a sign with garbled letters, the wrong character, a "safe" prompt that
came back gory. Catching that after a $1 image-to-video clip has been made is
expensive, and catching it at human review costs a whole re-render. So every
frame is checked right after generation, against the shot description, the
character reference sheets, and the content rules. A failed frame is
re-rolled with a new seed plus the judge's one-line fix, and the best attempt
is kept if none pass, flagged for the human.

The judge sees a downscaled JPEG (about 768px), which is enough to spot these
problems and costs roughly a quarter of the tokens of a full-size frame.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import SeriesBible
from ..llm import Claude, image_block
from ..models import FrameVerdict, LLMCall, Scene

SYSTEM = """You are the art director of a daily AI-generated vertical video series. You check each generated frame before it goes into the edit. You are strict about things a viewer would notice and relaxed about things they wouldn't.

Check the candidate frame (always the last image) for:
1. Does it show what the shot description asks for (subject, action, framing, mood)? Small creative liberties are fine.
2. If recurring characters are in the shot, do they match their reference images (face, hair, age, build, clothing)? Ignore differences in pose and background.
3. Visible defects: extra or fused fingers, distorted faces, duplicated limbs, garbled or legible text and signage, watermarks, logos.
4. Safety for a 13+ audience: no gore, blood, wounds or injury close-ups.
5. Vertical composition: the main subject shouldn't sit in the bottom fifth of the frame, where on-screen captions go.

Score 1-5 for usability as a frame of the series: 1 unusable, 3 acceptable for a background shot, 5 excellent. Put a single concrete phrase in `fix` that would most improve a retry (for example "both hands fully visible, five fingers each"), or leave it empty if the frame is fine."""


class FrameJudge(Protocol):
    def judge(self, image: Path, scene: Scene, refs: dict[str, Path]) -> tuple[FrameVerdict | None, LLMCall | None]: ...


def passes(v: FrameVerdict | None, min_score: int) -> bool:
    if v is None:  # not judged (judge disabled / offline)
        return True
    return v.matches_shot and v.characters_match and not v.unsafe and v.score >= min_score


class ClaudeFrameJudge:
    def __init__(self, claude: Claude, bible: SeriesBible, model: str, effort: str):
        self.claude, self.bible, self.model, self.effort = claude, bible, model, effort

    def judge(self, image: Path, scene: Scene, refs: dict[str, Path]) -> tuple[FrameVerdict, LLMCall]:
        content: list[dict] = []
        for i, (name, path) in enumerate(refs.items(), 1):
            content += [{"type": "text", "text": f"Reference image {i}: {name}"}, image_block(path, 512)]
        looks = []
        for name in scene.characters:
            sheet = self.bible.character(name)
            if sheet:
                looks.append(f"- {sheet.name}: {' '.join(sheet.look.split())}")
        brief = (
            f"Shot description: {scene.visual}\n"
            f"Characters in shot: {', '.join(scene.characters) or 'none'}\n"
            + ("Locked looks:\n" + "\n".join(looks) + "\n" if looks else "")
            + f"Series style: {' '.join(self.bible.visual_style.split())}\n\nCandidate frame:"
        )
        content += [{"type": "text", "text": brief}, image_block(image)]
        return self.claude.structured(
            purpose="frame-judge",
            model=self.model,
            effort=self.effort,
            system=SYSTEM,
            user=content,
            output=FrameVerdict,
            max_tokens=4000,
        )


ARTIFACTS = [
    ("extra fingers on the left hand", "both hands fully visible, five fingers each"),
    ("garbled lettering on a shop sign", "no signage or lettering anywhere in frame"),
    ("main subject sits in the caption zone", "subject framed in the upper two-thirds of the frame"),
    ("face looks melted in the background crowd", "background figures as distant silhouettes, faces not visible"),
]


class SimulatedFrameJudge:
    """Offline stand-in that behaves like the real judge (about 1 in 4 first attempts rejected), clearly labelled."""

    simulated = True

    def __init__(self, seed: str = "", model: str = "claude-opus-5-5"):
        self.seed, self.model = seed, model
        self._seen: dict[str, int] = {}

    def _call(self) -> LLMCall:
        # Typical usage of one real check (downscaled frame + brief), so simulated budgets are realistic.
        return LLMCall(purpose="frame-judge", model=self.model, input_tokens=1600, output_tokens=500,
                       served_by="simulated", simulated=True)

    def judge(self, image: Path, scene: Scene, refs: dict[str, Path]) -> tuple[FrameVerdict, LLMCall]:
        import hashlib

        key = f"{self.seed}|{image.parent.name}|{scene.visual}"  # retries share a folder; bake-off models don't
        attempt = self._seen.get(key, 0)
        self._seen[key] = attempt + 1
        h = int(hashlib.sha1(key.encode()).hexdigest()[:6], 16)
        if attempt == 0 and h % 4 == 0:
            artifact, fix = ARTIFACTS[(h // 4) % len(ARTIFACTS)]
            return FrameVerdict(matches_shot=True, characters_match=True, artifacts=[artifact], unsafe=False, score=2, fix=fix), self._call()
        return FrameVerdict(matches_shot=True, characters_match=True, artifacts=[], unsafe=False, score=3 + h % 2, fix=""), self._call()

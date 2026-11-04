"""Alternative hooks for creative testing.

The most expensive parts of an episode (stills, clips, narration) don't
change between ad variants; only the first two seconds of on-screen text and
the aspect ratio do. So one cheap LLM call plus re-renders turns one episode
into a grid of test creatives at almost no marginal media cost.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..llm import Claude
from ..models import EpisodeScript, LLMCall

Angle = Literal["curiosity", "threat", "stakes", "direct_question", "cold_open", "social_proof"]


class HookVariant(BaseModel):
    angle: Angle
    hook: str = Field(description="Max 8 words. Rendered in caps over the first shot.")


class HookSet(BaseModel):
    variants: list[HookVariant]


SYSTEM = """You write the on-screen hook text for the first two seconds of short vertical videos. Many viewers will be cold: they've never seen the series and don't know the characters.

Rules:
- 8 words maximum. It must read in under two seconds.
- Every hook must be true to this episode, and the episode's first ten seconds must pay it off. Bait the story doesn't deliver wins the click, then loses the viewer and trains the platform to show the video less.
- Use a different angle for each variant: curiosity (an open question), threat (immediate danger), stakes (what could be lost), direct_question (put the viewer in the decision), cold_open (drop into the action), social_proof (what other viewers chose or argued about, only if real).
- Don't use character names; cold viewers don't know them. Say "your grandmother", not "Lola Cora".
- No emojis, no hashtags, no quotation marks."""


class HookWriter(Protocol):
    def write(self, script: EpisodeScript, n: int, performance: str = "") -> tuple[list[HookVariant], LLMCall | None]: ...


class ClaudeHookWriter:
    def __init__(self, claude: Claude, model: str, effort: str):
        self.claude, self.model, self.effort = claude, model, effort

    def write(self, script: EpisodeScript, n: int, performance: str = "") -> tuple[list[HookVariant], LLMCall]:
        first = " ".join(s.narration for s in script.scenes[:2])
        perf = f"\n\n<performance_data>\n{performance}\n</performance_data>" if performance else ""
        user = (
            f"Episode title: {script.title}\nCurrent hook: {script.hook}\nWhat happens in the first ten seconds: {first}\n"
            f"The dilemma at the end: {script.question}{perf}\n\nWrite {n} alternative hooks, each with a different angle."
        )
        res, call = self.claude.structured(
            purpose="hooks", model=self.model, effort=self.effort, system=SYSTEM, user=user, output=HookSet, max_tokens=4000
        )
        return res.variants[:n], call


class MockHookWriter:
    def write(self, script: EpisodeScript, n: int, performance: str = "") -> tuple[list[HookVariant], LLMCall | None]:
        q = script.question.rstrip("?").split(",")[0]
        pool = [
            HookVariant(angle="direct_question", hook=(q[:40] + "?") if len(q) > 3 else "What would you do?"),
            HookVariant(angle="threat", hook="One sound and they all wake up."),
            HookVariant(angle="stakes", hook="You can only save one of them."),
            HookVariant(angle="cold_open", hook="Day one. The city stopped moving."),
            HookVariant(angle="curiosity", hook="Why is nobody blinking?"),
        ]
        return pool[:n], None

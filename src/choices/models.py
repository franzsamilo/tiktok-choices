"""Domain models shared by every stage of the pipeline.

Two families live here:

* ``EpisodeScript`` and friends are what the writer model produces. They are
  passed to Claude as a structured-output schema, so they deliberately avoid
  validators/constraints the JSON-schema transform cannot express. Quality
  rules (word counts, label lengths, ...) are enforced afterwards by
  ``story.critic`` where a failure can be explained and fed back.
* ``EpisodeRecord`` / ``SeriesState`` are our own bookkeeping and are free to
  use defaults, enums and timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ChoiceKey = Literal["A", "B"]
Platform = Literal["tiktok", "instagram", "file", "simulated"]


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# What the writer produces
# --------------------------------------------------------------------------- #


class Scene(BaseModel):
    narration: str = Field(description="Voice-over for this shot. Second person, present tense.")
    visual: str = Field(
        description=(
            "What the camera sees, as a concrete image-generation description: subject, "
            "setting, lighting, framing. Refer to recurring characters by name only; "
            "their locked appearance is injected by the pipeline."
        )
    )
    motion: str = Field(description="One short camera/subject motion cue for image-to-video, e.g. 'slow push-in'.")
    characters: list[str] = Field(description="Names of recurring characters visible in this shot (may be empty).")


class ChoiceOption(BaseModel):
    label: str = Field(description="1-4 words, ALL CAPS friendly, shown on the choice card. e.g. 'TAKE THE BIKE'.")
    keywords: list[str] = Field(
        description="3-6 lowercase words a viewer might type to mean this option, e.g. ['bike', 'ride', 'motor']."
    )
    stakes: str = Field(description="Private note for the writer: what this option risks and what it could gain.")


class Companion(BaseModel):
    name: str
    status: Literal["with_you", "separated", "missing", "infected", "dead"]
    note: str = Field(description="One line on their current condition or mood.")


class CanonUpdate(BaseModel):
    """The state of the world at the END of this episode, before the audience votes."""

    location: str
    inventory: list[str] = Field(description="The full inventory after this episode (not a diff).")
    companions: list[Companion] = Field(description="Every recurring companion and their status after this episode.")
    new_facts: list[str] = Field(description="New facts established this episode that later episodes must respect.")
    open_threads: list[str] = Field(description="Unresolved mysteries/threads still open after this episode.")


class EpisodeScript(BaseModel):
    title: str = Field(description="Short episode title, max 6 words.")
    hook: str = Field(description="On-screen text for the first 2 seconds. Max 8 words. Must stop the scroll.")
    scenes: list[Scene]
    question: str = Field(description="The dilemma posed to the audience, as one spoken sentence ending in '?'.")
    vote_style: Literal["ab", "yes_no"] = Field(
        description="'ab' for two named options; 'yes_no' when option A is YES and option B is NO."
    )
    choice_a: ChoiceOption
    choice_b: ChoiceOption
    canon: CanonUpdate
    caption: str = Field(description="Platform caption, max 150 characters, ends with a call to comment A or B.")
    hashtags: list[str] = Field(description="3-6 hashtags without the # sign.")

    def choice(self, key: ChoiceKey) -> ChoiceOption:
        return self.choice_a if key == "A" else self.choice_b

    def full_narration(self) -> str:
        return " ".join(s.narration.strip() for s in self.scenes)


# --------------------------------------------------------------------------- #
# Quality review
# --------------------------------------------------------------------------- #


class Issue(BaseModel):
    severity: Literal["blocker", "warning"]
    code: str
    message: str


class CriticVerdict(BaseModel):
    """Structured output of the LLM critic."""

    continuity_errors: list[str] = Field(description="Contradictions with canon. Empty if none.")
    dilemma_is_balanced: bool = Field(description="True if neither option is obviously correct.")
    hook_strength: int = Field(description="1-5, how likely the first 2 seconds stop a scrolling viewer.")
    notes: list[str] = Field(description="Concrete, actionable fixes for the writer. Empty if none.")


class QualityReport(BaseModel):
    passed: bool
    issues: list[Issue] = []
    critic: CriticVerdict | None = None
    attempts: int = 1


# --------------------------------------------------------------------------- #
# Social + votes
# --------------------------------------------------------------------------- #


class Comment(BaseModel):
    platform: Platform
    id: str
    post_id: str
    author_id: str
    author_name: str = ""
    text: str
    created_at: datetime
    likes: int = 0
    parent_id: str | None = None
    is_owner: bool = False

    @property
    def key(self) -> str:
        """Globally unique id (comment ids are only unique per platform)."""
        return f"{self.platform}:{self.id}"


class VoteMethod(StrEnum):
    RULE = "rule"
    LLM = "llm"
    NONE = "none"


class ParsedVote(BaseModel):
    choice: ChoiceKey | None
    method: VoteMethod
    reason: str
    confidence: float = 0.0


class PlatformCount(BaseModel):
    a: float = 0.0
    b: float = 0.0
    voters: int = 0


class TallyResult(BaseModel):
    winner: ChoiceKey
    decided_by: Literal["votes", "tiebreak", "low_turnout"]
    weighted_a: float
    weighted_b: float
    unique_voters: int
    comments_seen: int
    comments_excluded: int
    ambiguous: int
    llm_classified: int
    by_platform: dict[str, PlatformCount]
    top_comments: dict[str, list[str]] = Field(default_factory=dict, description="Most-liked comment texts per side.")
    closed_at: datetime

    @property
    def share_a(self) -> float:
        total = self.weighted_a + self.weighted_b
        return self.weighted_a / total if total else 0.5


class Outcome(BaseModel):
    """What the audience decided at the end of the previous episode."""

    day: int
    question: str
    chosen_key: ChoiceKey
    chosen_label: str
    rejected_label: str
    stakes: str
    decided_by: Literal["votes", "tiebreak", "low_turnout"]
    share: float = Field(description="Share of weighted votes the winning option got (0-1).")
    unique_voters: int
    crowd_quotes: list[str] = []


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


class EpisodeStatus(StrEnum):
    SCRIPTED = "scripted"
    MEDIA_READY = "media_ready"
    RENDERED = "rendered"
    AWAITING_REVIEW = "awaiting_review"
    PUBLISHED = "published"
    RESOLVED = "resolved"


STATUS_ORDER = list(EpisodeStatus)


class PostRef(BaseModel):
    platform: Platform
    post_id: str
    url: str = ""
    published_at: datetime = Field(default_factory=utcnow)


class LLMCall(BaseModel):
    purpose: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    served_by: str = ""


class SceneTiming(BaseModel):
    index: int
    start: float
    duration: float


class EpisodeRecord(BaseModel):
    day: int
    status: EpisodeStatus
    script: EpisodeScript
    outcome: Outcome | None = None
    quality: QualityReport
    reviewer_notes: list[str] = []
    posts: list[PostRef] = []
    voting_closes_at: datetime | None = None
    tally: TallyResult | None = None
    timings: list[SceneTiming] = []
    llm_calls: list[LLMCall] = []
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def at_least(self, status: EpisodeStatus) -> bool:
        return STATUS_ORDER.index(self.status) >= STATUS_ORDER.index(status)


class EpisodeSummary(BaseModel):
    day: int
    title: str
    summary: str
    question: str
    chosen: str | None = None
    decided_by: str | None = None


class SeriesState(BaseModel):
    series_id: str
    day: int = 0
    location: str = ""
    inventory: list[str] = []
    companions: list[Companion] = []
    facts: list[str] = []
    open_threads: list[str] = []
    history: list[EpisodeSummary] = []

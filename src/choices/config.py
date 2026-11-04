"""Typed configuration loaded from YAML.

``config/series.yaml`` is the creative brief (the "story bible"); it is owned by
the creator. ``config/pipeline.yaml`` is the plumbing (providers, models,
voting rules); it is owned by the engineer. Keeping them apart means a creator
can rewrite the premise without touching anything that can break a run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Story bible
# --------------------------------------------------------------------------- #


class CharacterSheet(BaseModel):
    name: str
    role: str
    look: str = Field(description="Locked visual descriptor injected verbatim into every image prompt.")
    voice: str = ""
    starts_with_you: bool = True


class SeriesBible(BaseModel):
    series_id: str
    title: str
    logline: str
    premise: str
    point_of_view: str
    setting: str
    tone: str
    world_rules: list[str]
    content_rules: list[str]
    format_rules: list[str]
    characters: list[CharacterSheet]
    starting_inventory: list[str]
    starting_location: str
    visual_style: str
    negative_prompt: str
    hashtags: list[str] = []
    language: str = "English, with natural Taglish where a character would really say it"

    def character(self, name: str) -> CharacterSheet | None:
        key = name.strip().lower()
        for c in self.characters:
            if c.name.lower() == key or c.name.split()[0].lower() == key:
                return c
        return None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class ProviderConfig(BaseModel):
    writer: Literal["claude", "mock"] = "claude"
    critic: Literal["claude", "rules"] = "claude"
    vote_classifier: Literal["claude", "none"] = "claude"
    image: Literal["fal", "mock"] = "mock"
    video: Literal["fal", "mock", "none"] = "none"
    voice: Literal["elevenlabs", "mock"] = "mock"
    frame_judge: Literal["claude", "mock", "none"] = "claude"
    hooks: Literal["claude", "mock"] = "claude"


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelConfig(BaseModel):
    writer: str = "claude-opus-5-5"
    writer_effort: Effort = "high"
    critic: str = "claude-opus-5-5"
    critic_effort: Effort = "medium"
    vote_classifier: str = "claude-opus-5-5"
    vote_classifier_effort: Effort = "low"
    frame_judge: str = "claude-opus-5-5"
    frame_judge_effort: Effort = "low"
    hooks: str = "claude-opus-5-5"
    hooks_effort: Effort = "low"
    fal_image: str = "fal-ai/nano-banana-2"  # text-to-image (shots with no recurring character)
    fal_image_edit: str = "fal-ai/nano-banana-2/edit"  # multi-reference (shots with cast members)
    fal_video: str = "fal-ai/kling-video/v3/pro/image-to-video"
    fal_video_audio: bool = False  # we narrate separately; native audio costs ~50% more per second
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_id: str = ""


class LLMPrice(BaseModel):
    input: float  # USD per million tokens
    output: float
    cache_read: float
    cache_write: float


class Pricing(BaseModel):
    """Unit prices used for the cost ledger, budget guard and `choices estimate`.

    Prices change. ``checked`` records when these were last verified; update
    them from each provider's pricing page.
    """

    checked: str = "unverified"
    llm: dict[str, LLMPrice] = Field(default_factory=dict)
    image: dict[str, float] = Field(default_factory=dict)  # USD per image
    video: dict[str, float] = Field(default_factory=dict)  # USD per second
    video_audio_multiplier: float = 1.5
    voice: dict[str, float] = Field(default_factory=dict)  # USD per 1,000 characters


class BudgetConfig(BaseModel):
    max_usd_per_episode: float = 4.0
    on_exceed: Literal["degrade", "stop"] = "degrade"  # degrade = skip animation / rerolls, keep the episode


class VideoPolicy(BaseModel):
    animate: Literal["hero", "all", "none"] = "hero"
    max_clips: int = 2  # hero shots per episode; the rest get Ken Burns motion
    min_seconds: int = 3
    max_seconds: int = 15


class QAConfig(BaseModel):
    enabled: bool = True
    min_score: int = 3  # 1-5; frames below this are re-rolled
    max_rerolls: int = 2


class MetricsConfig(BaseModel):
    track_days: int = 7  # keep refreshing a post's analytics this long after publishing
    refresh_hours: float = 6
    learn_from_last: int = 20  # episodes considered for the "what worked" digest
    min_episodes_to_learn: int = 2


class NotifyConfig(BaseModel):
    webhook_url_env: str = "REVIEW_WEBHOOK_URL"  # Slack or Discord incoming webhook


class WritingConfig(BaseModel):
    target_seconds: int = 50
    words_per_second: float = 2.6
    min_scenes: int = 4
    max_scenes: int = 7
    max_revisions: int = 2
    history_episodes_in_full: int = 3


class RenderConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    hook_seconds: float = 2.4
    choice_card_seconds: float = 4.5
    scene_padding_seconds: float = 0.35
    min_scene_seconds: float = 3.0
    music_path: str = ""
    music_volume_db: float = -22.0
    target_lufs: float = -14.0
    caption_words_per_chunk: int = 3


class VotingConfig(BaseModel):
    window_hours: float = 24
    min_votes: int = 5
    tie_margin: float = 0.02
    tie_break: Literal["chaos", "A", "B"] = "chaos"
    keep: Literal["latest", "first"] = "latest"
    like_weight: float = 0.0
    like_cap: int = 50
    platform_weights: dict[str, float] = Field(default_factory=lambda: {"tiktok": 1.0, "instagram": 1.0})
    max_llm_comments: int = 600
    llm_batch_size: int = 120
    quote_crowd: bool = True


class TikTokConfig(BaseModel):
    enabled: bool = False
    api_base: str = "https://business-api.tiktok.com/open_api/v1.3"
    business_id_env: str = "TIKTOK_BUSINESS_ID"
    access_token_env: str = "TIKTOK_ACCESS_TOKEN"


class InstagramConfig(BaseModel):
    enabled: bool = False
    graph_base: str = "https://graph.facebook.com/v21.0"
    user_id_env: str = "IG_USER_ID"
    access_token_env: str = "IG_ACCESS_TOKEN"


class PublishConfig(BaseModel):
    mode: Literal["export", "api"] = "export"
    require_review: bool = True
    public_base_url_env: str = "PUBLIC_MEDIA_BASE_URL"
    disclose_ai: bool = True


class PipelineConfig(BaseModel):
    series_file: str = "config/series.yaml"
    data_dir: str = "runs"
    providers: ProviderConfig = ProviderConfig()
    models: ModelConfig = ModelConfig()
    writing: WritingConfig = WritingConfig()
    render: RenderConfig = RenderConfig()
    voting: VotingConfig = VotingConfig()
    tiktok: TikTokConfig = TikTokConfig()
    instagram: InstagramConfig = InstagramConfig()
    publish: PublishConfig = PublishConfig()
    pricing: Pricing = Pricing()
    budget: BudgetConfig = BudgetConfig()
    video: VideoPolicy = VideoPolicy()
    qa: QAConfig = QAConfig()
    metrics: MetricsConfig = MetricsConfig()
    notify: NotifyConfig = NotifyConfig()
    # Offline runs price the mock calls as if they were the configured real
    # models, so dashboards and budgets behave like production. Always labelled.
    simulate_costs: bool = False

    def offline(self) -> PipelineConfig:
        """A copy with every paid/networked provider swapped for its local stand-in."""
        cfg = self.model_copy(deep=True)
        cfg.providers = ProviderConfig(
            writer="mock", critic="rules", vote_classifier="none", image="mock",
            video="mock" if self.providers.video != "none" else "none", voice="mock",
            frame_judge="mock" if self.providers.frame_judge != "none" else "none", hooks="mock",
        )
        cfg.simulate_costs = True
        cfg.tiktok.enabled = False
        cfg.instagram.enabled = False
        cfg.publish.mode = "export"
        return cfg


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_pipeline(path: str | Path = "config/pipeline.yaml") -> PipelineConfig:
    p = Path(path)
    if not p.exists():
        return PipelineConfig()
    return PipelineConfig.model_validate(_read_yaml(p))


def load_bible(path: str | Path) -> SeriesBible:
    return SeriesBible.model_validate(_read_yaml(Path(path)))

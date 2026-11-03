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
    video: Literal["fal", "none"] = "none"
    voice: Literal["elevenlabs", "mock"] = "mock"


class ModelConfig(BaseModel):
    writer: str = "claude-opus-5-5"
    writer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    critic: str = "claude-opus-5-5"
    critic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    vote_classifier: str = "claude-opus-5-5"
    vote_classifier_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    fal_image: str = "fal-ai/flux/dev"
    fal_video: str = "fal-ai/kling-video/v2.1/standard/image-to-video"
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_id: str = ""


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

    def offline(self) -> PipelineConfig:
        """A copy with every paid/networked provider swapped for its local stand-in."""
        cfg = self.model_copy(deep=True)
        cfg.providers = ProviderConfig(
            writer="mock", critic="rules", vote_classifier="none", image="mock", video="none", voice="mock"
        )
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

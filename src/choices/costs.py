"""What each episode costs, what it would cost under other settings, and the budget guard.

Media spend is recorded as it happens (``CostEntry``). LLM spend is derived
from the token usage already stored on every call, so nothing is counted
twice. The ledger is shared by the parallel media workers, so spending checks
are atomic: two scenes can't both decide there's room for the last clip.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .config import PipelineConfig, Pricing
from .models import CostEntry, EpisodeRecord, LLMCall

log = logging.getLogger(__name__)

FREE_PROVIDERS = {"mock", "none"}


def llm_cost(call: LLMCall, pricing: Pricing) -> float:
    p = pricing.llm.get(call.model)
    if p is None:
        return 0.0
    return (
        call.input_tokens * p.input
        + call.output_tokens * p.output
        + call.cache_read_tokens * p.cache_read
        + call.cache_write_tokens * p.cache_write
    ) / 1_000_000


def image_cost(model: str, pricing: Pricing) -> float:
    return pricing.image.get(model, 0.0)


def video_cost(model: str, seconds: float, pricing: Pricing, audio: bool = False) -> float:
    rate = pricing.video.get(model, 0.0)
    return rate * seconds * (pricing.video_audio_multiplier if audio else 1.0)


def voice_cost(model: str, chars: int, pricing: Pricing) -> float:
    return pricing.voice.get(model, 0.0) * chars / 1000


def breakdown(ep: EpisodeRecord, pricing: Pricing) -> dict[str, float]:
    """USD per stage, LLM calls included."""
    out: dict[str, float] = {}
    for c in ep.costs:
        out[c.stage] = out.get(c.stage, 0.0) + c.usd
    for call in ep.llm_calls:
        stage = call.purpose.replace("-revision", "")
        out[stage] = out.get(stage, 0.0) + llm_cost(call, pricing)
    return {k: round(v, 4) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def total(ep: EpisodeRecord, pricing: Pricing) -> float:
    return round(sum(breakdown(ep, pricing).values()), 4)


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Records spend on an episode and answers "can we afford this?" atomically."""

    def __init__(self, ep: EpisodeRecord, cfg: PipelineConfig):
        self.ep, self.pricing, self.cap = ep, cfg.pricing, cfg.budget.max_usd_per_episode
        self.on_exceed = cfg.budget.on_exceed
        self._lock = threading.Lock()
        self._reserved = 0.0

    def spent(self) -> float:
        return total(self.ep, self.pricing)

    def reserve(self, usd: float, what: str) -> bool:
        """Claim budget for a planned call. False (or an exception, if on_exceed=stop) when it doesn't fit."""
        with self._lock:
            if usd <= 0:
                return True
            if self.spent() + self._reserved + usd <= self.cap:
                self._reserved += usd
                return True
            msg = (
                f"budget: skipped {what} (${usd:.2f}); ${self.spent() + self._reserved:.2f} of "
                f"${self.cap:.2f} already committed"
            )
            self.ep.decisions.append(msg)
            log.warning("day %d: %s", self.ep.day, msg)
            if self.on_exceed == "stop":
                raise BudgetExceeded(msg)
            return False

    def record(self, entry: CostEntry, reserved: float = 0.0) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved)
            self.ep.costs.append(entry)

    def record_call(self, call: LLMCall, reserved: float = 0.0) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved)
            self.ep.llm_calls.append(call)


# --------------------------------------------------------------------------- #
# Estimates: what would an episode cost under each setting?
# --------------------------------------------------------------------------- #


@dataclass
class Estimate:
    name: str
    per_episode: float
    lines: dict[str, float]

    @property
    def per_month(self) -> float:
        return self.per_episode * 30


def estimate(cfg: PipelineConfig, *, scenes: int = 5, reroll_rate: float = 0.3) -> list[Estimate]:
    """Projected cost per episode for the main configurations, from the pricing table.

    Assumptions are deliberately simple and stated in the output: narration at
    the target length, `scenes` shots of equal length, a share of frames
    re-rolled by the QA gate, and typical token counts per LLM call.
    """
    p, m, w = cfg.pricing, cfg.models, cfg.writing
    words = w.target_seconds * w.words_per_second
    chars = int(words * 5.8) + 90  # narration + spoken question/CTA
    shot_seconds = max(cfg.video.min_seconds, min(cfg.video.max_seconds, round(w.target_seconds / scenes)))

    def llm(model: str, inp: int, out: int, cached: int = 0) -> float:
        return llm_cost(LLMCall(purpose="", model=model, input_tokens=inp, output_tokens=out, cache_read_tokens=cached), p)

    base = {
        "writer + critic": llm(m.writer, 4000, 6000, 3000) + llm(m.critic, 5000, 1500),
        "frame QA": scenes * (1 + reroll_rate) * llm(m.frame_judge, 1600, 500),
        "images": scenes * (1 + reroll_rate) * max(image_cost(m.fal_image, p), image_cost(m.fal_image_edit, p)),
        "voice": voice_cost(m.elevenlabs_model, chars, p),
        "vote reading": llm(m.vote_classifier, 6000, 1500),
    }

    def with_video(name: str, clips: int, audio: bool) -> Estimate:
        lines = dict(base)
        if clips:
            lines["video"] = clips * video_cost(m.fal_video, shot_seconds, p, audio)
        return Estimate(name, round(sum(lines.values()), 3), {k: round(v, 3) for k, v in lines.items()})

    hero = min(cfg.video.max_clips, scenes)
    return [
        with_video("Stills + Ken Burns motion", 0, False),
        with_video(f"Hero shots animated ({hero} of {scenes}), native audio off  [default]", hero, False),
        with_video(f"Every shot animated ({scenes}), native audio off", scenes, False),
        with_video(f"Every shot animated ({scenes}), native audio on", scenes, True),
    ]

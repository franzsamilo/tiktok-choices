"""The daily loop as a resumable state machine.

    write ──► media ──► render ──► review ──► publish ──► (voting window) ──► tally ──► write next day
    SCRIPTED  MEDIA_READY RENDERED  AWAITING_REVIEW PUBLISHED                    RESOLVED

``tick(now)`` advances the newest episode as far as it can go and stops at
the two places that need the outside world: a human approving the render, and
the voting window closing. Run it from cron every hour; it's idempotent. Every
artifact is cached on disk, so a crash or a provider timeout resumes from the
step that failed instead of regenerating (and re-paying for) the whole episode.
"""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import PipelineConfig, SeriesBible, load_bible
from .llm import Claude
from .media.base import ImageProvider, VideoProvider, VoiceProvider, VoiceResult
from .media.mock import MockImages, MockVoice, stable_seed
from .media.prompting import image_prompt, motion_prompt
from .models import (
    Comment,
    EpisodeRecord,
    EpisodeStatus,
    Outcome,
    PostRef,
    SeriesState,
    TallyResult,
    utcnow,
)
from .render.compose import SceneInput, render_episode
from .social.base import CommentSource, Publisher, SourceError
from .social.export import caption_text, export_bundle
from .social.files import load_comments
from .story.canon import apply_episode, initial_state, outcome_from_tally, record_decision
from .story.critic import LLMCritic
from .story.mock_writer import MockWriter
from .story.writer import ClaudeWriter, Writer, draft_episode
from .votes.classifier import LLMVoteClassifier
from .votes.count import count_votes

log = logging.getLogger(__name__)
S = EpisodeStatus


@dataclass
class TickResult:
    day: int
    status: EpisodeStatus
    message: str


class Pipeline:
    def __init__(
        self,
        cfg: PipelineConfig,
        *,
        bible: SeriesBible | None = None,
        claude: Claude | None = None,
        writer: Writer | None = None,
        images: ImageProvider | None = None,
        video: VideoProvider | None = None,
        voice: VoiceProvider | None = None,
        sources: dict[str, CommentSource] | None = None,
        publishers: dict[str, Publisher] | None = None,
    ):
        from .store import Store

        self.cfg = cfg
        self.bible = bible or load_bible(cfg.series_file)
        self.store = Store(cfg.data_dir, self.bible.series_id)
        self.claude = claude or Claude()
        p, m = cfg.providers, cfg.models

        self.writer: Writer = writer or (
            ClaudeWriter(self.claude, self.bible, cfg.writing, m.writer, m.writer_effort) if p.writer == "claude" else MockWriter()
        )
        self.critic = LLMCritic(self.claude, self.bible, m.critic, m.critic_effort) if p.critic == "claude" else None
        self.classifier = (
            LLMVoteClassifier(self.claude, m.vote_classifier, m.vote_classifier_effort, cfg.voting.llm_batch_size)
            if p.vote_classifier == "claude"
            else None
        )

        if images is not None:
            self.images = images
        elif p.image == "fal":
            from .media.fal import FalImages

            self.images = FalImages(m.fal_image)
        else:
            self.images = MockImages()

        if video is not None:
            self.video: VideoProvider | None = video
        elif p.video == "fal":
            from .media.fal import FalVideo

            self.video = FalVideo(m.fal_video, negative=self.bible.negative_prompt)
        else:
            self.video = None

        if voice is not None:
            self.voice = voice
        elif p.voice == "elevenlabs":
            from .media.elevenlabs import ElevenLabsVoice

            self.voice = ElevenLabsVoice(m.elevenlabs_voice_id, m.elevenlabs_model)
        else:
            self.voice = MockVoice(cfg.writing.words_per_second)

        self._sources = sources
        self._publishers = publishers

    # ------------------------------------------------------------------ state
    def state(self) -> SeriesState:
        return self.store.load_state() or initial_state(self.bible)

    def recent(self, before_day: int) -> list[EpisodeRecord]:
        n = self.cfg.writing.history_episodes_in_full
        days = [d for d in self.store.episode_days() if d < before_day][-n:]
        return [e for d in days if (e := self.store.load_episode(d))]

    # ------------------------------------------------------------------ stages
    def write(self, day: int, outcome: Outcome | None, notes: list[str] | None = None, previous=None) -> EpisodeRecord:
        log.info("day %d: writing", day)
        res = draft_episode(
            writer=self.writer,
            critic=self.critic,
            bible=self.bible,
            cfg=self.cfg.writing,
            state=self.state(),
            recent=self.recent(day),
            day=day,
            outcome=outcome,
            reviewer_notes=notes,
            previous_draft=previous,
        )
        ep = EpisodeRecord(day=day, status=S.SCRIPTED, script=res.script, outcome=outcome, quality=res.quality, llm_calls=res.calls)
        if notes:
            ep.reviewer_notes = list(notes)
        self.store.save_episode(ep)
        return ep

    def _voice_for(self, text: str, out: Path) -> VoiceResult:
        sidecar = out.with_suffix(".json")
        cached = VoiceResult.load(sidecar)
        if cached and Path(cached.path).exists():
            return cached
        res = self.voice.synthesize(text, out)
        res.save(sidecar)
        return res

    def produce_media(self, ep: EpisodeRecord) -> EpisodeRecord:
        log.info("day %d: producing media for %d scenes", ep.day, len(ep.script.scenes))
        r = self.cfg.render

        def scene_job(i: int) -> None:
            scene = ep.script.scenes[i]
            v = self._voice_for(scene.narration, self.store.scene_audio(ep.day, i))
            img = self.store.scene_image(ep.day, i)
            if not img.exists():
                prompt, negative = image_prompt(scene, self.bible)
                self.images.generate(prompt, negative, img, seed=stable_seed(self.bible.series_id, ep.day, i), width=r.width, height=r.height)
            if self.video is not None:
                clip = self.store.scene_clip(ep.day, i)
                if not clip.exists():
                    self.video.animate(img, motion_prompt(scene), clip, seconds=v.duration + r.scene_padding_seconds)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(scene_job, range(len(ep.script.scenes))))  # list() re-raises worker errors

        self._voice_for(self._cta_line(ep), self.store.cta_audio(ep.day))
        self.store.write_json(ep.day, "prompts.json", [
            {"scene": i + 1, "image": image_prompt(s, self.bible)[0], "motion": motion_prompt(s)}
            for i, s in enumerate(ep.script.scenes)
        ])
        ep.status = S.MEDIA_READY
        self.store.save_episode(ep)
        return ep

    @staticmethod
    def _cta_line(ep: EpisodeRecord) -> str:
        tail = "Comment yes, or no." if ep.script.vote_style == "yes_no" else "Comment A, or B."
        return f"{ep.script.question} {tail}"

    def render(self, ep: EpisodeRecord) -> EpisodeRecord:
        log.info("day %d: rendering", ep.day)
        scenes = []
        for i in range(len(ep.script.scenes)):
            v = VoiceResult.load(self.store.scene_audio(ep.day, i).with_suffix(".json"))
            if v is None:
                raise RuntimeError(f"day {ep.day} scene {i + 1}: narration missing; re-run media")
            clip = self.store.scene_clip(ep.day, i)
            scenes.append(SceneInput(image=self.store.scene_image(ep.day, i), clip=clip if clip.exists() else None, voice=v))
        cta = VoiceResult.load(self.store.cta_audio(ep.day).with_suffix(".json"))
        assert cta is not None
        res = render_episode(
            script=ep.script,
            day=ep.day,
            series_title=self.bible.title,
            scenes=scenes,
            cta_voice=cta,
            cfg=self.cfg.render,
            out_dir=self.store.day_dir(ep.day),
            window_hours=self.cfg.voting.window_hours,
        )
        ep.timings = res.timings
        ep.status = S.RENDERED
        self.store.save_episode(ep)
        return ep

    def submit_for_review(self, ep: EpisodeRecord, now: datetime) -> EpisodeRecord:
        from .review import write_review_page

        ep.status = S.AWAITING_REVIEW
        self.store.save_episode(ep)
        write_review_page(self, ep)
        auto = not self.cfg.publish.require_review and ep.quality.passed
        if auto:
            return self.approve(ep.day, now)
        return ep

    # ------------------------------------------------------------------ human gate
    def approve(self, day: int, now: datetime | None = None) -> EpisodeRecord:
        ep = self._require(day, S.AWAITING_REVIEW)
        now = now or utcnow()
        ep.posts = self._publish(ep)
        ep.status = S.PUBLISHED
        ep.voting_closes_at = now + timedelta(hours=self.cfg.voting.window_hours)
        self.store.save_episode(ep)
        self.store.save_state(apply_episode(self.state(), ep))
        self._refresh_pages(ep)
        log.info("day %d: published; voting closes %s", day, ep.voting_closes_at.isoformat())
        return ep

    def reject(self, day: int, notes: list[str], now: datetime | None = None) -> EpisodeRecord:
        """Send an episode back to the writer with human notes, then rebuild it."""
        ep = self._require(day, S.RENDERED, S.AWAITING_REVIEW)
        for sub in ("scenes", "audio", "render", "post"):
            shutil.rmtree(self.store.day_dir(day) / sub, ignore_errors=True)
        new = self.write(day, ep.outcome, notes=notes, previous=ep.script)
        new.llm_calls = ep.llm_calls + new.llm_calls
        self.store.save_episode(new)
        return self._produce(new, now or utcnow())

    def reroll(self, day: int, scene: int, now: datetime | None = None) -> EpisodeRecord:
        """Regenerate one scene's picture (the most common fix: one bad frame) and re-render."""
        ep = self._require(day, S.RENDERED, S.AWAITING_REVIEW)
        i = scene - 1
        if not 0 <= i < len(ep.script.scenes):
            raise ValueError(f"day {day} has scenes 1-{len(ep.script.scenes)}")
        img, clip = self.store.scene_image(day, i), self.store.scene_clip(day, i)
        if img.exists():
            img.rename(img.with_name(f"{img.stem}.rejected-{utcnow():%H%M%S}{img.suffix}"))
        clip.unlink(missing_ok=True)
        prompt, negative = image_prompt(ep.script.scenes[i], self.bible)
        r = self.cfg.render
        self.images.generate(prompt, negative, img, seed=stable_seed(self.bible.series_id, day, i, utcnow().isoformat()), width=r.width, height=r.height)
        ep.status = S.MEDIA_READY
        self.store.save_episode(ep)
        return self._produce(ep, now or utcnow())

    def link(self, day: int, platform: str, post_id: str, url: str = "") -> EpisodeRecord:
        ep = self._require(day, S.PUBLISHED, S.RESOLVED)
        ep.posts = [p for p in ep.posts if p.platform != platform]
        ep.posts.append(PostRef(platform=platform, post_id=post_id, url=url))  # type: ignore[arg-type]
        self.store.save_episode(ep)
        return ep

    # ------------------------------------------------------------------ publish + votes
    def publishers(self) -> dict[str, Publisher]:
        if self._publishers is not None:
            return self._publishers
        out: dict[str, Publisher] = {}
        if self.cfg.publish.mode == "api":
            if self.cfg.tiktok.enabled:
                from .social.tiktok import TikTokBusiness

                out["tiktok"] = TikTokBusiness(self.cfg.tiktok)
            if self.cfg.instagram.enabled:
                from .social.instagram import InstagramGraph

                out["instagram"] = InstagramGraph(self.cfg.instagram)
        return out

    def sources(self) -> dict[str, CommentSource]:
        if self._sources is not None:
            return self._sources
        out: dict[str, CommentSource] = {}
        if self.cfg.tiktok.enabled and os.environ.get(self.cfg.tiktok.access_token_env):
            from .social.tiktok import TikTokBusiness

            out["tiktok"] = TikTokBusiness(self.cfg.tiktok)
        if self.cfg.instagram.enabled and os.environ.get(self.cfg.instagram.access_token_env):
            from .social.instagram import InstagramGraph

            out["instagram"] = InstagramGraph(self.cfg.instagram)
        return out

    def _publish(self, ep: EpisodeRecord) -> list[PostRef]:
        day_dir = self.store.day_dir(ep.day)
        export_bundle(ep, day_dir, self.cfg.publish.disclose_ai)
        pubs = self.publishers()
        if not pubs:
            return list(ep.posts)
        base = os.environ.get(self.cfg.publish.public_base_url_env, "").rstrip("/")
        if not base:
            raise SourceError(
                f"publish.mode=api needs {self.cfg.publish.public_base_url_env}: a public URL where "
                f"{self.bible.series_id}/day-{ep.day:03d}/final.mp4 can be fetched by the platforms"
            )
        url = f"{base}/{self.bible.series_id}/day-{ep.day:03d}/final.mp4"
        caption = caption_text(ep, self.cfg.publish.disclose_ai)
        posts = list(ep.posts)
        for name, pub in pubs.items():
            try:
                posts.append(pub.publish(video_path=self.store.final_video(ep.day), video_url=url, caption=caption, day=ep.day))
            except SourceError as e:
                # One platform failing must not block the other. Post it by hand and `choices link` it.
                log.error("day %d: %s publish failed: %s", ep.day, name, e)
        return posts

    def collect_comments(self, ep: EpisodeRecord, extra_files: list[Path] | None = None) -> list[Comment]:
        comments: dict[str, Comment] = {c.key: c for c in self.store.load_comments(ep.day)}
        srcs = self.sources()
        for post in ep.posts:
            src = srcs.get(post.platform)
            if src is None:
                log.warning("day %d: no comment source configured for %s", ep.day, post.platform)
                continue
            for c in src.fetch(post.post_id):
                comments[c.key] = c
        inbox = self.store.day_dir(ep.day) / "inbox"
        files = list(extra_files or []) + (sorted(inbox.glob("*.csv")) + sorted(inbox.glob("*.json")) if inbox.exists() else [])
        inside_window = ep.voting_closes_at - timedelta(minutes=1) if ep.voting_closes_at else None
        for f in files:
            for c in load_comments(f, post_id=f"file:{Path(f).name}", default_time=inside_window):
                comments[c.key] = c
        out = sorted(comments.values(), key=lambda c: c.created_at)
        self.store.save_comments(ep.day, out)
        return out

    def tally(self, day: int, now: datetime | None = None, extra_files: list[Path] | None = None, force: bool = False) -> TallyResult:
        ep = self._require(day, S.PUBLISHED, S.RESOLVED)
        now = now or utcnow()
        if ep.voting_closes_at and now < ep.voting_closes_at and not force:
            raise RuntimeError(f"day {day}: voting is open until {ep.voting_closes_at:%Y-%m-%d %H:%M} UTC (use --force to tally early)")
        comments = self.collect_comments(ep, extra_files)
        cutoff = min(now, ep.voting_closes_at) if ep.voting_closes_at else now
        result, audit, calls = count_votes(
            ep.script, comments, self.cfg.voting, cutoff=cutoff, seed=f"{self.bible.series_id}:{day}", classifier=self.classifier
        )
        ep.tally = result
        ep.llm_calls += calls
        ep.status = S.RESOLVED
        self.store.save_episode(ep)
        self.store.write_json(day, "votes_audit.json", [
            {"platform": r.comment.platform, "author": r.comment.author_name or r.comment.author_id, "text": r.comment.text,
             "vote": r.vote.choice, "method": r.vote.method.value, "reason": r.vote.reason, "counted": r.counted, "note": r.note}
            for r in audit
        ])
        self.store.save_state(record_decision(self.state(), day, outcome_from_tally(ep, result)))
        self._refresh_pages(ep)
        log.info("day %d: %s wins (%s, %.0f vs %.0f, %d voters)", day, result.winner, result.decided_by,
                 result.weighted_a, result.weighted_b, result.unique_voters)
        return result

    # ------------------------------------------------------------------ the tick
    def _produce(self, ep: EpisodeRecord, now: datetime) -> EpisodeRecord:
        if ep.status == S.SCRIPTED:
            ep = self.produce_media(ep)
        if ep.status == S.MEDIA_READY:
            ep = self.render(ep)
        if ep.status == S.RENDERED:
            ep = self.submit_for_review(ep, now)
        return ep

    def tick(self, now: datetime | None = None) -> TickResult:
        now = now or utcnow()
        ep = self.store.latest_episode()

        if ep is None:
            ep = self._produce(self.write(1, None), now)
        elif ep.status in (S.SCRIPTED, S.MEDIA_READY, S.RENDERED):
            ep = self._produce(ep, now)
        elif ep.status == S.PUBLISHED:
            assert ep.voting_closes_at is not None
            if now < ep.voting_closes_at:
                left = ep.voting_closes_at - now
                return TickResult(ep.day, ep.status, f"day {ep.day}: voting open for another {left.total_seconds() / 3600:.1f}h")
            if not ep.posts and not (self.store.day_dir(ep.day) / "inbox").exists():
                return TickResult(ep.day, ep.status,
                    f"day {ep.day}: voting closed but no posts are linked. Run `choices link --day {ep.day} --tiktok <id>` "
                    f"or drop a comments export into {self.store.day_dir(ep.day) / 'inbox'}/")
            self.tally(ep.day, now)
            ep = self.store.load_episode(ep.day)
            assert ep is not None
        if ep.status == S.RESOLVED:
            assert ep.tally is not None
            ep = self._produce(self.write(ep.day + 1, outcome_from_tally(ep, ep.tally)), now)

        msgs = {
            S.AWAITING_REVIEW: f"day {ep.day}: ready for review at {self.store.day_dir(ep.day) / 'review.html'} "
                               f"(choices approve --day {ep.day} | choices reject --day {ep.day} --note '...')",
            S.PUBLISHED: f"day {ep.day}: published; voting closes {ep.voting_closes_at:%Y-%m-%d %H:%M} UTC"
            if ep.voting_closes_at else f"day {ep.day}: published",
        }
        return TickResult(ep.day, ep.status, msgs.get(ep.status, f"day {ep.day}: {ep.status.value}"))

    # ------------------------------------------------------------------ helpers
    def _require(self, day: int, *allowed: EpisodeStatus) -> EpisodeRecord:
        ep = self.store.load_episode(day)
        if ep is None:
            raise ValueError(f"no episode for day {day}")
        if allowed and ep.status not in allowed:
            raise ValueError(f"day {day} is {ep.status.value}; expected {' or '.join(a.value for a in allowed)}")
        return ep

    def _refresh_pages(self, ep: EpisodeRecord) -> None:
        from .review import write_index, write_review_page

        write_review_page(self, ep)
        write_index(self)

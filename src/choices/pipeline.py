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

import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .analytics import EpisodeKPI, episode_kpi, performance_digest
from .config import PipelineConfig, SeriesBible, VideoPolicy, load_bible
from .costs import FREE_PROVIDERS, Ledger, image_cost, total, video_cost, voice_cost
from .llm import Claude
from .media.base import ImageProvider, VideoProvider, VoiceProvider, VoiceResult
from .media.mock import MockImages, MockVideo, MockVoice, stable_seed
from .media.prompting import cast_prompt, image_prompt, motion_prompt
from .media.qa import ClaudeFrameJudge, FrameJudge, SimulatedFrameJudge, passes
from .models import (
    Comment,
    CostEntry,
    EpisodeRecord,
    EpisodeStatus,
    FrameQA,
    Outcome,
    PostRef,
    SeriesState,
    TallyResult,
    Variant,
    utcnow,
)
from .notify import notify
from .render.compose import SceneInput, render_episode
from .social.base import CommentSource, MetricsSource, Publisher, SourceError
from .social.export import caption_text, export_bundle
from .social.files import load_comments
from .story.canon import apply_episode, initial_state, outcome_from_tally, record_decision
from .story.critic import LLMCritic
from .story.hooks import ClaudeHookWriter, HookWriter, MockHookWriter
from .story.mock_writer import MockWriter
from .story.writer import ClaudeWriter, Writer, draft_episode
from .votes.classifier import LLMVoteClassifier
from .votes.count import count_votes

log = logging.getLogger(__name__)
S = EpisodeStatus
JUDGE_ESTIMATE_USD = 0.02  # one low-effort vision check of a downscaled frame


def pick_heroes(scenes, policy: VideoPolicy) -> list[int]:
    """Which shots get image-to-video. Writer-flagged heroes first, then the hook shot and the climax."""
    if policy.animate == "none" or policy.max_clips <= 0:
        return []
    if policy.animate == "all":
        return list(range(len(scenes)))
    picks = [i for i, sc in enumerate(scenes) if sc.hero][: policy.max_clips]
    for i in (0, len(scenes) - 1):
        if len(picks) >= policy.max_clips:
            break
        if i not in picks:
            picks.append(i)
    return sorted(picks)


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
        judge: FrameJudge | None = None,
        sources: dict[str, CommentSource] | None = None,
        publishers: dict[str, Publisher] | None = None,
        insights: dict[str, MetricsSource] | None = None,
    ):
        from .store import Store

        self.cfg = cfg
        self.bible = bible or load_bible(cfg.series_file)
        self.store = Store(cfg.data_dir, self.bible.series_id)
        self.claude = claude or Claude()
        p, m = cfg.providers, cfg.models

        sim = cfg.simulate_costs
        self.writer: Writer = writer or (
            ClaudeWriter(self.claude, self.bible, cfg.writing, m.writer, m.writer_effort)
            if p.writer == "claude"
            else MockWriter(simulate_model=m.writer if sim else None)
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

            self.images = FalImages(m.fal_image, m.fal_image_edit)
        else:
            self.images = MockImages(m.fal_image, m.fal_image_edit) if sim else MockImages()

        if video is not None:
            self.video: VideoProvider | None = video
        elif p.video == "fal":
            from .media.fal import FalVideo

            self.video = FalVideo(
                m.fal_video, negative=self.bible.negative_prompt, audio=m.fal_video_audio,
                min_seconds=cfg.video.min_seconds, max_seconds=cfg.video.max_seconds,
            )
        elif p.video == "mock":
            self.video = MockVideo(
                m.fal_video if sim else "mock", audio=m.fal_video_audio, min_seconds=cfg.video.min_seconds,
                max_seconds=cfg.video.max_seconds, width=cfg.render.width, height=cfg.render.height, fps=cfg.render.fps,
            )
        else:
            self.video = None

        self.hook_writer: HookWriter = (
            ClaudeHookWriter(self.claude, m.hooks, m.hooks_effort) if p.hooks == "claude" else MockHookWriter()
        )

        self.judge: FrameJudge | None = judge
        if judge is None and cfg.qa.enabled:
            if p.frame_judge == "claude":
                self.judge = ClaudeFrameJudge(self.claude, self.bible, m.frame_judge, m.frame_judge_effort)
            elif p.frame_judge == "mock":
                self.judge = SimulatedFrameJudge(seed=self.bible.series_id, model=m.frame_judge)

        if voice is not None:
            self.voice = voice
        elif p.voice == "elevenlabs":
            from .media.elevenlabs import ElevenLabsVoice

            self.voice = ElevenLabsVoice(m.elevenlabs_voice_id, m.elevenlabs_model)
        else:
            self.voice = MockVoice(cfg.writing.words_per_second, model=m.elevenlabs_model if sim else "mock")

        self._sources = sources
        self._publishers = publishers
        self._insights = insights

    # ------------------------------------------------------------------ state
    def state(self) -> SeriesState:
        return self.store.load_state() or initial_state(self.bible)

    def recent(self, before_day: int) -> list[EpisodeRecord]:
        n = self.cfg.writing.history_episodes_in_full
        days = [d for d in self.store.episode_days() if d < before_day][-n:]
        return [e for d in days if (e := self.store.load_episode(d))]

    # ------------------------------------------------------------------ stages
    def kpis(self, before_day: int | None = None) -> list[EpisodeKPI]:
        out = []
        for d in self.store.episode_days():
            if before_day is not None and d >= before_day:
                continue
            ep = self.store.load_episode(d)
            if ep is not None:
                out.append(episode_kpi(ep, self.cfg.pricing))
        return out

    def write(self, day: int, outcome: Outcome | None, notes: list[str] | None = None, previous=None) -> EpisodeRecord:
        log.info("day %d: writing", day)
        mc = self.cfg.metrics
        digest = performance_digest(self.kpis(before_day=day)[-mc.learn_from_last:], mc.min_episodes_to_learn)
        if digest:
            log.info("day %d: writer gets performance data from %d past episodes", day, digest.count("- Day"))
        res = draft_episode(
            performance=digest,
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
        ep = EpisodeRecord(day=day, status=S.SCRIPTED, script=res.script, outcome=outcome, quality=res.quality,
                           llm_calls=res.calls, performance_digest=digest)
        if notes:
            ep.reviewer_notes = list(notes)
        self.store.save_episode(ep)
        return ep

    def _voice_for(self, text: str, out: Path, ledger: Ledger | None = None) -> VoiceResult:
        sidecar = out.with_suffix(".json")
        cached = VoiceResult.load(sidecar)
        if cached and Path(cached.path).exists():
            return cached
        res = self.voice.synthesize(text, out)
        res.save(sidecar)
        if ledger is not None and self._billable(self.voice):
            usd = voice_cost(self.voice.model, len(text), self.cfg.pricing)
            ledger.record(CostEntry(stage="voice", provider=self.voice.name, model=self.voice.model,
                                    quantity=len(text), unit="chars", usd=round(usd, 4),
                                    simulated=self.voice.name == "mock"))
        return res

    def _billable(self, provider) -> bool:
        return provider.name not in FREE_PROVIDERS or self.cfg.simulate_costs

    # ------------------------------------------------------------------ cast (reference sheets)
    def cast_refs(self, names: list[str]) -> dict[str, Path]:
        refs: dict[str, Path] = {}
        for name in names:
            sheet = self.bible.character(name)
            if sheet is None:
                continue
            path = self.store.cast_image(sheet.name)
            if path.exists():
                refs[sheet.name] = path
        return refs

    def ensure_cast(self, force: bool = False) -> list[Path]:
        """Generate one reference sheet per character, once. Every later shot is conditioned on it."""
        made = []
        manifest = self.store.root / "cast" / "cast.json"
        entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        for c in self.bible.characters:
            out = self.store.cast_image(c.name)
            if out.exists() and not force:
                continue
            model = self.images.model_id(False)
            self.images.generate(cast_prompt(c.name, c.look, self.bible), " ".join(self.bible.negative_prompt.split()),
                                 out, seed=stable_seed(self.bible.series_id, "cast", c.name), width=1344, height=768)
            usd = image_cost(model, self.cfg.pricing) if self._billable(self.images) else 0.0
            entries[c.name] = {"path": out.name, "model": model, "usd": usd, "made_at": utcnow().isoformat()}
            made.append(out)
            log.info("cast: reference sheet for %s -> %s", c.name, out)
        if made:
            manifest.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return made

    # ------------------------------------------------------------------ frames with QA
    def _make_frame(self, ep: EpisodeRecord, i: int, ledger: Ledger, nonce: str = "") -> FrameQA:
        scene = ep.script.scenes[i]
        final = self.store.scene_image(ep.day, i)
        refs = self.cast_refs(scene.characters)
        model = self.images.model_id(bool(refs))
        r, qa = self.cfg.render, self.cfg.qa
        img_usd = image_cost(model, self.cfg.pricing) if self._billable(self.images) else 0.0
        attempts = 1 + (qa.max_rerolls if self.judge else 0)
        best: tuple[int, Path] | None = None
        notes: list[str] = []
        fix = ""
        tried = 0
        passed = False
        last_score: int | None = None

        for attempt in range(attempts):
            if attempt > 0 and not ledger.reserve(img_usd + JUDGE_ESTIMATE_USD, f"scene {i + 1} re-roll"):
                notes.append("re-roll skipped: over budget")
                break
            cand = final if attempt == 0 else final.with_name(f"{final.stem}.try{attempt}{final.suffix}")
            prompt, negative = image_prompt(scene, self.bible, list(refs), fix)
            self.images.generate(prompt, negative, cand, seed=stable_seed(self.bible.series_id, ep.day, i, attempt, nonce),
                                 width=r.width, height=r.height, refs=list(refs.values()))
            tried += 1
            if img_usd:
                ledger.record(CostEntry(stage="image", provider=self.images.name, model=model, quantity=1, unit="image",
                                        usd=img_usd, note=f"scene {i + 1}, attempt {attempt + 1}",
                                        simulated=self.images.name == "mock"),
                              reserved=img_usd if attempt else 0.0)
            if self.judge is None:
                passed = True
                break
            verdict, call = self.judge.judge(cand, scene, refs)
            if call:
                ledger.record_call(call, reserved=JUDGE_ESTIMATE_USD if attempt else 0.0)
            ok = passes(verdict, qa.min_score)
            last_score = verdict.score if verdict else None
            if verdict:
                problems = [p for p in verdict.artifacts]
                if not verdict.matches_shot:
                    problems.append("doesn't match the shot")
                if not verdict.characters_match:
                    problems.append("character off-model")
                if verdict.unsafe:
                    problems.append("unsafe content")
                notes.append(f"try {attempt + 1}: {verdict.score}/5" + (f" ({'; '.join(problems)})" if problems else ""))
                if best is None or verdict.score > best[0]:
                    best = (verdict.score, cand)
            if ok:
                passed = True
                break
            fix = verdict.fix if verdict else ""

        if not passed and best is not None and best[1] != final:
            shutil.copyfile(best[1], final)  # keep the strongest attempt, flagged for the human
        if not passed:
            ep.decisions.append(f"scene {i + 1}: no frame passed QA after {tried} tries; best kept for human review")
        score = best[0] if (best and not passed) else last_score
        return FrameQA(scene=i + 1, attempts=tried, passed=passed, score=score, notes=notes, judged=self.judge is not None,
                       simulated=bool(getattr(self.judge, "simulated", False)))

    def produce_media(self, ep: EpisodeRecord) -> EpisodeRecord:
        n = len(ep.script.scenes)
        log.info("day %d: producing media for %d scenes", ep.day, n)
        r = self.cfg.render
        ledger = Ledger(ep, self.cfg)
        self.ensure_cast()

        with ThreadPoolExecutor(max_workers=4) as pool:
            # 1. Narration first: it sets every shot's length (and so what a clip costs).
            voices = list(pool.map(lambda i: self._voice_for(ep.script.scenes[i].narration, self.store.scene_audio(ep.day, i), ledger), range(n)))
            self._voice_for(self._cta_line(ep), self.store.cta_audio(ep.day), ledger)

            # 2. Frames, each judged and re-rolled before anything expensive happens to it.
            todo = [i for i in range(n) if not self.store.scene_image(ep.day, i).exists()]
            results = list(pool.map(lambda i: self._make_frame(ep, i, ledger), todo))
            redone = {x.scene for x in results}
            ep.frame_qa = sorted([q for q in ep.frame_qa if q.scene not in redone] + results, key=lambda q: q.scene)

            # 3. Hero shots only get image-to-video, and only if the budget allows.
            if self.video is not None:
                ep.hero_scenes = pick_heroes(ep.script.scenes, self.cfg.video)
                jobs = []
                for i in ep.hero_scenes:
                    clip = self.store.scene_clip(ep.day, i)
                    if clip.exists():
                        continue
                    qa = next((q for q in ep.frame_qa if q.scene == i + 1), None)
                    if qa is not None and not qa.passed:
                        ep.decisions.append(f"scene {i + 1}: not animated because its frame failed QA")
                        continue
                    secs = self.video.billed_seconds(voices[i].duration + r.scene_padding_seconds)
                    usd = video_cost(self.video.model, secs, self.cfg.pricing, self.video.audio) if self._billable(self.video) else 0.0
                    if ledger.reserve(usd, f"scene {i + 1} animation ({secs}s)"):
                        jobs.append((i, secs, usd))

                def animate(job: tuple[int, int, float]) -> None:
                    i, secs, usd = job
                    assert self.video is not None
                    self.video.animate(self.store.scene_image(ep.day, i), motion_prompt(ep.script.scenes[i]),
                                       self.store.scene_clip(ep.day, i), seconds=secs)
                    ledger.record(CostEntry(stage="video", provider=self.video.name, model=self.video.model, quantity=secs,
                                            unit="second", usd=round(usd, 4), note=f"scene {i + 1}",
                                            simulated=self.video.name == "mock"), reserved=usd)

                list(pool.map(animate, jobs))

        refs_by_scene = [list(self.cast_refs(s.characters)) for s in ep.script.scenes]
        self.store.write_json(ep.day, "prompts.json", [
            {"scene": i + 1, "image": image_prompt(s, self.bible, refs_by_scene[i])[0], "motion": motion_prompt(s),
             "references": refs_by_scene[i], "hero": i in ep.hero_scenes}
            for i, s in enumerate(ep.script.scenes)
        ])
        ep.status = S.MEDIA_READY
        self.store.save_episode(ep)
        log.info("day %d: media done, $%.2f so far", ep.day, ledger.spent())
        return ep

    @staticmethod
    def _cta_line(ep: EpisodeRecord) -> str:
        tail = "Comment yes, or no." if ep.script.vote_style == "yes_no" else "Comment A, or B."
        return f"{ep.script.question} {tail}"

    def render(self, ep: EpisodeRecord) -> EpisodeRecord:
        log.info("day %d: rendering", ep.day)
        scenes, cta = self._scene_inputs(ep)
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
        flags = "" if ep.quality.passed else " (automated checks flagged issues)"
        notify(self.cfg.notify.webhook_url_env,
               f"{self.bible.title} day {ep.day} \"{ep.script.title}\" is ready for review{flags}: "
               f"${total(ep, self.cfg.pricing):.2f} spent. {self.store.day_dir(ep.day) / 'review.html'}\n"
               f"choices approve --day {ep.day}   |   choices reject --day {ep.day} --note \"...\"")
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
        new.costs = list(ep.costs)  # money already spent on the rejected cut still counts
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
        qa = self._make_frame(ep, i, Ledger(ep, self.cfg), nonce=utcnow().isoformat())
        ep.frame_qa = sorted([q for q in ep.frame_qa if q.scene != scene] + [qa], key=lambda q: q.scene)
        ep.status = S.SCRIPTED  # re-enter media: re-animates the shot if it's a hero, then re-renders
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

    def insight_sources(self) -> dict[str, MetricsSource]:
        if self._insights is not None:
            return self._insights
        return dict(self.sources())  # the TikTok/Instagram clients read analytics too

    def refresh_metrics(self, now: datetime | None = None, force: bool = False) -> int:
        """Pull analytics for recently published episodes. Cheap and rate-limited by refresh_hours."""
        now = now or utcnow()
        mc = self.cfg.metrics
        srcs = self.insight_sources()
        updated = 0
        for d in self.store.episode_days():
            ep = self.store.load_episode(d)
            if ep is None or ep.status not in (S.PUBLISHED, S.RESOLVED) or not ep.posts:
                continue
            published = min(p.published_at for p in ep.posts)
            if not force and now - published > timedelta(days=mc.track_days):
                continue
            changed = False
            for post in ep.posts:
                src = srcs.get(post.platform)
                prev = ep.metrics.get(post.platform)
                if src is None or (not force and prev and now - prev.captured_at < timedelta(hours=mc.refresh_hours)):
                    continue
                try:
                    m = src.metrics(post.post_id)
                except Exception as e:  # analytics must never block the story
                    log.warning("day %d: %s analytics failed: %s", d, post.platform, e)
                    continue
                m.captured_at = now
                ep.metrics[post.platform] = m
                changed = True
            if changed:
                self.store.save_episode(ep)
                updated += 1
        if updated:
            from .review import write_index

            write_index(self)
        return updated

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
        notify(self.cfg.notify.webhook_url_env,
               f"{self.bible.title} day {day}: the audience chose {result.winner} "
               f"({ep.script.choice(result.winner).label}), {result.unique_voters} voters, decided by {result.decided_by}.")
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
        self.refresh_metrics(now)
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

    # ------------------------------------------------------------------ ad variants
    FORMATS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}

    def _scene_inputs(self, ep: EpisodeRecord) -> tuple[list[SceneInput], VoiceResult]:
        scenes = []
        for i in range(len(ep.script.scenes)):
            v = VoiceResult.load(self.store.scene_audio(ep.day, i).with_suffix(".json"))
            if v is None:
                raise RuntimeError(f"day {ep.day} scene {i + 1}: narration missing; re-run media")
            clip = self.store.scene_clip(ep.day, i)
            scenes.append(SceneInput(image=self.store.scene_image(ep.day, i), clip=clip if clip.exists() else None, voice=v))
        cta = VoiceResult.load(self.store.cta_audio(ep.day).with_suffix(".json"))
        if cta is None:
            raise RuntimeError(f"day {ep.day}: choice-card narration missing; re-run media")
        return scenes, cta

    def make_variants(self, day: int, hooks: int = 3, formats: list[str] | None = None) -> list[Variant]:
        """Hooks x aspect ratios from one episode. Reuses every paid asset; only the overlay and crop change."""
        ep = self.store.load_episode(day)
        if ep is None or not ep.at_least(S.RENDERED):
            raise ValueError(f"day {day} must be rendered before making variants")
        formats = formats or ["9:16", "4:5", "1:1"]
        bad = [f for f in formats if f not in self.FORMATS]
        if bad:
            raise ValueError(f"unknown formats {bad}; choose from {list(self.FORMATS)}")

        digest = performance_digest(self.kpis(before_day=day), self.cfg.metrics.min_episodes_to_learn)
        new_hooks, call = self.hook_writer.write(ep.script, hooks, digest)
        if call:
            ep.llm_calls.append(call)
        all_hooks = [("original", ep.script.hook)] + [(h.angle, h.hook) for h in new_hooks]

        scenes, cta = self._scene_inputs(ep)
        scale = self.cfg.render.width / 1080
        root = self.store.day_dir(day) / "variants"
        out: list[Variant] = []
        for fmt in formats:
            w, h = self.FORMATS[fmt]
            rcfg = self.cfg.render.model_copy(update={"width": int(w * scale) // 2 * 2, "height": int(h * scale) // 2 * 2})
            tag = fmt.replace(":", "x")
            for angle, text in all_hooks:
                render_episode(
                    script=ep.script, day=day, series_title=self.bible.title, scenes=scenes, cta_voice=cta, cfg=rcfg,
                    out_dir=root / tag, window_hours=self.cfg.voting.window_hours, hook=text,
                    work_dir=root / f"_work_{tag}", final_name=f"{angle}.mp4", reuse=True, make_cover=False,
                )
                out.append(Variant(angle=angle, hook=text, format=fmt, path=f"variants/{tag}/{angle}.mp4"))
            shutil.rmtree(root / f"_work_{tag}", ignore_errors=True)
        ep.variants = out
        self.store.save_episode(ep)
        self.store.write_json(day, "variants/variants.json", [v.model_dump() for v in out])
        self._refresh_pages(ep)
        log.info("day %d: %d variants (%d hooks x %d formats), no new media spend", day, len(out), len(all_hooks), len(formats))
        return out

    # ------------------------------------------------------------------ model bake-off
    def bakeoff(self, day: int, models: list[str], scenes: list[int] | None = None) -> Path:
        """Run the same shots through several image models; score with the frame judge; compare cost and speed."""
        from .bakeoff import run_bakeoff

        ep = self.store.load_episode(day)
        if ep is None:
            raise ValueError(f"no episode for day {day}")
        return run_bakeoff(self, ep, models, scenes or [1, 2, 3])

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

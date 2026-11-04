"""Cost control, frame QA, reference images, analytics loop, variants, bake-off, notifications."""

import json
from pathlib import Path

import httpx
import pytest
from conftest import ROOT, T0

from choices.analytics import EpisodeKPI, performance_digest
from choices.config import InstagramConfig, TikTokConfig, VideoPolicy, load_pipeline
from choices.costs import BudgetExceeded, Ledger, estimate, llm_cost
from choices.llm import image_block
from choices.media.fal import FalImages, FalVideo
from choices.media.mock import MockImages
from choices.models import EpisodeRecord, EpisodeStatus, FrameVerdict, LLMCall, QualityReport, Scene
from choices.notify import notify
from choices.pipeline import Pipeline, pick_heroes
from choices.social.instagram import InstagramGraph
from choices.social.tiktok import TikTokBusiness


@pytest.fixture
def cfg(tmp_path):
    c = load_pipeline(ROOT / "config" / "pipeline.yaml").offline()
    c.data_dir = str(tmp_path / "runs")
    c.render.width, c.render.height, c.render.fps = 180, 320, 12
    return c


# --------------------------------------------------------------------------- costs


def test_llm_cost_uses_all_token_classes(cfg):
    call = LLMCall(purpose="writer", model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000,
                   cache_read_tokens=1_000_000, cache_write_tokens=0)
    assert llm_cost(call, cfg.pricing) == pytest.approx(4.0 + 2.0 + 0.2)
    assert llm_cost(LLMCall(purpose="x", model="unknown-model", input_tokens=10**6), cfg.pricing) == 0


def test_budget_guard_degrades_or_stops(cfg, pilot):
    ep = EpisodeRecord(day=1, status=EpisodeStatus.SCRIPTED, script=pilot, quality=QualityReport(passed=True))
    cfg.budget.max_usd_per_episode = 1.0
    ledger = Ledger(ep, cfg)
    assert ledger.reserve(0.8, "clip 1")
    assert not ledger.reserve(0.5, "clip 2")  # 0.8 reserved + 0.5 > 1.0
    assert ep.decisions and "clip 2" in ep.decisions[0]
    cfg.budget.on_exceed = "stop"
    with pytest.raises(BudgetExceeded):
        Ledger(ep, cfg).reserve(5.0, "big clip")


def test_estimate_orders_configurations_by_cost(cfg):
    rows = estimate(load_pipeline(ROOT / "config" / "pipeline.yaml"))
    costs = [r.per_episode for r in rows]
    assert costs == sorted(costs) and costs[0] < costs[1] < costs[3]
    assert "video" in rows[1].lines and "video" not in rows[0].lines
    assert rows[3].per_episode / rows[1].per_episode > 2  # hero-only saves more than half


def test_pick_heroes():
    sc = [Scene(narration="", visual="", motion="", characters=[], hero=h) for h in (False, False, True, False, False)]
    assert pick_heroes(sc, VideoPolicy()) == [0, 2]  # flagged shot + the hook shot
    assert pick_heroes(sc, VideoPolicy(max_clips=1)) == [2]
    assert pick_heroes([s.model_copy(update={"hero": False}) for s in sc], VideoPolicy()) == [0, 4]
    assert pick_heroes(sc, VideoPolicy(animate="all")) == [0, 1, 2, 3, 4]
    assert pick_heroes(sc, VideoPolicy(animate="none")) == []


# --------------------------------------------------------------------------- fal payloads


def test_nano_banana_payload_with_references(tmp_path):
    ref = tmp_path / "lola.png"
    MockImages().generate("x", "", ref, seed=1, width=64, height=64)
    imgs = FalImages("fal-ai/nano-banana-2", "fal-ai/nano-banana-2/edit")
    assert imgs.model_id(True) == "fal-ai/nano-banana-2/edit" and imgs.model_id(False) == "fal-ai/nano-banana-2"
    p = imgs.payload("fal-ai/nano-banana-2/edit", "a shot", "gore", seed=7, width=1080, height=1920, refs=[ref])
    assert p["aspect_ratio"] == "9:16" and p["resolution"] == "1K" and p["seed"] == 7
    assert p["image_urls"][0].startswith("data:image/png;base64,")
    assert p["prompt"].endswith("Avoid: gore")
    legacy = imgs.payload("fal-ai/flux/dev", "a shot", "gore", seed=7, width=1080, height=1920, refs=[])
    assert "image_urls" not in legacy and legacy["image_size"] == {"width": 768, "height": 1344}


def test_kling_v3_payload_bills_only_what_the_shot_needs(tmp_path):
    img = tmp_path / "f.png"
    MockImages().generate("x", "", img, seed=1, width=64, height=64)
    v = FalVideo("fal-ai/kling-video/v3/pro/image-to-video", negative="blur")
    p = v.payload(img, "push in", 9.2)
    assert p["duration"] == "10" and p["generate_audio"] is False and p["start_image_url"].startswith("data:")
    assert "image_url" not in p and p["negative_prompt"] == "blur"
    assert v.billed_seconds(1.0) == 3 and v.billed_seconds(40) == 15
    old = FalVideo("fal-ai/kling-video/v2.1/standard/image-to-video")
    assert old.payload(img, "x", 7)["duration"] == "10" and "image_url" in old.payload(img, "x", 3)


# --------------------------------------------------------------------------- QA gate + references


class RecordingImages(MockImages):
    def __init__(self):
        super().__init__("fal-ai/nano-banana-2", "fal-ai/nano-banana-2/edit")
        self.calls = []

    def generate(self, prompt, negative, out, *, seed, width, height, refs=None):
        self.calls.append({"prompt": prompt, "refs": [Path(r).name for r in (refs or [])], "out": Path(out).name})
        return super().generate(prompt, negative, out, seed=seed, width=width, height=height, refs=refs)


class FirstTryFails:
    """Rejects the first attempt of scene 1 with a fix, passes everything else."""

    def __init__(self):
        self.seen = set()

    def judge(self, image, scene, refs):
        key = scene.visual
        first = key not in self.seen
        self.seen.add(key)
        if first and "second-floor window" in key:
            return FrameVerdict(matches_shot=True, characters_match=True, artifacts=["garbled sign"], unsafe=False,
                                score=2, fix="no signage anywhere"), None
        return FrameVerdict(matches_shot=True, characters_match=True, artifacts=[], unsafe=False, score=4, fix=""), None


def test_frames_are_judged_rerolled_with_fix_and_conditioned_on_cast(cfg, bible):
    images = RecordingImages()
    pipe = Pipeline(cfg, bible=bible, images=images, judge=FirstTryFails())
    ep = pipe.produce_media(pipe.write(1, None))

    qa1 = next(q for q in ep.frame_qa if q.scene == 1)
    assert qa1.attempts == 2 and qa1.passed and "garbled sign" in qa1.notes[0]
    retry = next(c for c in images.calls if c["out"] == "01.try1.png")
    assert "no signage anywhere" in retry["prompt"]

    cast_calls = [c for c in images.calls if c["out"] in ("lola-cora.png", "migs.png", "bantay.png")]
    assert len(cast_calls) == 3  # reference sheets made once, up front
    scene2 = next(c for c in images.calls if c["out"] == "02.png")  # Lola Cora's close-up
    assert scene2["refs"] == ["lola-cora.png"] and "image 1 is Lola Cora" in scene2["prompt"]

    stages = {c.stage for c in ep.costs}
    assert {"image", "video", "voice"} <= stages and all(c.simulated for c in ep.costs)
    assert sum(1 for c in ep.costs if c.stage == "image") == len(ep.script.scenes) + 1  # one re-roll
    assert ep.hero_scenes == [0, 2] and pipe.store.scene_clip(1, 2).exists()


def test_budget_skips_animation_but_keeps_the_episode(cfg, bible):
    cfg.budget.max_usd_per_episode = 0.9  # enough for stills, not for clips
    pipe = Pipeline(cfg, bible=bible)
    ep = pipe.produce_media(pipe.write(1, None))
    assert ep.status == EpisodeStatus.MEDIA_READY
    assert not any(pipe.store.scene_clip(1, i).exists() for i in range(len(ep.script.scenes)))
    assert any("animation" in d for d in ep.decisions)


def test_image_block_downscales(tmp_path):
    p = tmp_path / "big.png"
    MockImages().generate("x", "", p, seed=1, width=1080, height=1920)
    block = image_block(p)
    assert block["source"]["media_type"] == "image/jpeg"
    import base64
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert max(im.size) == 768


# --------------------------------------------------------------------------- analytics


def test_tiktok_metrics_mapping(monkeypatch):
    monkeypatch.setenv("TIKTOK_BUSINESS_ID", "biz")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tok")

    def handler(req: httpx.Request):
        assert req.url.path.endswith("/business/video/list/")
        assert json.loads(req.url.params["filters"]) == {"video_ids": ["v9"]}
        assert "video_view_retention" in json.loads(req.url.params["fields"])
        return httpx.Response(200, json={"code": 0, "data": {"videos": [{
            "item_id": "v9", "video_views": 12000, "reach": 9000, "likes": 800, "comments": 240, "shares": 30,
            "favorites": 55, "average_time_watched": 21.5, "full_video_watched_rate": 34.0,
            "video_view_retention": [{"second": "1", "percentage": 0.9}, {"second": "3", "percentage": 0.62}]}]}})

    tt = TikTokBusiness(TikTokConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    m = tt.metrics("v9")
    assert (m.views, m.saves, m.completion_rate, m.hold_3s) == (12000, 55, 0.34, 0.62)


def test_instagram_metrics_fall_back_to_core_set(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url.params["metric"])
        if "reels_skip_rate" in req.url.params["metric"]:
            return httpx.Response(400, json={"error": {"message": "metric not supported"}})
        return httpx.Response(200, json={"data": [
            {"name": "reach", "values": [{"value": 5000}]}, {"name": "likes", "values": [{"value": 300}]},
            {"name": "comments", "values": [{"value": 90}]}, {"name": "shares", "values": [{"value": 12}]},
            {"name": "saved", "values": [{"value": 40}]}]})

    ig = InstagramGraph(InstagramConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    import choices.social.base as base

    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    m = ig.metrics("m1")
    assert len(seen) == 2 and m.views == 5000 and m.saves == 40 and m.hold_3s is None


def test_instagram_skip_rate_becomes_hold(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    body = {"data": [{"name": "views", "values": [{"value": 8000}]}, {"name": "reels_skip_rate", "values": [{"value": 41}]},
                     {"name": "ig_reels_avg_watch_time", "values": [{"value": 18500}]}]}
    ig = InstagramGraph(InstagramConfig(), client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    m = ig.metrics("m1")
    assert m.skip_rate == pytest.approx(0.41) and m.hold_3s == pytest.approx(0.59) and m.avg_watch_seconds == 18.5


def kpi(day, hold, votes=None, views=10000):
    return EpisodeKPI(day=day, title=f"t{day}", hook=f"hook {day}", question=f"q{day}?", views=views, hold_3s=hold,
                      completion=0.3, votes_per_1k=votes, shares_per_1k=1.0, cost_usd=3.0, cost_per_1k_views=0.3, simulated=False)


def test_performance_digest_ranks_and_waits_for_data():
    assert performance_digest([kpi(1, 0.5)]) == ""
    d = performance_digest([kpi(1, 0.5, 9), kpi(2, 0.7, 14), kpi(3, 0.6, 20), kpi(4, 0.4, 5)])
    best, worst = d.split("Weakest hooks:")
    assert "Day 2" in best.split("\n")[3] and "Day 4" in worst.split("\n")[1]
    assert 'Day 3: "q3?" (20 votes per 1k views)' in d and "not instructions" in d


def test_metrics_feed_dashboard_and_next_writer(cfg, bible):
    from choices.analytics import SimulatedInsights

    pipe = Pipeline(cfg, bible=bible)
    sim = SimulatedInsights(pipe.store)
    pipe._insights = {"tiktok": sim, "instagram": sim}
    now = T0
    for day in (1, 2):
        ep = pipe.write(day, None)
        ep.status = EpisodeStatus.PUBLISHED
        from choices.models import PostRef

        ep.posts = [PostRef(platform="tiktok", post_id=f"sim-tiktok-{day}", published_at=now)]
        pipe.store.save_episode(ep)
    assert pipe.refresh_metrics(now) == 2
    assert pipe.refresh_metrics(now) == 0  # rate-limited by refresh_hours
    ep3 = pipe.write(3, None)
    assert "Strongest hooks" in ep3.performance_digest
    html = (pipe.store.root / "index.html").read_text()
    assert "Cost per 1K views" in html and "Simulated" in html


# --------------------------------------------------------------------------- variants, bake-off, notify


def test_variants_reuse_media(cfg, bible):
    cfg.publish.require_review = True
    pipe = Pipeline(cfg, bible=bible)
    pipe.tick(T0)
    before = len(pipe.store.load_episode(1).costs)
    vs = pipe.make_variants(1, hooks=1, formats=["9:16", "1:1"])
    assert len(vs) == 4 and {v.format for v in vs} == {"9:16", "1:1"}
    for v in vs:
        assert (pipe.store.day_dir(1) / v.path).exists()
    ep = pipe.store.load_episode(1)
    assert len(ep.costs) == before  # no new media spend
    with pytest.raises(ValueError):
        pipe.make_variants(1, formats=["3:2"])


def test_bakeoff_report(cfg, bible):
    pipe = Pipeline(cfg, bible=bible)
    pipe.produce_media(pipe.write(1, None))
    report = pipe.bakeoff(1, ["mock-a", "mock-b"], [1, 2])
    data = json.loads((report.parent / "results.json").read_text())
    assert [s["model"] for s in data["summary"]] == ["mock-a", "mock-b"]
    assert all(s["images"] == 2 and s["pass_rate"] is not None for s in data["summary"])
    assert "per usable frame" in report.read_text()


def test_notify_posts_slack_and_discord_keys(monkeypatch):
    got = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: got.append(json.loads(r.content)) or httpx.Response(204)))
    assert not notify("NOPE_UNSET_ENV", "hi", client)
    monkeypatch.setenv("HOOK_URL", "https://hooks.example/abc")
    assert notify("HOOK_URL", "Day 4 ready", client)
    assert got == [{"text": "Day 4 ready", "content": "Day 4 ready"}]
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert not notify("HOOK_URL", "x", bad)  # never raises

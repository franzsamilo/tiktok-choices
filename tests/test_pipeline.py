"""End to end, offline, at thumbnail resolution: write -> render -> review gate -> publish -> vote -> next day."""

import json
from datetime import timedelta

import pytest
from conftest import ROOT, T0

from choices.config import load_pipeline
from choices.models import EpisodeStatus as S
from choices.pipeline import Pipeline
from choices.render.ffmpeg import probe_duration


@pytest.fixture
def pipe(tmp_path, bible):
    cfg = load_pipeline(ROOT / "config" / "pipeline.yaml").offline()
    cfg.data_dir = str(tmp_path / "runs")
    cfg.render.width, cfg.render.height, cfg.render.fps = 180, 320, 12
    cfg.publish.require_review = True
    return Pipeline(cfg, bible=bible)


def test_full_loop(pipe, tmp_path):
    r = pipe.tick(T0)
    assert (r.day, r.status) == (1, S.AWAITING_REVIEW)
    d1 = pipe.store.day_dir(1)
    for f in ("final.mp4", "cover.jpg", "review.html", "prompts.json", "scenes/01.png", "audio/01.json", "audio/cta.json"):
        assert (d1 / f).exists(), f
    assert probe_duration(d1 / "final.mp4") > 30

    # Ticking again doesn't skip the human.
    assert pipe.tick(T0).status == S.AWAITING_REVIEW

    ep = pipe.approve(1, T0)
    assert ep.status == S.PUBLISHED and (d1 / "post" / "caption.txt").exists()
    assert pipe.state().day == 1  # canon applied on publish
    assert "no posts are linked" in pipe.tick(T0 + timedelta(hours=25)).message

    # Votes arrive via a comment export dropped into the inbox.
    inbox = d1 / "inbox"
    inbox.mkdir()
    rows = ["text,username,likes"] + [f"keep it locked,user{i},0" for i in range(4)] + [f"A,fan{i},1" for i in range(7)]
    (inbox / "tiktok.csv").write_text("\n".join(rows), encoding="utf-8")
    assert "voting open" in pipe.tick(T0 + timedelta(hours=2)).message

    r = pipe.tick(T0 + timedelta(hours=25))
    assert (r.day, r.status) == (2, S.AWAITING_REVIEW)
    ep1 = pipe.store.load_episode(1)
    assert ep1.status == S.RESOLVED and ep1.tally.winner == "A" and ep1.tally.unique_voters == 11
    ep2 = pipe.store.load_episode(2)
    assert ep2.outcome.chosen_label == "OPEN THE DOOR"
    assert ep2.script.title == "The Click"  # the branch the audience picked
    audit = json.loads((d1 / "votes_audit.json").read_text())
    assert len(audit) == 11 and all(a["counted"] for a in audit)
    assert (pipe.store.root / "index.html").exists()


def test_reject_rewrites_with_notes_and_reroll_keeps_script(pipe):
    pipe.tick(T0)
    ep = pipe.reject(1, ["Make Migs younger"], T0)
    assert ep.status == S.AWAITING_REVIEW and ep.reviewer_notes == ["Make Migs younger"]
    before = (pipe.store.scene_image(1, 2)).read_bytes()
    ep = pipe.reroll(1, 3, T0)
    assert ep.status == S.AWAITING_REVIEW
    assert pipe.store.scene_image(1, 2).read_bytes() != before
    assert list((pipe.store.day_dir(1) / "scenes").glob("03.rejected-*.png"))


def test_wrong_state_transitions_are_refused(pipe):
    pipe.tick(T0)
    with pytest.raises(ValueError):
        pipe.tally(1, T0)
    with pytest.raises(ValueError):
        pipe.link(1, "tiktok", "123")

"""Adapters against recorded-shape fake HTTP: no network, no keys."""

import json
from types import SimpleNamespace

import httpx
import pytest

from choices.config import InstagramConfig, TikTokConfig
from choices.llm import _FALLBACK_BETA, Claude, LLMRefusal
from choices.media.elevenlabs import words_from_alignment
from choices.models import CriticVerdict
from choices.social.files import load_comments
from choices.social.instagram import InstagramGraph
from choices.social.tiktok import TikTokBusiness
from choices.votes.classifier import LLMVoteClassifier, VoteBatch


def test_tiktok_paginates_and_maps(monkeypatch):
    monkeypatch.setenv("TIKTOK_BUSINESS_ID", "biz")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tok")
    pages = {
        0: {"comments": [{"comment_id": "1", "unique_identifier": "u1", "username": "ana", "text": "A", "create_time": 1767225600, "likes": 3,
                          "reply_list": [{"comment_id": "1r", "unique_identifier": "u2", "text": "B!", "create_time": 1767225700, "parent_comment_id": "1"}]}],
            "cursor": 30, "has_more": True},
        30: {"comments": [{"comment_id": "2", "user_id": "u3", "text": "b po", "create_time": "1767225800", "owner": True}], "cursor": 60, "has_more": False},
    }

    def handler(req: httpx.Request):
        assert req.headers["Access-Token"] == "tok"
        assert req.url.path.endswith("/business/comment/list/")
        assert req.url.params["video_id"] == "v1" and req.url.params["business_id"] == "biz"
        return httpx.Response(200, json={"code": 0, "message": "OK", "data": pages[int(req.url.params["cursor"])]})

    tt = TikTokBusiness(TikTokConfig(), client=httpx.Client(transport=httpx.MockTransport(handler), headers={"Access-Token": "tok"}))
    cs = tt.fetch("v1")
    assert [c.id for c in cs] == ["1", "1r", "2"]
    assert cs[1].parent_id == "1" and cs[0].likes == 3 and cs[2].author_id == "u3" and cs[2].is_owner


def test_tiktok_error_envelope_raises(monkeypatch):
    monkeypatch.setenv("TIKTOK_BUSINESS_ID", "biz")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tok")
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 40001, "message": "Access token is expired", "request_id": "x"}))
    tt = TikTokBusiness(TikTokConfig(), client=httpx.Client(transport=t))
    with pytest.raises(Exception, match="expired"):
        tt.fetch("v1")


def test_instagram_follows_paging_and_reads_replies(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "999")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    page2 = "https://graph.facebook.com/v21.0/m1/comments?after=abc&access_token=tok"

    def handler(req: httpx.Request):
        if "after" in req.url.params:
            return httpx.Response(200, json={"data": [{"id": "c3", "text": "A", "username": "me", "timestamp": "2026-11-01T10:00:00+0000", "from": {"id": "999"}}]})
        assert req.url.params["access_token"] == "tok"
        return httpx.Response(200, json={
            "data": [{"id": "c1", "text": "B obviously", "username": "ana", "timestamp": "2026-11-01T09:00:00+0000", "like_count": 4,
                      "from": {"id": "111", "username": "ana"},
                      "replies": {"data": [{"id": "c2", "text": "same, B", "username": "ben", "timestamp": "2026-11-01T09:05:00+0000"}]}}],
            "paging": {"next": page2},
        })

    ig = InstagramGraph(InstagramConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    cs = ig.fetch("m1")
    assert [c.id for c in cs] == ["c1", "c2", "c3"]
    assert cs[0].author_id == "111" and cs[0].likes == 4
    assert cs[1].parent_id == "c1" and cs[1].author_id == "ben"
    assert cs[2].is_owner


def test_file_import_handles_aliases(tmp_path):
    p = tmp_path / "export.csv"
    p.write_text("Comment,Username,Likes,Platform\nA talaga,ana,3,tiktok\n,empty,0,\nB po,ben,,instagram\n", encoding="utf-8")
    cs = load_comments(p)
    assert [(c.text, c.author_id, c.likes, c.platform) for c in cs] == [("A talaga", "ana", 3, "tiktok"), ("B po", "ben", 0, "instagram")]
    j = tmp_path / "export.json"
    j.write_text(json.dumps({"comments": [{"text": "A", "author": "zed"}]}), encoding="utf-8")
    assert load_comments(j)[0].author_id == "zed"


def test_elevenlabs_alignment_to_words():
    al = {"characters": list("Hi there."), "character_start_times_seconds": [0, .1, .2, .3, .4, .5, .6, .7, .8],
          "character_end_times_seconds": [.1, .2, .3, .4, .5, .6, .7, .8, .9]}
    ws = words_from_alignment(al)
    assert [(w.word, w.start, w.end) for w in ws] == [("Hi", 0, .2), ("there.", .3, .9)]


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def parse(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def fake_client(*responses):
    msgs = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def resp(parsed, stop="end_turn", **extra):
    usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=900)
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop, usage=usage, model="claude-opus-5-5", **extra)


def test_claude_wrapper_request_shape_and_usage():
    v = CriticVerdict(continuity_errors=[], dilemma_is_balanced=True, hook_strength=4, notes=[])
    client, msgs = fake_client(resp(v))
    out, call = Claude(client).structured(purpose="critic", model="claude-opus-5-5", effort="medium", system="S", user="U", output=CriticVerdict)
    assert out is v and call.input_tokens == 1200 and call.cache_read_tokens == 900
    kw = msgs.calls[0]
    assert kw["fallbacks"] == "default" and kw["betas"] == [_FALLBACK_BETA]
    assert kw["thinking"] == {"type": "adaptive"} and kw["output_config"] == {"effort": "medium"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_format"] is CriticVerdict


def test_claude_wrapper_raises_on_refusal_and_skips_fallback_for_other_models():
    client, msgs = fake_client(resp(None, stop="refusal", stop_details=SimpleNamespace(category="cyber", explanation="no")))
    with pytest.raises(LLMRefusal):
        Claude(client).structured(purpose="x", model="claude-haiku-5-5", effort="low", system="S", user="U", output=CriticVerdict)
    assert "fallbacks" not in msgs.calls[0]


def test_vote_classifier_batches_and_ignores_unknown_ids(pilot):
    from conftest import comment

    cs = [comment(f"thoughts {i}", f"u{i}") for i in range(5)]
    batch1 = VoteBatch(votes=[{"id": "c0", "vote": "A"}, {"id": "c1", "vote": "none"}, {"id": "c99", "vote": "B"}])
    batch2 = VoteBatch(votes=[{"id": "c0", "vote": "B"}, {"id": "c1", "vote": "B"}])
    client, msgs = fake_client(resp(batch1), resp(batch2))
    out, calls = LLMVoteClassifier(Claude(client), "claude-opus-5-5", "low", batch_size=3).classify(pilot, cs)
    assert len(msgs.calls) == 2 and len(calls) == 2
    assert out == {cs[0].key: "A", cs[1].key: None, cs[3].key: "B", cs[4].key: "B"}
    assert '<comment id="c0">thoughts 0</comment>' in msgs.calls[0]["messages"][0]["content"]

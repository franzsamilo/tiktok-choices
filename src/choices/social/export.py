"""Export mode: produce a ready-to-post bundle instead of calling platform APIs.

This is the default because both platforms gate API publishing behind app
review (unaudited TikTok apps can only post privately). A person posts the
bundle by hand, then registers the post ids with ``choices link`` so the vote
can be collected automatically the next day.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import EpisodeRecord


def caption_text(ep: EpisodeRecord, disclose_ai: bool) -> str:
    tags = " ".join(f"#{h}" for h in ep.script.hashtags)
    note = "\n\nMade with AI. Story steered by your votes." if disclose_ai else ""
    return f"{ep.script.caption}\n\nA: {ep.script.choice_a.label}\nB: {ep.script.choice_b.label}{note}\n\n{tags}"


def export_bundle(ep: EpisodeRecord, day_dir: Path, disclose_ai: bool) -> Path:
    out = day_dir / "post"
    out.mkdir(parents=True, exist_ok=True)
    src = day_dir / "final.mp4"
    if src.exists():
        shutil.copy2(src, out / f"day-{ep.day:03d}.mp4")
    cover = day_dir / "cover.jpg"
    if cover.exists():
        shutil.copy2(cover, out / "cover.jpg")
    (out / "caption.txt").write_text(caption_text(ep, disclose_ai), encoding="utf-8")
    (out / "HOW_TO_POST.txt").write_text(
        "1. Post the .mp4 to TikTok and Instagram Reels with caption.txt.\n"
        "2. Turn ON the AI-generated content label on both platforms.\n"
        "3. Pin a comment: 'Comment A or B. Voting closes in 24h.'\n"
        "4. Register the post ids so votes are collected automatically:\n"
        f"   choices link --day {ep.day} --tiktok <video_id> --instagram <media_id>\n",
        encoding="utf-8",
    )
    return out

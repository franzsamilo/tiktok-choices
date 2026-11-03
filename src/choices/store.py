"""Filesystem persistence.

Everything a run produces lives under ``<data_dir>/<series_id>/``::

    state.json                 canon + history (SeriesState)
    day-003/episode.json       EpisodeRecord (script, status, posts, tally, ...)
    day-003/scenes/01.png      generated stills (and 01.mp4 when image-to-video is on)
    day-003/audio/01.mp3       narration per scene + 01.json word timings
    day-003/final.mp4          the post
    day-003/comments.json      raw comments pulled for the vote
    day-003/review.html        human review page

Plain JSON on disk keeps every intermediate inspectable and makes the pipeline
resumable: a step whose output already exists is skipped on re-run, so a
failed provider call costs one retry, not a whole episode.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from .models import Comment, EpisodeRecord, SeriesState, utcnow


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, data_dir: str | Path, series_id: str):
        self.root = Path(data_dir) / series_id
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- paths -------------------------------------------------------------
    def day_dir(self, day: int) -> Path:
        d = self.root / f"day-{day:03d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def scene_image(self, day: int, i: int) -> Path:
        return self.day_dir(day) / "scenes" / f"{i + 1:02d}.png"

    def scene_clip(self, day: int, i: int) -> Path:
        return self.day_dir(day) / "scenes" / f"{i + 1:02d}.mp4"

    def scene_audio(self, day: int, i: int) -> Path:
        return self.day_dir(day) / "audio" / f"{i + 1:02d}.mp3"

    def cta_audio(self, day: int) -> Path:
        return self.day_dir(day) / "audio" / "cta.mp3"

    def final_video(self, day: int) -> Path:
        return self.day_dir(day) / "final.mp4"

    # ---- state -------------------------------------------------------------
    def load_state(self) -> SeriesState | None:
        p = self.root / "state.json"
        if not p.exists():
            return None
        return SeriesState.model_validate_json(p.read_text(encoding="utf-8"))

    def save_state(self, state: SeriesState) -> None:
        _atomic_write(self.root / "state.json", state.model_dump_json(indent=2))

    # ---- episodes ----------------------------------------------------------
    def episode_days(self) -> list[int]:
        days = []
        for p in self.root.glob("day-*/episode.json"):
            try:
                days.append(int(p.parent.name.split("-")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(days)

    def load_episode(self, day: int) -> EpisodeRecord | None:
        p = self.root / f"day-{day:03d}" / "episode.json"
        if not p.exists():
            return None
        return EpisodeRecord.model_validate_json(p.read_text(encoding="utf-8"))

    def latest_episode(self) -> EpisodeRecord | None:
        days = self.episode_days()
        return self.load_episode(days[-1]) if days else None

    def save_episode(self, ep: EpisodeRecord) -> None:
        ep.updated_at = utcnow()
        _atomic_write(self.day_dir(ep.day) / "episode.json", ep.model_dump_json(indent=2))

    # ---- comments ----------------------------------------------------------
    def save_comments(self, day: int, comments: list[Comment]) -> None:
        payload = [c.model_dump(mode="json") for c in comments]
        _atomic_write(self.day_dir(day) / "comments.json", json.dumps(payload, indent=1, ensure_ascii=False))

    def load_comments(self, day: int) -> list[Comment]:
        p = self.day_dir(day) / "comments.json"
        if not p.exists():
            return []
        return [Comment.model_validate(c) for c in json.loads(p.read_text(encoding="utf-8"))]

    def write_json(self, day: int, name: str, obj: BaseModel | dict | list) -> Path:
        p = self.day_dir(day) / name
        text = obj.model_dump_json(indent=2) if isinstance(obj, BaseModel) else json.dumps(obj, indent=2, ensure_ascii=False)
        _atomic_write(p, text)
        return p

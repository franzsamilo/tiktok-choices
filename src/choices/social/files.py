"""Import comments from a file.

The escape hatch for when API access isn't approved yet: export comments with
whatever tool you have (a scraper, a browser extension, a spreadsheet you
typed up) into CSV or JSON, then ``choices tally --day N --comments file.csv``.

Accepted columns/keys (extra ones are ignored):
``platform, id, author_id, author_name, text, created_at, likes``.
Only ``text`` is required. Rows without a timestamp get ``default_time``
(the pipeline passes a moment inside the voting window), because hand-made
exports often have none and they shouldn't be thrown out as "late".
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..models import Comment
from .base import parse_time

ALIASES = {
    "text": ("text", "comment", "content", "message", "body"),
    "author_id": ("author_id", "user_id", "unique_id", "unique_identifier", "uid", "username", "author", "user"),
    "author_name": ("author_name", "username", "nickname", "display_name", "author", "user"),
    "created_at": ("created_at", "timestamp", "create_time", "time", "date"),
    "likes": ("likes", "like_count", "digg_count", "likes_count"),
    "id": ("id", "comment_id", "cid"),
    "platform": ("platform", "source"),
}


def _pick(row: dict, field: str) -> str:
    lower = {k.lower().strip(): v for k, v in row.items() if k}
    for alias in ALIASES[field]:
        v = lower.get(alias)
        if v not in (None, ""):
            return str(v)
    return ""


def load_comments(
    path: str | Path, post_id: str = "", default_platform: str = "file", default_time: datetime | None = None
) -> list[Comment]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("comments", data) if isinstance(data, dict) else data
    else:
        with p.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

    out: list[Comment] = []
    for i, row in enumerate(rows):
        text = _pick(row, "text")
        if not text:
            continue
        platform = _pick(row, "platform").lower() or default_platform
        if platform not in ("tiktok", "instagram", "file", "simulated"):
            platform = "file"
        cid = _pick(row, "id") or hashlib.sha1(f"{i}:{text}".encode()).hexdigest()[:12]
        likes = _pick(row, "likes")
        out.append(
            Comment(
                platform=platform,  # type: ignore[arg-type]
                id=cid,
                post_id=post_id,
                author_id=_pick(row, "author_id") or cid,
                author_name=_pick(row, "author_name"),
                text=text,
                created_at=parse_time(ts) if (ts := _pick(row, "created_at")) else (default_time or parse_time(None)),
                likes=int(float(likes)) if likes else 0,
            )
        )
    return out

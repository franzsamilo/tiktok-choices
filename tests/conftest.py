from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from choices.config import load_bible
from choices.models import Comment
from choices.story.mock_writer import _pilot

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def bible():
    return load_bible(ROOT / "config" / "series.yaml")


@pytest.fixture
def pilot():
    return _pilot()


def comment(text: str, author: str = "u1", *, platform: str = "tiktok", minutes: int = 10, likes: int = 0, owner: bool = False, cid: str | None = None) -> Comment:
    return Comment(
        platform=platform,  # type: ignore[arg-type]
        id=cid or f"{author}-{minutes}-{abs(hash(text)) % 10_000}",
        post_id="p1",
        author_id=author,
        author_name=author,
        text=text,
        created_at=T0 + timedelta(minutes=minutes),
        likes=likes,
        is_owner=owner,
    )

"""Interfaces for reading comments and publishing posts, plus shared HTTP helpers."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from ..models import Comment, PostMetrics, PostRef

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


class CommentSource(Protocol):
    platform: str

    def fetch(self, post_id: str) -> list[Comment]: ...


class MetricsSource(Protocol):
    def metrics(self, post_id: str) -> PostMetrics: ...


class Publisher(Protocol):
    platform: str

    def publish(self, *, video_path: Path, video_url: str, caption: str, day: int) -> PostRef: ...


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SourceError(f"environment variable {name} is not set")
    return v


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff: float = 2.0,
    **kwargs,
) -> dict:
    """HTTP with retry on 429/5xx/network errors and exponential backoff (2s, 4s, 8s, ...)."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = client.request(method, url, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                raise SourceError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                # Client errors don't get better with retries.
                raise SourceError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:500]}")
            return r.json()
        except (httpx.TransportError, SourceError) as e:
            last = e
            retryable = isinstance(e, httpx.TransportError) or "HTTP 429" in str(e) or "HTTP 5" in str(e)
            if not retryable or attempt == retries:
                break
            wait = backoff * (2**attempt)
            log.warning("retrying in %.0fs: %s", wait, e)
            time.sleep(wait)
    assert last is not None
    raise last


def parse_time(value: str | int | float | None) -> datetime:

    if value is None or value == "":
        return datetime.now(UTC)
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    s = str(value).replace("Z", "+00:00")
    # Graph API uses +0000 (no colon), which fromisoformat accepts on 3.11+.
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

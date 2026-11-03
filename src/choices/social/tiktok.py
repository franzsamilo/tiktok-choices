"""TikTok via the TikTok API for Business (business accounts).

Comments: ``GET {api_base}/business/comment/list/`` with ``business_id`` and
``video_id``, cursor-paginated, max 30 per page, ``Access-Token`` header.
Publish: ``POST {api_base}/business/video/publish/`` with a public
``video_url``, then poll ``business/publish/status/`` for the post id.

Notes from building this:
* TikTok's native poll sticker results are not exposed through the API, which
  is why the series votes in comments. Comments are also the stronger
  ranking signal, so the format helps the algorithm too.
* The API envelope is ``{"code": 0, "message": "OK", "data": {...}}``. A
  non-zero ``code`` is an error even on HTTP 200.
* Field names drift between API versions (``user_id`` is being replaced by
  ``unique_identifier``), so the mapping below tolerates either.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from ..config import TikTokConfig
from ..models import Comment, PostRef
from .base import SourceError, env, parse_time, request_json

log = logging.getLogger(__name__)


class TikTokBusiness:
    platform = "tiktok"

    def __init__(self, cfg: TikTokConfig, client: httpx.Client | None = None):
        self.base = cfg.api_base.rstrip("/")
        self.business_id = env(cfg.business_id_env)
        self.client = client or httpx.Client(timeout=30, headers={"Access-Token": env(cfg.access_token_env)})

    def _call(self, method: str, path: str, **kw) -> dict:
        body = request_json(self.client, method, f"{self.base}/{path}", **kw)
        if body.get("code", 0) != 0:
            raise SourceError(f"TikTok {path}: code={body.get('code')} {body.get('message')} (request_id={body.get('request_id')})")
        return body.get("data") or {}

    # ---- comments ------------------------------------------------------------
    def fetch(self, post_id: str, max_pages: int = 300) -> list[Comment]:
        out: list[Comment] = []
        cursor = 0
        for _ in range(max_pages):
            data = self._call(
                "GET",
                "business/comment/list/",
                params={
                    "business_id": self.business_id,
                    "video_id": post_id,
                    "status": "PUBLIC",
                    "sort_field": "create_time",
                    "sort_order": "asc",
                    "cursor": cursor,
                    "max_count": 30,
                },
            )
            for raw in data.get("comments") or []:
                out.append(self._to_comment(raw, post_id))
                for reply in raw.get("reply_list") or []:
                    out.append(self._to_comment(reply, post_id))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", cursor + 30)
        log.info("tiktok %s: %d comments", post_id, len(out))
        return out

    @staticmethod
    def _to_comment(raw: dict, post_id: str) -> Comment:
        return Comment(
            platform="tiktok",
            id=str(raw.get("comment_id")),
            post_id=post_id,
            author_id=str(raw.get("unique_identifier") or raw.get("user_id") or raw.get("username") or ""),
            author_name=raw.get("username") or raw.get("display_name") or "",
            text=raw.get("text") or "",
            created_at=parse_time(raw.get("create_time")),
            likes=int(raw.get("likes") or 0),
            parent_id=raw.get("parent_comment_id") or None,
            is_owner=bool(raw.get("owner")),
        )

    # ---- publish -------------------------------------------------------------
    def publish(self, *, video_path: Path, video_url: str, caption: str, day: int) -> PostRef:
        data = self._call(
            "POST",
            "business/video/publish/",
            json={
                "business_id": self.business_id,
                "video_url": video_url,
                "post_info": {"caption": caption, "disable_comment": False},
            },
        )
        share_id = data.get("share_id")
        if not share_id:
            raise SourceError(f"TikTok publish returned no share_id: {data}")
        for _ in range(60):  # up to ~5 minutes
            status = self._call(
                "GET", "business/publish/status/", params={"business_id": self.business_id, "publish_id": share_id}
            )
            post_ids = status.get("post_ids") or []
            if post_ids:
                return PostRef(platform="tiktok", post_id=str(post_ids[0]))
            if "FAIL" in str(status.get("status", "")).upper():
                raise SourceError(f"TikTok publish failed: {status.get('reason') or status}")
            time.sleep(5)
        raise SourceError(f"TikTok publish {share_id} did not finish in time; link it later with `choices link`")

"""Instagram via the Instagram Graph API (professional accounts).

Comments: ``GET /{ig-media-id}/comments?fields=id,text,username,timestamp,like_count,replies{...}``,
paginated with ``paging.next``. Reading ``username`` needs the
``instagram_manage_comments`` (Facebook Login) or
``instagram_business_manage_comments`` (Instagram Login) permission. Without
it, every comment falls back to its own id as the voter id, which means no
de-duplication, so check the permission before trusting a tally.

Reels publishing is two steps: create a ``REELS`` media container from a
public ``video_url``, wait for ``status_code == FINISHED``, then
``media_publish``.

As with TikTok, poll/quiz sticker results aren't readable through the API,
so votes come from comments.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from ..config import InstagramConfig
from ..models import Comment, PostRef
from .base import SourceError, env, parse_time, request_json

log = logging.getLogger(__name__)

FIELDS = "id,text,username,timestamp,like_count,from{id,username},replies{id,text,username,timestamp,like_count,from{id,username}}"


class InstagramGraph:
    platform = "instagram"

    def __init__(self, cfg: InstagramConfig, client: httpx.Client | None = None):
        self.base = cfg.graph_base.rstrip("/")
        self.user_id = env(cfg.user_id_env)
        self.token = env(cfg.access_token_env)
        self.client = client or httpx.Client(timeout=30)

    def _get(self, url: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        if "access_token=" not in url:
            p["access_token"] = self.token
        # Passing params={} would make httpx replace the URL's own query string,
        # silently dropping the cursor from a `paging.next` URL.
        body = request_json(self.client, "GET", url, params=p or None)
        if "error" in body:
            raise SourceError(f"Instagram: {body['error']}")
        return body

    # ---- comments ------------------------------------------------------------
    def fetch(self, post_id: str, max_pages: int = 300) -> list[Comment]:
        out: list[Comment] = []
        url = f"{self.base}/{post_id}/comments"
        params: dict | None = {"fields": FIELDS, "limit": 50}
        for _ in range(max_pages):
            body = self._get(url, params)
            for raw in body.get("data", []):
                out.append(self._to_comment(raw, post_id))
                for reply in (raw.get("replies") or {}).get("data", []):
                    out.append(self._to_comment(reply, post_id, parent=raw.get("id")))
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                break
            url, params = nxt, None  # `next` already carries every query param
        log.info("instagram %s: %d comments", post_id, len(out))
        return out

    def _to_comment(self, raw: dict, post_id: str, parent: str | None = None) -> Comment:
        frm = raw.get("from") or {}
        author = frm.get("id") or raw.get("username") or ""
        return Comment(
            platform="instagram",
            id=str(raw["id"]),
            post_id=post_id,
            author_id=str(author),
            author_name=raw.get("username") or frm.get("username") or "",
            text=raw.get("text") or "",
            created_at=parse_time(raw.get("timestamp")),
            likes=int(raw.get("like_count") or 0),
            parent_id=parent,
            is_owner=str(frm.get("id") or "") == self.user_id,
        )

    # ---- publish -------------------------------------------------------------
    def publish(self, *, video_path: Path, video_url: str, caption: str, day: int) -> PostRef:
        create = request_json(
            self.client,
            "POST",
            f"{self.base}/{self.user_id}/media",
            data={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": self.token},
        )
        container = create.get("id")
        if not container:
            raise SourceError(f"Instagram container creation failed: {create}")
        for _ in range(60):  # video processing usually takes 30-90 seconds
            st = self._get(f"{self.base}/{container}", {"fields": "status_code,status"})
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise SourceError(f"Instagram processing {code}: {st.get('status')}")
            time.sleep(5)
        else:
            raise SourceError("Instagram container never finished processing")
        pub = request_json(
            self.client,
            "POST",
            f"{self.base}/{self.user_id}/media_publish",
            data={"creation_id": container, "access_token": self.token},
        )
        media_id = pub.get("id")
        if not media_id:
            raise SourceError(f"Instagram publish failed: {pub}")
        link = self._get(f"{self.base}/{media_id}", {"fields": "permalink"}).get("permalink", "")
        return PostRef(platform="instagram", post_id=str(media_id), url=link)

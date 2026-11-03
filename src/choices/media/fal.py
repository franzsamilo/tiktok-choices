"""fal.ai adapter for image generation and image-to-video, via fal's queue API.

Flow: ``POST https://queue.fal.run/{model}`` with the input JSON, which
returns a ``request_id`` plus ``status_url`` and ``response_url``. Poll the
status until ``COMPLETED``, then GET the response. Auth is
``Authorization: Key $FAL_KEY``.

Model IDs are config (``models.fal_image`` / ``models.fal_video``) because the
best image and video model changes every few weeks. Input field names differ
slightly per model family; the payloads here follow the FLUX and Kling
conventions on fal. Check the model's schema page when you switch families.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from ..social.base import SourceError, env, request_json

log = logging.getLogger(__name__)

QUEUE = "https://queue.fal.run"


class FalClient:
    def __init__(self, client: httpx.Client | None = None, poll_seconds: float = 3.0, timeout_seconds: float = 900):
        self.client = client or httpx.Client(timeout=120, headers={"Authorization": f"Key {env('FAL_KEY')}"})
        self.poll, self.timeout = poll_seconds, timeout_seconds

    def run(self, model: str, payload: dict) -> dict:
        sub = request_json(self.client, "POST", f"{QUEUE}/{model}", json=payload)
        status_url, response_url = sub.get("status_url"), sub.get("response_url")
        if not status_url or not response_url:
            raise SourceError(f"fal: unexpected submit response: {sub}")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            st = request_json(self.client, "GET", status_url)
            status = st.get("status")
            if status == "COMPLETED":
                if st.get("error"):
                    raise SourceError(f"fal {model}: {st['error']}")
                return request_json(self.client, "GET", response_url)
            if status not in ("IN_QUEUE", "IN_PROGRESS"):
                raise SourceError(f"fal {model}: unexpected status {st}")
            time.sleep(self.poll)
        raise SourceError(f"fal {model}: timed out after {self.timeout}s (request {sub.get('request_id')})")

    def download(self, url: str, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with out.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        return out


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


class FalImages:
    name = "fal"

    def __init__(self, model: str, client: FalClient | None = None):
        self.model = model
        self._client = client

    @property
    def client(self) -> FalClient:
        if self._client is None:
            self._client = FalClient()
        return self._client

    def generate(self, prompt: str, negative: str, out: Path, *, seed: int, width: int, height: int) -> Path:
        # Generate near 9:16 at a size the model handles natively; the renderer scales/crops.
        payload = {
            "prompt": prompt,
            "image_size": {"width": 768, "height": 1344} if height > width else {"width": 1344, "height": 768},
            "num_images": 1,
            "seed": seed,
            "enable_safety_checker": True,
        }
        if negative and "flux" not in self.model:  # FLUX ignores negative prompts
            payload["negative_prompt"] = negative
        res = self.client.run(self.model, payload)
        images = res.get("images") or []
        if not images:
            raise SourceError(f"fal {self.model}: no images in response")
        if (res.get("has_nsfw_concepts") or [False])[0]:
            raise SourceError(f"fal {self.model}: safety checker flagged the image; revise the scene's visual")
        return self.client.download(images[0]["url"], out)


class FalVideo:
    name = "fal"

    def __init__(self, model: str, negative: str = "", client: FalClient | None = None):
        self.model, self.negative = model, negative
        self._client = client

    @property
    def client(self) -> FalClient:
        if self._client is None:
            self._client = FalClient()
        return self._client

    def animate(self, image: Path, prompt: str, out: Path, *, seconds: float) -> Path:
        payload = {
            "prompt": prompt,
            "image_url": _data_uri(image),
            "duration": "10" if seconds > 5.5 else "5",
        }
        if self.negative:
            payload["negative_prompt"] = self.negative
        res = self.client.run(self.model, payload)
        video = res.get("video") or {}
        if not video.get("url"):
            raise SourceError(f"fal {self.model}: no video in response")
        return self.client.download(video["url"], out)

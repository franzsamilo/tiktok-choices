"""fal.ai adapter for image generation and image-to-video, via fal's queue API.

Flow: ``POST https://queue.fal.run/{model}`` with the input JSON, which
returns a ``request_id`` plus ``status_url`` and ``response_url``. Poll the
status until ``COMPLETED``, then GET the response. Auth is
``Authorization: Key $FAL_KEY``.

Defaults (checked 2026-11-04, see docs/MODELS.md):

* Images: Nano Banana 2 (``fal-ai/nano-banana-2``), with its ``/edit`` variant
  for any shot that has a cast member. The edit endpoint accepts up to 14
  ``image_urls``, so every shot is conditioned on the characters' reference
  sheets instead of hoping a text description lands the same face twice.
* Video: Kling v3 Pro image-to-video with ``generate_audio: false``. Native
  audio bills about 1.5x per second, and the series has its own narration.
  ``duration`` is a whole number of seconds from 3 to 15, so each clip is sized
  to its shot rather than padded to a fixed 5s or 10s.

Payload shapes differ per model family; the legacy FLUX/Kling-v2 shapes are
kept so older model IDs still work.
"""

from __future__ import annotations

import base64
import logging
import math
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


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _aspect(width: int, height: int) -> str:
    ratio = width / height
    options = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9, "2:3": 2 / 3, "3:4": 3 / 4}
    return min(options, key=lambda k: abs(options[k] - ratio))


class FalImages:
    name = "fal"

    def __init__(self, model: str, edit_model: str = "", client: FalClient | None = None, max_refs: int = 14):
        self.model, self.edit_model, self.max_refs = model, edit_model, max_refs
        self._client = client

    @property
    def client(self) -> FalClient:
        if self._client is None:
            self._client = FalClient()
        return self._client

    def model_id(self, has_refs: bool) -> str:
        return self.edit_model if has_refs and self.edit_model else self.model

    def payload(self, model: str, prompt: str, negative: str, *, seed: int, width: int, height: int, refs: list[Path]) -> dict:
        if "flux" in model and "/edit" not in model:
            # Legacy FLUX text-to-image shape (no negative prompt support).
            return {
                "prompt": prompt,
                "image_size": {"width": 768, "height": 1344} if height > width else {"width": 1344, "height": 768},
                "num_images": 1,
                "seed": seed,
                "enable_safety_checker": True,
            }
        # Nano Banana family (Gemini image models on fal): no negative prompt
        # field, so the constraints are folded into the prompt itself.
        p: dict = {
            "prompt": f"{prompt}. Avoid: {negative}" if negative else prompt,
            "aspect_ratio": _aspect(width, height),
            "resolution": "1K",
            "num_images": 1,
            "seed": seed,
            "output_format": "png",
        }
        if refs:
            p["image_urls"] = [data_uri(r) for r in refs[: self.max_refs]]
        return p

    def generate(
        self, prompt: str, negative: str, out: Path, *, seed: int, width: int, height: int, refs: list[Path] | None = None
    ) -> Path:
        refs = list(refs or [])
        model = self.model_id(bool(refs))
        res = self.client.run(model, self.payload(model, prompt, negative, seed=seed, width=width, height=height, refs=refs if model == self.edit_model else []))
        images = res.get("images") or []
        if not images:
            raise SourceError(f"fal {model}: no images in response")
        if (res.get("has_nsfw_concepts") or [False])[0]:
            raise SourceError(f"fal {model}: safety checker flagged the image; revise the scene's visual")
        return self.client.download(images[0]["url"], out)


class FalVideo:
    name = "fal"

    def __init__(self, model: str, negative: str = "", audio: bool = False, client: FalClient | None = None,
                 min_seconds: int = 3, max_seconds: int = 15):
        self.model, self.negative, self.audio = model, negative, audio
        self.min_seconds, self.max_seconds = min_seconds, max_seconds
        self._client = client

    @property
    def client(self) -> FalClient:
        if self._client is None:
            self._client = FalClient()
        return self._client

    def billed_seconds(self, seconds: float) -> int:
        if "kling-video/v3" in self.model or "kling-video/o3" in self.model:
            return max(self.min_seconds, min(self.max_seconds, math.ceil(seconds)))
        return 10 if seconds > 5.5 else 5  # older models only take 5 or 10

    def payload(self, image: Path, prompt: str, seconds: float) -> dict:
        dur = str(self.billed_seconds(seconds))
        if "kling-video/v3" in self.model or "kling-video/o3" in self.model:
            p = {"prompt": prompt, "start_image_url": data_uri(image), "duration": dur, "generate_audio": self.audio}
        else:
            p = {"prompt": prompt, "image_url": data_uri(image), "duration": dur}
        if self.negative:
            p["negative_prompt"] = self.negative
        return p

    def animate(self, image: Path, prompt: str, out: Path, *, seconds: float) -> Path:
        res = self.client.run(self.model, self.payload(image, prompt, seconds))
        video = res.get("video") or {}
        if not video.get("url"):
            raise SourceError(f"fal {self.model}: no video in response")
        return self.client.download(video["url"], out)

"""Thin wrapper around the Anthropic SDK used by the writer, critic and vote classifier.

Every call goes through ``structured()`` so the whole pipeline gets the same
behaviour for free:

* structured outputs via ``messages.parse`` (a Pydantic model in, a validated
  instance out; no regex-scraping JSON out of prose),
* adaptive thinking with an explicit effort level per purpose,
* server-side refusal fallback on models that support it,
* a cached system prompt (the story bible is identical across calls),
* token usage recorded on the episode so cost per episode is visible.

``user`` may be a plain string or a list of content blocks (text + images),
which is how the frame judge shows Claude the generated still next to the
character reference sheets.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from .models import LLMCall

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Models that accept the server-side `fallbacks: "default"` refusal routing.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    def __init__(self, purpose: str, category: str | None, explanation: str | None):
        super().__init__(f"{purpose}: model declined (category={category}): {explanation or ''}".strip())
        self.category = category


class Claude:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        # Lazy so that offline runs never need credentials.
        if self._client is None:
            self._client = anthropic.Anthropic(max_retries=4)
        return self._client

    def structured(
        self,
        *,
        purpose: str,
        model: str,
        effort: str,
        system: str,
        user: str | list[dict],
        output: type[T],
        max_tokens: int = 16000,
    ) -> tuple[T, LLMCall]:
        kwargs: dict = {}
        if model in _FALLBACK_MODELS:
            kwargs = {"betas": [_FALLBACK_BETA], "fallbacks": "default"}

        try:
            resp = self.client.beta.messages.parse(
                model=model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=output,
                **kwargs,
            )
        except TypeError as e:
            if "authentication" not in str(e).lower():
                raise
            raise LLMError(
                "no Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`), or pass --offline"
            ) from e

        usage = resp.usage
        call = LLMCall(
            purpose=purpose,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            served_by=getattr(resp, "model", model) or model,
        )
        log.info(
            "%s: %s in / %s out tokens (cache read %s) via %s",
            purpose, call.input_tokens, call.output_tokens, call.cache_read_tokens, call.served_by,
        )

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(purpose, getattr(details, "category", None), getattr(details, "explanation", None))
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: hit max_tokens={max_tokens} before finishing; raise it or shorten the brief")

        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError(f"{purpose}: response did not contain a parsable {output.__name__}")
        return parsed, call


def image_block(path: Path, max_side: int = 768) -> dict:
    """An image content block, downscaled first: ~768px is plenty to judge a frame and cuts tokens ~4x."""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}

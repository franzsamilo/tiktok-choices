"""Provider interfaces for images, image-to-video and voice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class WordTiming(BaseModel):
    word: str
    start: float
    end: float


class VoiceResult(BaseModel):
    path: str
    duration: float
    words: list[WordTiming]

    def save(self, sidecar: Path) -> None:
        sidecar.write_text(self.model_dump_json(indent=1), encoding="utf-8")

    @classmethod
    def load(cls, sidecar: Path) -> VoiceResult | None:
        if not sidecar.exists():
            return None
        return cls.model_validate(json.loads(sidecar.read_text(encoding="utf-8")))


class ImageProvider(Protocol):
    name: str

    def model_id(self, has_refs: bool) -> str: ...

    def generate(
        self, prompt: str, negative: str, out: Path, *, seed: int, width: int, height: int, refs: list[Path] | None = None
    ) -> Path: ...


class VideoProvider(Protocol):
    name: str
    model: str
    audio: bool

    def billed_seconds(self, seconds: float) -> int: ...

    def animate(self, image: Path, prompt: str, out: Path, *, seconds: float) -> Path: ...


class VoiceProvider(Protocol):
    name: str
    model: str

    def synthesize(self, text: str, out: Path) -> VoiceResult: ...

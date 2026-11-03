"""ElevenLabs text-to-speech with character-level timestamps.

``POST /v1/text-to-speech/{voice_id}/with-timestamps`` returns the audio as
base64 plus an ``alignment`` block with per-character start/end times. Those
are folded into word timings here, which drive the word-by-word captions.
Captions then match the voice exactly, with no forced-alignment step.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..render.ffmpeg import probe_duration
from ..social.base import SourceError, env, request_json
from .base import VoiceResult, WordTiming

API = "https://api.elevenlabs.io/v1"


def words_from_alignment(alignment: dict) -> list[WordTiming]:
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words: list[WordTiming] = []
    buf, w_start, w_end = "", None, 0.0
    for ch, s, e in zip(chars, starts, ends, strict=False):
        if ch.isspace():
            if buf:
                words.append(WordTiming(word=buf, start=w_start or 0.0, end=w_end))
            buf, w_start = "", None
            continue
        if w_start is None:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        words.append(WordTiming(word=buf, start=w_start or 0.0, end=w_end))
    return words


class ElevenLabsVoice:
    name = "elevenlabs"

    def __init__(self, voice_id: str, model: str, client: httpx.Client | None = None):
        self.voice_id, self.model = voice_id, model
        self._client = client

    @property
    def client(self) -> httpx.Client:
        # Lazy, so commands that never synthesize (status, approve) don't need the key.
        if self._client is None:
            self._client = httpx.Client(timeout=120, headers={"xi-api-key": env("ELEVENLABS_API_KEY")})
        return self._client

    def synthesize(self, text: str, out: Path) -> VoiceResult:
        if not self.voice_id:
            raise SourceError("models.elevenlabs_voice_id is empty; pick a narrator voice in config/pipeline.yaml")
        body = request_json(
            self.client,
            "POST",
            f"{API}/text-to-speech/{self.voice_id}/with-timestamps",
            params={"output_format": "mp3_44100_128"},
            json={
                "text": text,
                "model_id": self.model,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25},
            },
        )
        audio = body.get("audio_base64")
        if not audio:
            raise SourceError("ElevenLabs returned no audio")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(audio))
        words = words_from_alignment(body.get("alignment") or body.get("normalized_alignment") or {})
        duration = probe_duration(out)
        return VoiceResult(path=str(out), duration=duration, words=words)

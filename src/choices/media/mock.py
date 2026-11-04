"""Offline stand-ins: procedural key art and silent narration with estimated timing.

The mock frames are intentionally stylised (a moody, rainy Manila skyline with
motionless figures) rather than grey boxes, so a fully offline render still
reads like the show and the layout, captions and pacing can be judged.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..render.ffmpeg import run
from ..render.fonts import font_path
from .base import VoiceResult, WordTiming

PALETTES = {
    "night": ((8, 14, 30), (28, 46, 74), (255, 196, 120)),
    "dawn": ((26, 52, 64), (164, 120, 92), (255, 210, 150)),
    "rain": ((18, 34, 42), (82, 104, 110), (255, 200, 130)),
    "candle": ((22, 12, 8), (90, 50, 24), (255, 170, 80)),
    "noon": ((54, 62, 66), (150, 156, 152), (255, 230, 190)),
    "dusk": ((24, 20, 40), (150, 70, 60), (255, 170, 110)),
}
KEYS = [
    ("candle", ("candle", "altar", "lamp")),
    ("night", ("night", "dark", "midnight", "shadow")),
    ("dusk", ("dusk", "sunset", "evening")),
    ("dawn", ("dawn", "morning", "sunrise")),
    ("noon", ("noon", "midday", "afternoon")),
    ("rain", ("rain", "flood", "wet", "storm")),
]


def _palette(prompt: str) -> tuple:
    p = prompt.lower()
    for name, words in KEYS:
        if any(w in p for w in words):
            return PALETTES[name]
    return PALETTES["rain"]


class MockImages:
    """Procedural stand-in. ``model``/``edit_model`` only name what it stands in for (for simulated pricing)."""

    name = "mock"

    def __init__(self, model: str = "mock", edit_model: str = "mock"):
        self.model, self.edit_model = model, edit_model

    def model_id(self, has_refs: bool) -> str:
        return self.edit_model if has_refs else self.model

    def generate(
        self, prompt: str, negative: str, out: Path, *, seed: int, width: int, height: int, refs: list[Path] | None = None
    ) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        rng = random.Random(seed)
        w, h = width, height
        top, bottom, light = _palette(prompt)
        p = prompt.lower()

        # Sky gradient.
        t = np.linspace(0, 1, h)[:, None]
        sky = np.zeros((h, w, 3), dtype=np.float32)
        for c in range(3):
            sky[..., c] = top[c] * (1 - t) + bottom[c] * t
        img = Image.fromarray(sky.clip(0, 255).astype(np.uint8), "RGB")
        d = ImageDraw.Draw(img, "RGBA")

        # Two layers of skyline with lit windows.
        horizon = int(h * rng.uniform(0.52, 0.62))
        for layer, shade in ((0, 0.55), (1, 0.25)):
            x = -20
            base = horizon + layer * int(h * 0.06)
            while x < w:
                bw = rng.randint(int(w * 0.08), int(w * 0.22))
                bh = rng.randint(int(h * 0.10), int(h * 0.32))
                col = tuple(int(v * shade) for v in top)
                d.rectangle([x, base - bh, x + bw, h], fill=col + (255,))
                if layer == 0:
                    for wy in range(base - bh + 14, base - 10, 26):
                        for wx in range(x + 10, x + bw - 10, 22):
                            if rng.random() < 0.07:
                                d.rectangle([wx, wy, wx + 8, wy + 12], fill=light + (190,))
                x += bw + rng.randint(-10, 18)

        # Sagging power lines.
        for _ in range(rng.randint(3, 5)):
            y0, y1 = rng.randint(int(h * 0.18), int(h * 0.45)), rng.randint(int(h * 0.18), int(h * 0.45))
            sag = rng.randint(30, 120)
            pts = [(x, y0 + (y1 - y0) * x / w + sag * math.sin(math.pi * x / w)) for x in range(0, w + 1, 12)]
            d.line(pts, fill=(10, 10, 12, 220), width=3)

        # Street and motionless figures (the Tulog).
        d.rectangle([0, int(h * 0.80), w, h], fill=tuple(int(v * 0.2) for v in top) + (255,))
        crowd = 14 if any(k in p for k in ("crowd", "dozen", "silhouettes", "motionless")) else rng.randint(3, 7)
        for _ in range(crowd):
            depth = rng.uniform(0.3, 1.0)
            fx = rng.randint(0, w)
            fy = int(h * 0.80 + depth * h * 0.12)
            fh = int(h * 0.12 * depth)
            fw = int(fh * 0.28)
            col = (6, 8, 10, int(160 + 90 * depth))
            d.rounded_rectangle([fx - fw // 2, fy - fh, fx + fw // 2, fy], radius=fw // 3, fill=col)
            d.ellipse([fx - fw // 3, fy - fh - fw * 0.75, fx + fw // 3, fy - fh + fw * 0.1], fill=col)

        # Rain, fog, glow.
        if any(k in p for k in ("rain", "wet", "storm", "flood")):
            rain = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            rd = ImageDraw.Draw(rain)
            for _ in range(int(w * h / 2600)):
                x, y = rng.randint(0, w), rng.randint(0, h)
                ln = rng.randint(18, 46)
                rd.line([(x, y), (x - ln * 0.18, y + ln)], fill=(200, 220, 230, rng.randint(40, 110)), width=1)
            img = Image.alpha_composite(img.convert("RGBA"), rain).convert("RGB")
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        arr = np.asarray(img).astype(np.float32)
        fog = np.linspace(0, 1, h)[:, None, None]
        fog = np.exp(-((fog - horizon / h) ** 2) / 0.01) * 0.35
        arr = arr * (1 - fog) + np.array(bottom, dtype=np.float32) * fog

        # Vignette + film grain.
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        arr *= (1 - 0.55 * np.clip(r - 0.35, 0, 1) ** 1.5)[..., None]
        grain = np.random.default_rng(seed).normal(0, 7, (h, w, 1))
        arr = (arr + grain).clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr, "RGB")

        # Honest label so nobody mistakes a mock frame for generated art.
        d = ImageDraw.Draw(img, "RGBA")
        f = ImageFont.truetype(font_path("semibold"), max(14, w // 48))
        label = "OFFLINE PREVIEW FRAME"
        d.text((int(w * 0.06), int(h * 0.06)), label, font=f, fill=(255, 255, 255, 110))
        img.save(out)
        return out


def _word_weights(words: list[str]) -> list[float]:
    out = []
    for wd in words:
        wt = 0.18 + 0.055 * len(re.sub(r"\W", "", wd))
        if wd.endswith((",", ";", ":")):
            wt += 0.22
        if wd.endswith((".", "!", "?", '."', '?"', '!"')):
            wt += 0.42
        out.append(wt)
    return out


class MockVoice:
    """Silent audio with plausible word timings, so captions and pacing render exactly as with real TTS."""

    name = "mock"

    def __init__(self, words_per_second: float = 2.6, model: str = "mock"):
        self.wps, self.model = words_per_second, model

    def synthesize(self, text: str, out: Path) -> VoiceResult:
        out.parent.mkdir(parents=True, exist_ok=True)
        words = text.split()
        weights = _word_weights(words)
        target = max(0.8, len(words) / self.wps)
        scale = target / sum(weights) if weights else 1.0
        timings, t = [], 0.05
        for wd, wt in zip(words, weights, strict=False):
            dur = wt * scale
            speak = min(dur, (0.18 + 0.055 * len(wd)) * scale)
            timings.append(WordTiming(word=wd, start=round(t, 3), end=round(t + speak, 3)))
            t += dur
        duration = round(t + 0.1, 3)
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{duration}", "-q:a", "9", str(out)])
        return VoiceResult(path=str(out), duration=duration, words=timings)


def stable_seed(*parts: object) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class MockVideo:
    """Offline image-to-video: a faster, handheld-feeling move so hero shots visibly differ from Ken Burns stills."""

    name = "mock"

    def __init__(self, model: str = "mock", audio: bool = False, min_seconds: int = 3, max_seconds: int = 15,
                 width: int = 1080, height: int = 1920, fps: int = 30):
        self.model, self.audio = model, audio
        self.min_seconds, self.max_seconds = min_seconds, max_seconds
        self.width, self.height, self.fps = width, height, fps

    def billed_seconds(self, seconds: float) -> int:
        return max(self.min_seconds, min(self.max_seconds, math.ceil(seconds)))

    def animate(self, image: Path, prompt: str, out: Path, *, seconds: float) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        w, h, fps = self.width, self.height, self.fps
        frames = max(1, round(seconds * fps))
        z = f"1.0+0.22*on/{frames}"
        x = f"iw/2-iw/zoom/2+{w // 90}*sin(on/5)"
        y = f"ih/2-ih/zoom/2+{h // 160}*cos(on/7)"
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},eq=contrast=1.06,format=yuv420p")
        run(["ffmpeg", "-y", "-i", str(image), "-vf", vf, "-frames:v", str(frames),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", str(out)])
        return out

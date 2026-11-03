"""Assemble the final vertical video with ffmpeg.

Timeline: each scene lasts as long as its narration plus a short breath
(never under ``min_scene_seconds``), so picture and voice can't drift apart.
The choice card follows, with the question read aloud.

    [scene 1 ......][scene 2 ....][scene 3 .......] ... [choice card ....]
     hook overlay                                         question + A/B
     word-by-word captions ------------------------->
     narration per scene, ambient bed under everything, stinger on the card

Scenes with an image-to-video clip use the clip (looped or trimmed to fit);
otherwise the still gets a slow Ken Burns move, varied per scene so cuts feel
intentional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import RenderConfig
from ..media.base import VoiceResult, WordTiming
from ..models import EpisodeScript, SceneTiming
from . import cards
from .captions import build_ass
from .ffmpeg import RenderError, filter_path, require_ffmpeg, run
from .fonts import FONTS_DIR

log = logging.getLogger(__name__)

# Ken Burns moves as (zoom_from, zoom_to, x_from, x_to, y_from, y_to) in relative units.
MOVES = [
    (1.00, 1.12, 0.5, 0.5, 0.5, 0.45),  # push in, drift up
    (1.14, 1.02, 0.5, 0.5, 0.5, 0.5),   # pull out
    (1.10, 1.10, 0.38, 0.62, 0.5, 0.5),  # pan right
    (1.06, 1.16, 0.55, 0.45, 0.55, 0.5),  # push in, drift left
    (1.10, 1.10, 0.62, 0.38, 0.48, 0.52),  # pan left
]


@dataclass
class SceneInput:
    image: Path
    clip: Path | None
    voice: VoiceResult


@dataclass
class RenderResult:
    video: Path
    cover: Path
    timings: list[SceneTiming]
    duration: float


def _ken_burns(image: Path, out: Path, seconds: float, move: tuple, cfg: RenderConfig) -> None:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    frames = max(1, round(seconds * fps))
    z0, z1, x0, x1, y0, y1 = move
    # Supersample 2x before zoompan: it rounds positions to whole pixels, and
    # without headroom slow moves visibly judder.
    W, H = w * 2, h * 2
    p = f"(on/{frames})"
    z = f"{z0}+({z1}-{z0})*{p}"
    x = f"max(0,min(iw-iw/zoom,({x0}+({x1}-{x0})*{p})*iw-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,({y0}+({y1}-{y0})*{p})*ih-ih/zoom/2))"
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(out),
    ])


def _clip_segment(clip: Path, out: Path, seconds: float, cfg: RenderConfig) -> None:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},setsar=1,format=yuv420p"
    run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{seconds:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(out),
    ])


def _card_segment(backdrop: Path, overlay: Path, out: Path, seconds: float, cfg: RenderConfig) -> None:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    frames = max(1, round(seconds * fps))
    fc = (
        f"[0:v]scale={w * 2}:{h * 2},zoompan=z='1+0.04*on/{frames}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
        f"d={frames}:s={w}x{h}:fps={fps}[bg];"
        f"[1:v]format=rgba,fade=in:st=0:d=0.3:alpha=1[ov];"
        f"[bg][ov]overlay=0:0:format=auto,setsar=1,format=yuv420p[v]"
    )
    run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(backdrop), "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(overlay),
        "-filter_complex", fc, "-map", "[v]", "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(out),
    ])


def _audio_mix(
    voices: list[tuple[Path, float]], total: float, card_start: float, out: Path, cfg: RenderConfig
) -> None:
    """Narration clips at their offsets, an ambient bed under everything, and a stinger on the card."""
    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    for i, (path, offset) in enumerate(voices):
        inputs += ["-i", str(path)]
        ms = int(offset * 1000)
        chains.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{i}]")
        labels.append(f"[v{i}]")

    n = len(voices)
    if cfg.music_path:
        inputs += ["-stream_loop", "-1", "-i", cfg.music_path]
        chains.append(
            f"[{n}:a]aresample=44100,aformat=channel_layouts=stereo,atrim=0:{total:.3f},"
            f"volume={cfg.music_volume_db}dB,afade=t=out:st={max(0, total - 1.5):.3f}:d=1.5[bed]"
        )
    else:
        # Generated drone: filtered brown noise + a low, slowly beating pair of sines.
        inputs += [
            "-f", "lavfi", "-t", f"{total:.3f}", "-i", "anoisesrc=color=brown:amplitude=0.6:seed=7",
            "-f", "lavfi", "-t", f"{total:.3f}", "-i", "sine=f=55:sample_rate=44100",
            "-f", "lavfi", "-t", f"{total:.3f}", "-i", "sine=f=55.7:sample_rate=44100",
        ]
        chains.append(
            f"[{n}:a]lowpass=f=320,highpass=f=40[nz];[{n + 1}:a][{n + 2}:a]amix=inputs=2:normalize=0,volume=0.5[hum];"
            f"[nz][hum]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo,"
            f"volume={cfg.music_volume_db}dB,afade=t=in:d=1.0,afade=t=out:st={max(0, total - 1.5):.3f}:d=1.5[bed]"
        )
    # Stinger: a low thump under the choice card.
    inputs += ["-f", "lavfi", "-t", "1.6", "-i", "sine=f=48:sample_rate=44100"]
    sm = int(card_start * 1000)
    chains.append(
        f"[{n + (1 if cfg.music_path else 3)}:a]afade=t=in:d=0.02,afade=t=out:st=0.08:d=1.4,volume=4dB,"
        f"aformat=channel_layouts=stereo,adelay={sm}|{sm}[sting]"
    )
    labels += ["[bed]", "[sting]"]
    chains.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
        f"atrim=0:{total:.3f},loudnorm=I={cfg.target_lufs}:TP=-1.5:LRA=11[a]"
    )
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chains), "-map", "[a]", "-ar", "44100", "-c:a", "aac", "-b:a", "192k", str(out)])


def render_episode(
    *,
    script: EpisodeScript,
    day: int,
    series_title: str,
    scenes: list[SceneInput],
    cta_voice: VoiceResult,
    cfg: RenderConfig,
    out_dir: Path,
    window_hours: float = 24,
) -> RenderResult:
    require_ffmpeg()
    if len(scenes) != len(script.scenes):
        raise RenderError(f"{len(scenes)} media inputs for {len(script.scenes)} scenes")
    work = out_dir / "render"
    work.mkdir(parents=True, exist_ok=True)

    # 1. Timeline driven by narration length.
    timings: list[SceneTiming] = []
    t = 0.0
    for i, sc in enumerate(scenes):
        dur = round(max(cfg.min_scene_seconds, sc.voice.duration + cfg.scene_padding_seconds), 3)
        timings.append(SceneTiming(index=i, start=round(t, 3), duration=dur))
        t += dur
    card_start = round(t, 3)
    card_dur = round(max(cfg.choice_card_seconds, cta_voice.duration + 0.8), 3)
    total = round(card_start + card_dur, 3)

    # 2. Picture segments.
    segs: list[Path] = []
    for i, (sc, tm) in enumerate(zip(scenes, timings, strict=False)):
        seg = work / f"seg{i + 1:02d}.mp4"
        if sc.clip and sc.clip.exists():
            _clip_segment(sc.clip, seg, tm.duration, cfg)
        else:
            _ken_burns(sc.image, seg, tm.duration, MOVES[i % len(MOVES)], cfg)
        segs.append(seg)

    overlay = cards.choice_card(script, day, cfg.width, cfg.height, work / "card.png", window_hours)
    backdrop = cards.blurred_backdrop(scenes[-1].image, cfg.width, cfg.height, work / "card_bg.png")
    card_seg = work / "seg_card.mp4"
    _card_segment(backdrop, overlay, card_seg, card_dur, cfg)
    segs.append(card_seg)

    listing = work / "segments.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in segs), encoding="utf-8")
    picture = work / "picture.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", picture.name], cwd=work)

    # 3. Sound.
    voices = [(Path(sc.voice.path), tm.start + 0.12) for sc, tm in zip(scenes, timings, strict=False)]
    voices.append((Path(cta_voice.path), card_start + 0.35))
    audio = work / "mix.m4a"
    _audio_mix(voices, total, card_start, audio, cfg)

    # 4. Captions (scene narration only; the card shows the question as text).
    words: list[WordTiming] = []
    for sc, tm in zip(scenes, timings, strict=False):
        off = tm.start + 0.12
        words += [WordTiming(word=w.word, start=w.start + off, end=w.end + off) for w in sc.voice.words]
    ass = work / "captions.ass"
    ass.write_text(
        build_ass(
            width=cfg.width,
            height=cfg.height,
            words=words,
            hook=script.hook,
            hook_seconds=cfg.hook_seconds,
            badge=f"{series_title.upper()} · DAY {day}",
            words_per_chunk=cfg.caption_words_per_chunk,
            caption_until=card_start,
        ),
        encoding="utf-8",
    )

    # 5. Final encode: burn captions, mux audio, web-optimised.
    final = out_dir / "final.mp4"
    vf = f"subtitles=filename={filter_path(ass)}:fontsdir={filter_path(FONTS_DIR)}"
    run([
        "ffmpeg", "-y", "-i", str(picture), "-i", str(audio), "-vf", vf,
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(cfg.fps), "-c:a", "copy", "-t", f"{total:.3f}",
        "-movflags", "+faststart", str(final),
    ])

    cover = cards.cover(scenes[0].image, script, day, series_title, cfg.width, cfg.height, out_dir / "cover.jpg")
    log.info("rendered %s (%.1fs)", final, total)
    return RenderResult(video=final, cover=cover, timings=timings, duration=total)

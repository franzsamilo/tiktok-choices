"""Word-by-word "karaoke" captions as an ASS subtitle file, burned in by ffmpeg/libass.

Short chunks (about 3 words) with the spoken word highlighted are the native
caption style on short-form video. Most people watch muted, so captions
are the narration for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..media.base import WordTiming
from .fonts import ASS_BLACK

YELLOW = "&H0033D6FF&"  # ASS colours are &HAABBGGRR
WHITE = "&H00FFFFFF&"
AMBER = "&H005CC4FF&"


@dataclass
class Chunk:
    words: list[WordTiming]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[WordTiming], max_words: int = 3, max_chars: int = 18) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[WordTiming] = []
    for w in words:
        if cur and (len(cur) >= max_words or len(" ".join(x.word for x in cur + [w])) > max_chars):
            chunks.append(Chunk(cur))
            cur = []
        cur.append(w)
        if re.search(r"[.!?,;:]['\"”’]?$", w.word):
            chunks.append(Chunk(cur))
            cur = []
    if cur:
        chunks.append(Chunk(cur))
    return chunks


def build_ass(
    *,
    width: int,
    height: int,
    words: list[WordTiming],
    hook: str,
    hook_seconds: float,
    badge: str,
    words_per_chunk: int = 3,
    caption_until: float | None = None,
) -> str:
    s = min(width / 1080, height / 1350)  # tighter dimension, so 4:5 and 1:1 cuts scale down
    fs = int(84 * s)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{ASS_BLACK},{fs},{WHITE},{WHITE},&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,{max(3, int(7 * s))},{max(1, int(3 * s))},5,0,0,0,1
Style: Hook,{ASS_BLACK},{int(92 * s)},{WHITE},{WHITE},&H00141210&,&H00141210&,0,0,0,0,100,100,0,0,3,{int(26 * s)},0,7,0,0,0,1
Style: Badge,{ASS_BLACK},{int(40 * s)},{AMBER},{AMBER},&H00000000&,&H00000000&,0,0,0,0,100,100,6,0,1,{max(2, int(4 * s))},0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    cx, cy = width // 2, int(height * 0.655)

    chunks = chunk_words(words, max_words=words_per_chunk)
    for ci, chunk in enumerate(chunks):
        texts = [_esc(w.word.upper()) for w in chunk.words]
        # Hold a chunk until the next one starts if the pause is short (no
        # blinking between phrases), but never let two chunks overlap.
        nxt = chunks[ci + 1].start if ci + 1 < len(chunks) else None
        hold = chunk.end + 0.12
        if nxt is not None:
            hold = nxt if nxt - chunk.end < 0.6 else min(hold, nxt)
        for i, w in enumerate(chunk.words):
            start = chunk.start if i == 0 else w.start
            end = chunk.words[i + 1].start if i + 1 < len(chunk.words) else hold
            if caption_until is not None:
                end = min(end, caption_until)
            if end <= start:
                continue
            parts = [
                (f"{{\\c{YELLOW}\\fscx108\\fscy108}}{t}{{\\c{WHITE}\\fscx100\\fscy100}}" if j == i else t)
                for j, t in enumerate(texts)
            ]
            events.append(
                f"Dialogue: 1,{_t(start)},{_t(end)},Caption,,0,0,0,,{{\\pos({cx},{cy})}}" + " ".join(parts)
            )

    if hook:
        left = int(80 * s)
        top = int(height * 0.13)
        events.append(f"Dialogue: 2,{_t(0)},{_t(hook_seconds)},Badge,,0,0,0,,{{\\pos({left},{top})\\fad(120,200)}}{_esc(badge)}")
        hook_text = _esc(hook.upper())
        # Manual wrap to ~16 chars per line keeps the boxed hook compact.
        lines, cur = [], ""
        for word in hook_text.split():
            if cur and len(cur) + 1 + len(word) > 16:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            lines.append(cur)
        events.append(
            f"Dialogue: 2,{_t(0.05)},{_t(hook_seconds)},Hook,,0,0,0,,"
            f"{{\\pos({left + int(26 * s)},{top + int(78 * s)})\\fad(120,200)}}" + "\\N".join(lines)
        )
    return header + "\n".join(events) + "\n"

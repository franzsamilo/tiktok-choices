"""Small ffmpeg/ffprobe helpers with readable errors."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class RenderError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RenderError(f"{tool} not found on PATH. Install ffmpeg (brew install ffmpeg / apt install ffmpeg).")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    if cmd and cmd[0] == "ffmpeg" and "-loglevel" not in cmd:
        cmd = [cmd[0], "-hide_banner", "-loglevel", "error", *cmd[1:]]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RenderError(f"{' '.join(cmd[:3])} ... failed ({proc.returncode}):\n{tail}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError as e:
        raise RenderError(f"could not read duration of {path}: {proc.stderr.strip()}") from e


def filter_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option value."""
    s = str(p.resolve()).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")

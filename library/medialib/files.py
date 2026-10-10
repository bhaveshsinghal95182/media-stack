"""Local video and subtitle files: finding them and reading episode numbers from names."""

from __future__ import annotations

import os
import re

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".webm", ".mov", ".wmv",
              ".mpg", ".mpeg"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
SXXEXX = re.compile(r"[Ss](\d{1,2})[ ._-]?((?:[Ee]\d{1,3}[ ._-]?)+)")


def ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def walk(src: str) -> list[str]:
    """Every file under a folder (sorted), or the file itself."""
    if os.path.isfile(src):
        return [src]
    out = []
    for d, dirs, files in os.walk(src):
        dirs.sort()
        out += [os.path.join(d, f) for f in sorted(files)]
    return out


def local_videos(src: str) -> list[str]:
    return [p for p in walk(src) if ext(p) in VIDEO_EXTS
            and "sample" not in os.path.basename(p).lower()]


def local_subs(src: str, video: str, whole_dir: bool) -> list[tuple[str, str]]:
    """Subtitle files for a video, as (path, suffix after the video name).

    Sidecars named like the video ("Movie.en.srt") keep their suffix. When a
    whole folder is imported for one video, every subtitle in it comes along,
    e.g. "Subs/2_English.srt" -> suffix ".2_English".
    """
    stem = os.path.splitext(os.path.basename(video))[0]
    pool = walk(src) if whole_dir else walk(os.path.dirname(video) or ".")
    out = []
    for p in pool:
        if ext(p) not in SUB_EXTS:
            continue
        sub_stem = os.path.splitext(os.path.basename(p))[0]
        if sub_stem.startswith(stem):
            out.append((p, sub_stem[len(stem):]))
        elif whole_dir:
            out.append((p, "." + sub_stem))
    return out


def parse_sxxexx(name: str) -> tuple[int, list[int]] | None:
    """(season, [episodes]) from an S01E02-style name."""
    m = SXXEXX.search(name)
    if not m:
        return None
    return int(m.group(1)), sorted(int(e) for e in re.findall(r"\d+", m.group(2)))

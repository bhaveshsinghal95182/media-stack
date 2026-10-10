"""Small formatting and parsing helpers shared by every module."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

QBIT_INFINITY = 8640000  # qBittorrent's ETA for "never"


def human(n: float) -> str:
    """Bytes as a short human-readable size: 512 B, 1.5 KB, 3.2 GB."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def safe_name(title: str) -> str:
    """A title usable as a file or folder name."""
    return " ".join("".join(c for c in title if c not in '<>:"/\\|?*').split())


def eta(seconds: int) -> str:
    if not seconds or seconds >= QBIT_INFINITY:
        return ""
    m = seconds // 60
    if m >= 1440:
        return f"{m // 1440}d {m % 1440 // 60}h"
    if m >= 60:
        return f"{m // 60}h {m % 60}m"
    return f"{max(m, 1)}m"


def episode_code(season: int | None, episode: int | None) -> str:
    return f"S{season or 0:02d}E{episode or 0:02d}"


def episode_counts(statistics: dict | None) -> str:
    """'have/total' episodes from a Sonarr series or season statistics block."""
    st = statistics or {}
    return f"{st.get('episodeFileCount', 0)}/{st.get('episodeCount', 0)}"


def iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_utc(value: str | None) -> float | None:
    """Epoch seconds from an ISO-8601 date as the Arrs and Jellyfin send them.

    Naive dates count as UTC; Jellyfin's 7 fractional digits are cut to Python's 6.
    """
    if not value:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()

"""Request parameter types shared by the routes."""

from typing import Literal

from medialib import LibraryError

MediaKind = Literal["movie", "show"]


def title_key(key: str) -> tuple[str, int]:
    """'show:81189' -> ('show', 81189): how lists of titles are posted."""
    kind, _, ext_id = key.partition(":")
    if kind not in ("movie", "show") or not ext_id.isdigit():
        raise LibraryError(f"bad title key {key!r}")
    return kind, int(ext_id)

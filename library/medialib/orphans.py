"""Leftover torrents: in qBittorrent, but nothing in the library needs them any more."""

from __future__ import annotations

import time

from medialib.errors import LibraryError
from medialib.fmt import human
from medialib.stack import Kind, Stack


def claimed_hashes(stack: Stack, torrents: dict[str, dict]) -> set[str]:
    """Torrents something still needs: imported into the library, or still on their way.

    Not claimed (so listed as leftovers): torrents subgate rejected, downloads that were
    grabbed but are no longer in any queue, finished copies an Arr can't import for an
    episode/movie that already has a file (duplicates), and finished torrents none of
    whose files is hardlinked into the library any more.
    """
    queued: set[str] = set()     # on its way: in an Arr queue
    imported: set[str] = set()   # the Arr's history says it was imported
    for kind in ("movie", "show"):
        q, i = _claimed_by(stack.kind(kind), torrents)
        queued |= q
        imported |= i
    # History lies both ways: Sonarr drops it with deleted episode rows, and keeps it after
    # the library copy is gone. Imports are hardlinks, so the disk has the real answer.
    claimed = set(queued)
    for h, t in torrents.items():
        if h in claimed:
            continue
        links = torrent_links(stack, t) if t["progress"] >= 1 else None
        in_library = h in imported if links is None else any(n > 1 for n in links)
        if in_library:
            claimed.add(h)
    return claimed


def _claimed_by(k: Kind, torrents: dict[str, dict]) -> tuple[set[str], set[str]]:
    """(downloads queued for a reason, downloads history says were imported) for one Arr."""
    items = {i["id"]: i for i in k.items()}
    imported: set[str] = set()
    grabbed: dict[str, set[int | None]] = {}  # downloadId -> movie/episode ids it was for
    for item_id in items:
        for h in k.history(item_id):
            d = (h.get("downloadId") or "").lower()
            if not d:
                continue
            if h.get("eventType") == "downloadFolderImported":
                imported.add(d)
            elif h.get("eventType") == "grabbed":
                grabbed.setdefault(d, set()).add(h.get("movieId" if k.is_movie else "episodeId"))
    has_file: dict[int, bool] = {}

    def already_have(d: str) -> bool:
        if d not in grabbed:
            # No grab on record (e.g. its episode row was deleted): nothing will
            # ever import it, since the import rescue needs the grab too.
            return True
        ids = grabbed[d]
        if not ids or None in ids:
            return False
        for i in ids - has_file.keys():
            has_file[i] = bool((items.get(i) or {}).get("hasFile")) if k.is_movie \
                else bool(k.arr.get(f"/episode/{i}").get("hasFile"))
        return all(has_file[i] for i in ids)

    queued = set()
    for r in k.arr.queue():
        d = (r.get("downloadId") or "").lower()
        t = torrents.get(d)
        if not d or d in imported:
            continue
        if t and "subgate-rejected" in (t.get("tags") or ""):
            continue
        if (t and t["progress"] >= 1 and r.get("trackedDownloadState") == "importBlocked"
                and already_have(d)):
            continue
        queued.add(d)
    return queued, imported


def torrent_links(stack: Stack, t: dict) -> list[int] | None:
    """Hardlink count of each of the torrent's files, None when unknown."""
    try:
        paths = stack.qb.file_paths(t)
    except (LibraryError, ValueError):
        return None
    return stack.volume.link_counts(paths)


def orphan_list(stack: Stack) -> list[dict]:
    torrents = stack.qb.torrents_by_hash()
    claimed = claimed_hashes(stack, torrents)
    return [{"hash": t["hash"], "name": t["name"], "size": t["size"],
             "sizeHuman": human(t["size"]), "category": t["category"], "state": t["state"],
             "content_path": t.get("content_path")}
            for h, t in torrents.items() if h not in claimed]


def orphans_delete(stack: Stack, hashes: list[str]) -> dict:
    """Delete leftover torrents with their data, re-checking each is still a leftover."""
    current = {o["hash"]: o for o in orphan_list(stack)}
    targets = [current[h] for h in hashes if h in current]
    if len(targets) != len(hashes):
        raise LibraryError("some torrents are no longer leftovers; refresh and try again")
    if not targets:
        return {"ok": True, "still": [], "freedHuman": human(0)}
    stack.qb.delete([t["hash"] for t in targets])
    time.sleep(3)
    left = {t["hash"] for t in stack.qb.torrents()}
    still = [t["name"] for t in targets if t["hash"] in left
             or (t.get("content_path") and stack.volume.exists(t["content_path"]))]
    return {"ok": not still, "still": still,
            "freedHuman": human(sum(t["size"] for t in targets if t["name"] not in still))}

"""The library's movies and shows: list them, add them, delete them completely."""

from __future__ import annotations

import time
from collections.abc import Callable

from medialib.errors import LibraryError
from medialib.fmt import episode_counts, human
from medialib.stack import Kind, Stack, poster
from medialib.tv_access import tv_hidden_ids

Log = Callable[[str], None]


def overview(stack: Stack) -> dict[str, list[dict]]:
    """Every movie and show with its download state, sorted by title."""
    torrents = stack.qb.torrents_by_hash()
    return {"movies": _rows(stack, stack.kind("movie"), torrents),
            "shows": _rows(stack, stack.kind("show"), torrents)}


def _rows(stack: Stack, k: Kind, torrents: dict[str, dict]) -> list[dict]:
    queue = k.arr.queue()
    items = sorted(k.items(), key=lambda i: i["title"].lower())
    hidden = set() if k.is_movie else tv_hidden_ids(stack, items)
    rows = []
    for item in items:
        live = [torrents[h] for h in k.hashes(item, queue) if h in torrents]
        size = k.size(item)
        row = {"kind": k.kind, "id": item["id"], "extId": item.get(k.id_key),
               "title": item["title"], "year": item.get("year"), "label": k.label(item),
               "size": size, "sizeHuman": human(size) if size else "",
               "status": _status(live, size), "monitored": item.get("monitored"),
               "poster": poster(item)}
        if not k.is_movie:
            row["episodes"] = episode_counts(item.get("statistics"))
            row["hiddenFromTv"] = item["id"] in hidden
        rows.append(row)
    return rows


def _status(torrents: list[dict], size: int) -> str:
    downloading = [t for t in torrents if t["progress"] < 1]
    if downloading:
        return "downloading " + ", ".join(f"{t['progress'] * 100:.0f}%" for t in downloading)
    return "on disk" if size else "missing"


def add_title(stack: Stack, kind: str, ext_id: int) -> dict:
    """Add a title and search now; if it's already there, search again."""
    k = stack.kind(kind)
    existing = k.in_library(ext_id)
    if existing:
        k.search(existing["id"])
        return {"id": existing["id"], "label": k.label(existing), "added": False,
                "message": f"{k.label(existing)} was already in {k.arr.name}; searching again."}
    item = k.add(k.lookup_id(ext_id), search=True)
    return {"id": item["id"], "label": k.label(item), "added": True,
            "message": f"Added {k.label(item)} to {k.arr.name}; searching now."}


def grabbed(stack: Stack, kind: str, item_id: int) -> list[str]:
    """Release names in the queue for a title."""
    return sorted({r.get("title", "?") for r in stack.kind(kind).queue_for(item_id)})


# --------------------------------------------------------------------------
# Delete: the Arr entry and files, the torrents with their data, the Seerr record
# --------------------------------------------------------------------------

def delete_plan(stack: Stack, kind: str, item_id: int) -> dict:
    """What deleting a title removes, without changing anything."""
    k = stack.kind(kind)
    item = k.item(item_id)
    torrents = stack.qb.torrents_by_hash()
    live = [torrents[h] for h in k.hashes(item) if h in torrents]
    return {"kind": kind, "id": item_id, "label": k.label(item), "path": item["path"],
            "arr": k.arr.name, "size": k.size(item), "sizeHuman": human(k.size(item)),
            "tmdbId": item.get("tmdbId"), "tvdbId": item.get("tvdbId"),
            "torrents": [{"hash": t["hash"], "name": t["name"], "size": t["size"],
                          "sizeHuman": human(t["size"]), "state": t["state"],
                          "content_path": t.get("content_path")} for t in live]}


def delete_execute(stack: Stack, plan: dict, exclude: bool = False,
                   log: Log = print) -> dict:
    """Carry out a delete plan and verify on disk. `exclude` stops lists re-adding it."""
    k = stack.kind(plan["kind"])
    item_id = plan["id"]
    # 1. Queue entries first, so the Arr doesn't re-grab or fight the removal.
    q_ids = [r["id"] for r in k.queue_for(item_id)]
    if q_ids:
        log(f"Removing {len(q_ids)} queue entr{'y' if len(q_ids) == 1 else 'ies'}")
        k.arr.remove_from_queue(q_ids, blocklist=False, search_again=False)
    # 2. Torrents and their data (imports are hardlinks: the other half of every file).
    if plan["torrents"]:
        log(f"Deleting {len(plan['torrents'])} torrent(s) and their data")
        stack.qb.delete([t["hash"] for t in plan["torrents"]])
    # 3. The title and its library files.
    log(f"Deleting {plan['label']} from {k.arr.name} with its files")
    exclusion = "addImportExclusion" if k.is_movie else "addImportListExclusion"
    k.arr.send("DELETE", f"/{k.res}/{item_id}", deleteFiles="true",
               **{exclusion: str(exclude).lower()})
    # 4. Seerr keeps its own record and would keep showing the title as available.
    seerr_note = _clear_seerr(stack, k, plan)
    log(f"Seerr: {seerr_note}")
    time.sleep(3)
    problems = _delete_problems(stack, k, plan)
    freed = sum(t["size"] for t in plan["torrents"]) or plan["size"]
    return {"ok": not problems, "problems": problems, "seerr": seerr_note,
            "freed": freed, "freedHuman": human(freed), "label": plan["label"]}


def _clear_seerr(stack: Stack, k: Kind, plan: dict) -> str:
    if not stack.seerr_key:
        return "skipped (no Seerr API key)"
    try:
        media = stack.seerr("GET", "/media?take=1000&filter=all").get("results", [])
        mtype = "movie" if k.is_movie else "tv"
        hits = [m for m in media if m.get("mediaType") == mtype and (
            (plan.get("tmdbId") and m.get("tmdbId") == plan["tmdbId"])
            or (not k.is_movie and plan.get("tvdbId") and m.get("tvdbId") == plan["tvdbId"]))]
        for m in hits:
            stack.seerr("DELETE", f"/media/{m['id']}")
    except LibraryError as e:  # Seerr trouble must not undo a delete that worked
        return f"failed: {e}"
    return "cleared" if hits else "nothing to clear"


def _delete_problems(stack: Stack, k: Kind, plan: dict) -> list[str]:
    problems = []
    if any(i["id"] == plan["id"] for i in k.items()):
        problems.append(f"still listed in {k.arr.name}")
    if stack.volume.exists(plan["path"]):
        problems.append(f"library folder still exists: {plan['path']}")
    remaining = stack.qb.torrents_by_hash()
    for t in plan["torrents"]:
        if t["hash"].lower() in remaining:
            problems.append(f"torrent still in qBittorrent: {t['name']}")
        elif t.get("content_path") and stack.volume.exists(t["content_path"]):
            problems.append(f"torrent data still on disk: {t['content_path']}")
    return problems

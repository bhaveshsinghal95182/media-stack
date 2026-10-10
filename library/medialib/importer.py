"""Import local video files into the library, registered unmonitored so they never re-download."""

from __future__ import annotations

import os
from collections.abc import Callable

from medialib.errors import LibraryError
from medialib.files import SUB_EXTS, VIDEO_EXTS, ext, local_subs, local_videos, parse_sxxexx
from medialib.fmt import human, safe_name
from medialib.following import refresh_series
from medialib.stack import Kind, Stack, summarize_lookup
from medialib.volume import Progress

SCAN_LIMIT = 5000           # files examined per folder when counting its videos
SPACE_MARGIN = 2 * 1024**3  # keep this much free in the volume after an import


class ImportFolder:
    """The folder imports come from (~/Downloads, mounted read-only).

    It also holds ID scans and API-key exports, so only folders containing videos, video
    files and subtitles are ever listed, and nothing outside it can be reached.
    """

    def __init__(self, root: str, label: str = "Downloads"):
        self.root, self.label = os.path.realpath(root), label

    def _inside(self, path: str) -> bool:
        return path == self.root or path.startswith(self.root + os.sep)

    def resolve(self, rel: str) -> str:
        path = os.path.realpath(os.path.join(self.root, rel.lstrip("/")))
        if not self._inside(path):
            raise LibraryError("path is outside the import folder")
        if not os.path.exists(path):
            raise LibraryError("no such file or folder")
        return path

    def relative(self, path: str) -> str:
        rel = os.path.relpath(path, self.root)
        return "" if rel == "." else rel

    def display(self, text: str) -> str:
        """Container paths in a message, shown relative to the import folder."""
        return text.replace(self.root + os.sep, self.label + "/")

    def browse(self, rel: str) -> dict:
        path = self.resolve(rel)
        if not os.path.isdir(path):
            raise LibraryError("not a folder")
        entries = []
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if name.startswith(".") or not self._inside(os.path.realpath(full)):
                continue  # hidden files, and symlinks leading out
            try:
                entry = self._entry(name, full)
            except OSError:
                continue
            if entry:
                entries.append(entry)
        relpath = self.relative(path)
        return {"path": relpath, "parts": relpath.split(os.sep) if relpath else [],
                "entries": entries}

    @staticmethod
    def _entry(name: str, full: str) -> dict | None:
        rel_ext = ext(name)
        if os.path.isdir(full):
            count, size = _count_videos(full)
            return {"name": name, "type": "dir", "videos": count,
                    "sizeHuman": human(size)} if count else None
        if rel_ext in VIDEO_EXTS:
            return {"name": name, "type": "video", "sizeHuman": human(os.path.getsize(full))}
        if rel_ext in SUB_EXTS:
            return {"name": name, "type": "sub"}
        return None


def _count_videos(path: str) -> tuple[int, int]:
    count = size = seen = 0
    for d, _, files in os.walk(path):
        for f in files:
            seen += 1
            if seen > SCAN_LIMIT:
                return count, size
            if ext(f) in VIDEO_EXTS:
                count += 1
                try:
                    size += os.path.getsize(os.path.join(d, f))
                except OSError:
                    pass
    return count, size


def import_candidates(stack: Stack, kind: str, path: str, query: str | None = None,
                      year: int | None = None) -> dict:
    """Guess the title from the filename and return matching lookups."""
    k = stack.kind(kind)
    videos = local_videos(path)
    if not videos:
        raise LibraryError(f"no video files in {path}")
    guess = query or ""
    if not guess:
        parsed = k.arr.get("/parse", title=os.path.basename(videos[0])) or {}
        if k.is_movie:
            info = parsed.get("parsedMovieInfo") or {}
            guess = (info.get("movieTitles") or [""])[0]
            year = year or info.get("year") or None
        else:
            guess = (parsed.get("parsedEpisodeInfo") or {}).get("seriesTitle") or ""
    results = k.lookup(guess) if guess else []
    if year:
        results = [r for r in results if r.get("year") == year] or results
    return {"guess": guess, "year": year,
            "results": [summarize_lookup(k, r) for r in results[:8]]}


def _episode_for(k: Kind, series: dict | None, name: str, known: set | None):
    """(season, [episodes]) for a filename: Sonarr's parser, else S01E02 in the name."""
    if series and series.get("id"):
        for title in (name, f"{series['title']} {name}"):
            parsed = k.arr.get("/parse", title=title) or {}
            if (parsed.get("series") or {}).get("id") == series["id"] and parsed.get("episodes"):
                eps = parsed["episodes"]
                return eps[0]["seasonNumber"], sorted(e["episodeNumber"] for e in eps)
    hit = parse_sxxexx(name)
    if hit and (known is None or all((hit[0], e) in known for e in hit[1])):
        return hit
    return None


def _movie_files(path: str, base: str, folder: str) -> list[dict]:
    videos = local_videos(path)
    if len(videos) != 1:
        raise LibraryError(f"expected exactly one video file, found {len(videos)}: "
                           + ", ".join(os.path.basename(v) for v in videos[:5])
                           + ". Pick the single file instead.")
    video = videos[0]
    files = [{"src": video, "dest": f"{folder}/{base}{ext(video)}",
              "size": os.path.getsize(video)}]
    for s, suffix in local_subs(path, video, os.path.isdir(path)):
        files.append({"src": s, "dest": f"{folder}/{base}{suffix}{ext(s)}",
                      "size": os.path.getsize(s), "sub": True})
    return files


def _show_files(k: Kind, series: dict, folder: str, path: str, replace: bool,
                episodes: list[dict] | None) -> tuple[list[dict], list[dict]]:
    known = {(e["seasonNumber"], e["episodeNumber"]) for e in episodes} if episodes else None
    have = {(e["seasonNumber"], e["episodeNumber"]) for e in episodes or [] if e.get("hasFile")}
    title = safe_name(series["title"])
    files, skipped = [], []
    for v in local_videos(path):
        hit = _episode_for(k, series, os.path.basename(v), known)
        if not hit:
            skipped.append({"src": v, "why": "couldn't tell which episode (needs S01E02-style name)"})
            continue
        season, eps = hit
        tag = f"S{season:02}" + "".join(f"E{e:02}" for e in eps)
        if any((season, e) in have for e in eps) and not replace:
            skipped.append({"src": v, "why": f"{tag} already has a file (use replace)"})
            continue
        base = f"{folder}/Season {season:02}/{title} - {tag}"
        files.append({"src": v, "dest": base + ext(v), "size": os.path.getsize(v),
                      "tag": tag, "season": season, "episodes": eps})
        for s, suffix in local_subs(path, v, False):
            files.append({"src": s, "dest": f"{base}{suffix}{ext(s)}",
                          "size": os.path.getsize(s), "sub": True})
    return files, skipped


def import_plan(stack: Stack, kind: str, path: str, ext_id: int, replace: bool = False) -> dict:
    """Preview an import without changing anything."""
    k = stack.kind(kind)
    existing = k.in_library(ext_id)
    item = existing or k.lookup_id(ext_id)
    warnings, skipped = [], []
    if k.is_movie:
        if existing and existing.get("hasFile") and not replace:
            raise LibraryError(f"{k.label(item)} already has a file in the library; "
                               "use replace to swap it")
        base = f"{safe_name(item['title'])} ({item['year']})"
        folder = item.get("path") or f"{k.profile_and_root()[1]}/{base}"
        files = _movie_files(path, base, folder)
        if existing and k.queue_for(existing["id"]):
            warnings.append(f"{k.arr.name} is downloading a release for this movie; "
                            "it will be cancelled (not blocklisted).")
    else:
        folder = item.get("path") or f"{k.profile_and_root()[1]}/{safe_name(item['title'])}"
        episodes = k.arr.get("/episode", seriesId=existing["id"]) if existing else None
        files, skipped = _show_files(k, item, folder, path, replace, episodes)
        if not existing:
            warnings.append(f"{item['title']} will be added to Sonarr unmonitored (no downloads).")
        if not [f for f in files if not f.get("sub")]:
            raise LibraryError("nothing to import: " + "; ".join(
                f"{os.path.basename(s['src'])}: {s['why']}" for s in skipped))
    need = sum(f["size"] for f in files)
    free = min(r.get("freeSpace") or 0 for r in k.arr.get("/rootfolder"))
    if free and need > free - SPACE_MARGIN:
        raise LibraryError(f"not enough space in the volume: need {human(need)}, "
                           f"{human(free)} free")
    return {"kind": kind, "path": path, "extId": ext_id, "replace": replace,
            "label": k.label(item), "folder": folder, "inLibrary": bool(existing),
            "files": [dict(f, sizeHuman=human(f["size"]), name=os.path.basename(f["src"]))
                      for f in files],
            "skipped": [dict(s, name=os.path.basename(s["src"])) for s in skipped],
            "warnings": warnings, "total": need, "totalHuman": human(need)}


def import_execute(stack: Stack, kind: str, path: str, ext_id: int, replace: bool = False,
                   monitor: bool = False, log: Callable[[str], None] = print,
                   progress: Progress | None = None) -> dict:
    """Do the import. Re-plans after adding, so episode matching uses real data.

    monitor=True leaves a movie monitored, so Radarr may still upgrade it later.
    """
    k = stack.kind(kind)
    item = k.in_library(ext_id)
    if not item:
        log(f"Adding to {k.arr.name} (unmonitored, no search)")
        item = k.add(k.lookup_id(ext_id), search=False)
        if not k.is_movie:
            # A new series' episode list loads in the background; matching
            # filenames before it exists finds nothing.
            refresh_series(k.arr, item["id"], new=True)
    plan = import_plan(stack, kind, path, ext_id, replace)
    _make_room(k, item, plan, replace, log)
    _copy(stack, kind, plan, log, progress)
    log(f"Asking {k.arr.name} to register the files")
    done = _register_movie(k, item, plan, monitor) if k.is_movie \
        else _register_episodes(k, item, plan)
    return {"ok": True, "label": plan["label"], "imported": done,
            "skipped": plan["skipped"], "folder": plan["folder"]}


def _make_room(k: Kind, item: dict, plan: dict, replace: bool, log: Callable[[str], None]) -> None:
    """Cancel a movie's running download, and remove the files being replaced."""
    if k.is_movie:
        queued = [r["id"] for r in k.queue_for(item["id"])]
        if queued:
            log("Cancelling the running download for this movie")
            k.arr.remove_from_queue(queued, blocklist=False, search_again=False)
        if replace and item.get("hasFile"):
            log("Removing the old library file")
            k.arr.send("DELETE", f"/moviefile/{item['movieFile']['id']}")
    elif replace:
        tags = {(f["season"], e) for f in plan["files"] if not f.get("sub") for e in f["episodes"]}
        old = {e["episodeFileId"] for e in k.arr.get("/episode", seriesId=item["id"])
               if e.get("hasFile") and (e["seasonNumber"], e["episodeNumber"]) in tags}
        for fid in old:
            k.arr.send("DELETE", f"/episodefile/{fid}")


def _copy(stack: Stack, kind: str, plan: dict, log: Callable[[str], None],
          progress: Progress | None) -> None:
    total, copied = plan["total"] or 1, 0
    for f in plan["files"]:
        log(f"Copying {f['name']} -> {f['dest']}")

        def file_progress(done: int, _size: int, base: int = copied) -> None:
            if progress:
                progress(base + done, total)
        stack.volume.copy_in(kind, f["src"], f["dest"], file_progress)
        copied += f["size"]


def _register_movie(k: Kind, item: dict, plan: dict, monitor: bool) -> str:
    k.arr.run_command({"name": "RescanMovie", "movieId": item["id"]})
    item = k.item(item["id"])
    if item.get("monitored") and not monitor:
        k.arr.send("PUT", f"/movie/{item['id']}", dict(item, monitored=False))
    if not item.get("hasFile"):
        raise LibraryError(f"copied, but {k.arr.name} didn't register the file; "
                           f"check {plan['folder']}")
    return item["movieFile"]["relativePath"]


def _register_episodes(k: Kind, item: dict, plan: dict) -> str:
    k.arr.run_command({"name": "RescanSeries", "seriesId": item["id"]})
    got = {(e["seasonNumber"], e["episodeNumber"])
           for e in k.arr.get("/episode", seriesId=item["id"]) if e.get("hasFile")}
    videos = [f for f in plan["files"] if not f.get("sub")]
    missing = [f["tag"] for f in videos if any((f["season"], e) not in got for e in f["episodes"])]
    if missing:
        raise LibraryError(f"copied, but {k.arr.name} didn't register: {', '.join(missing)}")
    return f"{len(videos)} episode file(s)"

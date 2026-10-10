"""Import finished downloads Sonarr grabbed but can't place by their release name."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from medialib.stack import Stack

RETRY_AFTER = 3600  # seconds before retrying a rescue that didn't import
PASSED = re.compile(r"subgate-(ok|skipped)")


def grab_episodes(stack: Stack, download_id: str) -> tuple[int | None, list[int]]:
    """Series and episodes Sonarr grabbed a download for, from its own history."""
    recs = stack.sonarr.get("/history", page=1, pageSize=50,
                            downloadId=download_id).get("records", [])
    grabs = [r for r in recs if r.get("eventType") == "grabbed"]
    series = {r.get("seriesId") for r in grabs}
    if len(series) != 1:
        return None, []
    return series.pop(), sorted({r["episodeId"] for r in grabs if r.get("episodeId")})


def _rank(entry: tuple[dict, dict]) -> tuple:
    """Best copy first: resolution, then custom format score, then the oldest torrent."""
    record, torrent = entry
    resolution = ((record.get("quality") or {}).get("quality") or {}).get("resolution") or 0
    return resolution, record.get("customFormatScore") or 0, -torrent.get("added_on", 0)


class ImportRescuer:
    """Fixes "matched to series by ID" / "Unable to parse" blocked imports.

    The episode comes from Sonarr's own grab history, and only downloads subgate passed
    are used. One copy per episode: the best one. Episodes that already have a file are
    left alone (their extra copies show up as leftovers in Cleanup).
    """

    def __init__(self) -> None:
        self.tried: dict[str, float] = {}  # downloadId -> when it was last attempted

    def __call__(self, stack: Stack, log: Callable[[str], None] = print,
                 now: float | None = None) -> list[str]:
        now = now or time.time()
        done, taken = [], set()
        for (series_id, eps), group in self._candidates(stack, now).items():
            info = [stack.sonarr.get(f"/episode/{e}") for e in eps]
            # Numbers, not ids: a series Sonarr holds twice has two ids per episode.
            numbers = {(series_id, i.get("seasonNumber"), i.get("episodeNumber")) for i in info}
            if any(i.get("hasFile") for i in info) or numbers & taken:
                continue
            taken |= numbers
            for record, _t in group:  # the whole episode waits, so a slow import isn't doubled
                self.tried[record["downloadId"]] = now
            record, torrent = max(group, key=_rank)
            if self._import(stack, series_id, list(eps), record):
                done.append(torrent["name"])
                log(f"Importing {torrent['name']} (Sonarr couldn't match its name; "
                    "used its grab history)")
            else:
                log(f"Sonarr can't place {torrent['name']} by itself; import it by hand in "
                    "Sonarr (Activity → Queue → Manual Import)")
        return done

    def _candidates(self, stack: Stack, now: float) -> dict[tuple, list[tuple[dict, dict]]]:
        """Finished, subgate-passed, import-blocked downloads grouped by their episodes."""
        torrents = stack.qb.torrents_by_hash()
        groups: dict[tuple[int, tuple[int, ...]], list[tuple[dict, dict]]] = {}
        for r in stack.sonarr.queue():
            d = r.get("downloadId") or ""
            t = torrents.get(d.lower())
            if (r.get("trackedDownloadState") != "importBlocked" or not t or t["progress"] < 1
                    or not PASSED.search(t.get("tags") or "")
                    or now - self.tried.get(d, -RETRY_AFTER) < RETRY_AFTER):
                continue
            series_id, eps = grab_episodes(stack, d)
            if series_id and eps:
                groups.setdefault((series_id, tuple(eps)), []).append((r, t))
        return groups

    @staticmethod
    def _import(stack: Stack, series_id: int, eps: list[int], record: dict) -> bool:
        files = stack.sonarr.get("/manualimport", downloadId=record["downloadId"],
                                 filterExistingFiles="false")
        if len(files) == 1:
            plan = [(files[0], eps)]
        elif files and all(f.get("episodes") for f in files):  # a pack whose files do parse
            plan = [(f, [e["id"] for e in f["episodes"]]) for f in files]
        else:
            return False
        stack.sonarr.command({
            "name": "ManualImport", "importMode": "copy",  # copy = hardlink here
            "files": [{"path": f["path"], "seriesId": series_id, "episodeIds": ids,
                       "quality": f.get("quality"), "languages": f.get("languages") or [],
                       "releaseGroup": f.get("releaseGroup"),
                       "indexerFlags": f.get("indexerFlags") or 0,
                       "releaseType": f.get("releaseType") or "singleEpisode",
                       "downloadId": record["downloadId"]} for f, ids in plan]})
        return True

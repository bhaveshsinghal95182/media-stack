"""Shared logic for the media library tools (CLI and web page).

Everything here returns data instead of printing, so both front-ends run the
same code. Delete removes everything a title occupies: the Radarr/Sonarr entry
and library files, the qBittorrent torrent *and its data* (imports are
hardlinks, so removing one copy frees nothing) and the Seerr record, then
verifies on disk. Import copies local files into the title's library folder
and has Radarr/Sonarr register them, unmonitored, so they never re-download.

Runs on the host (talks to localhost ports, touches the volume via
`docker exec`/`docker cp`) or inside the media-library container
(LIBRARY_LOCAL_DATA=1: service hostnames, volume mounted at /data).
"""

from __future__ import annotations

import calendar
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STACK_ROOT = os.path.dirname(HERE)
CATEGORIES = ("radarr", "tv-sonarr")
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".webm", ".mov", ".wmv",
              ".mpg", ".mpeg"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
CONTAINER = {"movie": "media-radarr", "show": "media-sonarr"}


class LibraryError(Exception):
    """A user-facing failure (bad input, service down, verification failed)."""


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _ext(p: str) -> str:
    return os.path.splitext(p)[1].lower()


def safe_name(title: str) -> str:
    return " ".join("".join(c for c in title if c not in '<>:"/\\|?*').split())


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class Settings:
    def __init__(self):
        env = dict(os.environ)
        dotenv = os.path.join(STACK_ROOT, ".env")
        if os.path.exists(dotenv):
            with open(dotenv) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
        for k in ("QBIT_USERNAME", "QBIT_PASSWORD", "RADARR_API_KEY", "SONARR_API_KEY"):
            if not env.get(k):
                raise LibraryError(f"{k} is not set (.env)")
        self.local_data = env.get("LIBRARY_LOCAL_DATA") == "1"
        self.radarr_url = env.get("RADARR_URL", "http://localhost:7878").rstrip("/")
        self.sonarr_url = env.get("SONARR_URL", "http://localhost:8989").rstrip("/")
        self.qbit_url = env.get("QBIT_URL", "http://localhost:8080").rstrip("/")
        self.seerr_url = env.get("SEERR_URL", "http://localhost:5055").rstrip("/")
        self.radarr_key = env["RADARR_API_KEY"]
        self.sonarr_key = env["SONARR_API_KEY"]
        self.qbit_user = env["QBIT_USERNAME"]
        self.qbit_pass = env["QBIT_PASSWORD"]
        self.seerr_settings_path = env.get(
            "SEERR_SETTINGS", os.path.join(STACK_ROOT, "config/seerr/settings.json"))


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------

def http_json(url: str, method: str = "GET", body=None, headers: dict | None = None,
              timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:  # Radarr/Sonarr send {"message": ...} (or a list of validation errors)
            j = json.loads(detail)
            j = j[0] if isinstance(j, list) and j else j
            detail = j.get("message") or j.get("errorMessage") or j.get("title") or detail
        except (ValueError, AttributeError):
            pass
        detail = detail[:300]
        raise LibraryError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LibraryError(f"{url.split('/api')[0]} unreachable ({e.reason}); "
                           "is the stack running?") from None
    return json.loads(raw) if raw else None


class Arr:
    def __init__(self, base: str, key: str, name: str):
        self.base, self.key, self.name = base, key, name

    def get(self, path: str, timeout: int = 60, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return http_json(self.base + "/api/v3" + path + q, headers={"X-Api-Key": self.key},
                         timeout=timeout)

    def send(self, method: str, path: str, body=None, timeout: int = 60, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return http_json(self.base + "/api/v3" + path + q, method, body, {"X-Api-Key": self.key},
                         timeout)

    def queue(self) -> list[dict]:
        return self.get("/queue", page=1, pageSize=1000).get("records", [])

    def run_command(self, body: dict, timeout: int = 300) -> None:
        cmd = self.send("POST", "/command", body)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            st = self.get(f"/command/{cmd['id']}")
            if st.get("status") == "completed":
                return
            if st.get("status") in ("failed", "aborted", "cancelled"):
                raise LibraryError(f"{self.name} {body['name']} {st['status']}: {st.get('message')}")
        raise LibraryError(f"{self.name} {body['name']} still running after {timeout}s")


class QBit:
    def __init__(self, base: str, user: str, password: str):
        self.base, self.user, self.password = base, user, password
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _raw(self, path: str, data: dict | None = None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.base}/api/v2/{path}", data=body,
                                     headers={"Referer": self.base})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except urllib.error.URLError as e:
            raise LibraryError(f"qBittorrent unreachable ({e.reason}); is the stack running?") from None

    def _login(self) -> None:
        status, body = self._raw("auth/login", {"username": self.user, "password": self.password})
        if status not in (200, 204) or body.strip() == b"Fails.":
            # qBittorrent bans an IP for an hour after repeated failures: don't retry.
            raise LibraryError("qBittorrent login failed (check QBIT_PASSWORD in .env)")
        self.logged_in = True

    def call(self, path: str, data: dict | None = None) -> bytes:
        if not self.logged_in:
            self._login()
        status, body = self._raw(path, data)
        if status == 403:
            self._login()
            status, body = self._raw(path, data)
        if status >= 400:
            raise LibraryError(f"qBittorrent {path} -> HTTP {status}")
        return body

    def torrents(self) -> list[dict]:
        return [t for t in json.loads(self.call("torrents/info"))
                if t.get("category") in CATEGORIES]

    def delete(self, hashes: list[str]) -> None:
        self.call("torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true"})


# --------------------------------------------------------------------------
# The volume: local mount (container) or docker exec (host)
# --------------------------------------------------------------------------

class Volume:
    def __init__(self, local: bool):
        self.local = local

    def _docker(self, kind: str, *cmd: str, check: bool = True):
        r = subprocess.run(["docker", "exec", "-u", "abc", CONTAINER[kind], *cmd],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise LibraryError(f"{cmd[0]} in {CONTAINER[kind]} failed: {r.stderr.strip()}")
        return r

    def exists(self, path: str) -> bool:
        if self.local:
            return os.path.exists(path)
        return self._docker("movie", "test", "-e", path, check=False).returncode == 0

    def copy_in(self, kind: str, src: str, dest: str, progress=None) -> None:
        """Copy a file into the volume and verify its size."""
        size = os.path.getsize(src)
        if self.local:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".partial"
            done = 0
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    chunk = fi.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    fo.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, size)
            shutil.copystat(src, tmp)
            os.replace(tmp, dest)
            got = os.path.getsize(dest)
        else:
            self._docker(kind, "mkdir", "-p", os.path.dirname(dest))
            # -a keeps the owner: host uid 1000 == PUID, so the Arrs can manage it.
            r = subprocess.run(["docker", "cp", "-a", src, f"{CONTAINER[kind]}:{dest}"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise LibraryError(f"docker cp failed for {src}: {r.stderr.strip()}")
            got = int(self._docker(kind, "stat", "-c", "%s", dest).stdout.strip())
            if progress:
                progress(size, size)
        if got != size:
            raise LibraryError(f"size mismatch after copying {src} ({got} of {size} bytes)")


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------

class Stack:
    def __init__(self, settings: Settings | None = None):
        s = settings or Settings()
        self.settings = s
        self.radarr = Arr(s.radarr_url, s.radarr_key, "Radarr")
        self.sonarr = Arr(s.sonarr_url, s.sonarr_key, "Sonarr")
        self.qb = QBit(s.qbit_url, s.qbit_user, s.qbit_pass)
        self.volume = Volume(s.local_data)
        try:
            with open(s.seerr_settings_path) as f:
                self.seerr_settings = json.load(f)
        except (OSError, ValueError):
            self.seerr_settings = {}
        self.seerr_key = self.seerr_settings.get("main", {}).get("apiKey")

    def seerr(self, method: str, path: str):
        return http_json(self.settings.seerr_url + "/api/v1" + path, method,
                         headers={"X-Api-Key": self.seerr_key})

    def kind(self, kind: str) -> "Kind":
        if kind not in ("movie", "show"):
            raise LibraryError(f"unknown kind {kind!r}")
        return Kind(self, kind)


class Kind:
    """Movie (Radarr) or show (Sonarr) specifics."""

    def __init__(self, stack: Stack, kind: str):
        self.stack, self.kind = stack, kind
        if kind == "movie":
            self.arr, self.res, self.id_name, self.id_key = stack.radarr, "movie", "tmdb", "tmdbId"
            self.queue_key = "movieId"
        else:
            self.arr, self.res, self.id_name, self.id_key = stack.sonarr, "series", "tvdb", "tvdbId"
            self.queue_key = "seriesId"

    def label(self, item: dict) -> str:
        return f"{item['title']} ({item.get('year') or '?'}) [{self.id_name}:{item.get(self.id_key)}]"

    def items(self) -> list[dict]:
        return self.arr.get("/" + self.res)

    def item(self, item_id: int) -> dict:
        return self.arr.get(f"/{self.res}/{item_id}")

    def size(self, item: dict) -> int:
        if self.kind == "movie":
            return item.get("sizeOnDisk") or 0
        return (item.get("statistics") or {}).get("sizeOnDisk") or 0

    def history(self, item_id: int) -> list[dict]:
        if self.kind == "movie":
            return self.arr.get("/history/movie", movieId=item_id)
        return self.arr.get("/history/series", seriesId=item_id)

    def hashes(self, item: dict, queue: list[dict] | None = None) -> set[str]:
        """Torrent hashes that belong to this title (grab history + queue)."""
        out = {h["downloadId"].lower() for h in self.history(item["id"])
               if h.get("eventType") == "grabbed" and h.get("downloadId")}
        queue = self.arr.queue() if queue is None else queue
        out |= {r["downloadId"].lower() for r in queue
                if r.get(self.queue_key) == item["id"] and r.get("downloadId")}
        return out

    def lookup(self, query: str) -> list[dict]:
        if self.kind == "movie":
            if query.lower().startswith("tmdb:"):
                return [self.arr.get("/movie/lookup/tmdb", tmdbId=int(query[5:]))]
            return self.arr.get("/movie/lookup", term=query)
        return self.arr.get("/series/lookup", term=query)

    def lookup_id(self, ext_id: int) -> dict:
        res = self.lookup(f"{self.id_name}:{ext_id}")
        hit = [r for r in res if r.get(self.id_key) == ext_id]
        if not hit:
            raise LibraryError(f"{self.id_name}:{ext_id} not found")
        return hit[0]

    def in_library(self, ext_id: int) -> dict | None:
        return next((i for i in self.items() if i.get(self.id_key) == ext_id), None)

    def profile_and_root(self) -> tuple[int, str]:
        # Same defaults Seerr uses for requests, so both paths behave alike.
        seerr = self.stack.seerr_settings.get("radarr" if self.kind == "movie" else "sonarr") or []
        default = next((s for s in seerr if s.get("isDefault")), seerr[0] if seerr else {})
        profiles = self.arr.get("/qualityprofile")
        prof = next((p for p in profiles if p["id"] == default.get("activeProfileId")), None) \
            or next((p for p in profiles if p["name"] == default.get("activeProfileName")), None) \
            or profiles[0]
        root = default.get("activeDirectory") or self.arr.get("/rootfolder")[0]["path"]
        return prof["id"], root

    def add(self, lookup_item: dict, search: bool) -> dict:
        """Add a looked-up title. search=False adds it unmonitored (for imports)."""
        profile_id, root = self.profile_and_root()
        body = dict(lookup_item, qualityProfileId=profile_id, rootFolderPath=root,
                    monitored=search)
        if self.kind == "movie":
            body.update(minimumAvailability="released", addOptions={"searchForMovie": search})
        else:
            if "anime" in [g.lower() for g in lookup_item.get("genres", [])]:
                body["seriesType"] = "anime"
            body.update(seasonFolder=True, addOptions={
                "monitor": "all" if search else "none", "searchForMissingEpisodes": search})
        return self.arr.send("POST", "/" + self.res, body)


def poster(item: dict) -> str | None:
    for img in item.get("images") or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl") or img.get("url")
    return None


def summarize_lookup(k: Kind, r: dict) -> dict:
    return {"extId": r.get(k.id_key), "title": r["title"], "year": r.get("year"),
            "overview": (r.get("overview") or "")[:300], "poster": poster(r),
            "label": k.label(r), "inLibrary": bool(r.get("id"))}


# --------------------------------------------------------------------------
# Library overview
# --------------------------------------------------------------------------

def overview(stack: Stack) -> dict:
    torrents = {t["hash"].lower(): t for t in stack.qb.torrents()}
    claimed: set[str] = set()
    out = {}
    for kind in ("movie", "show"):
        k = stack.kind(kind)
        queue = k.arr.queue()
        claimed |= {r["downloadId"].lower() for r in queue if r.get("downloadId")}
        rows = []
        for item in sorted(k.items(), key=lambda i: i["title"].lower()):
            hs = k.hashes(item, queue)
            claimed |= hs
            live = [torrents[h] for h in hs if h in torrents]
            size = k.size(item)
            downloading = [t for t in live if t["progress"] < 1]
            if downloading:
                status = "downloading " + ", ".join(f"{t['progress'] * 100:.0f}%" for t in downloading)
            elif size:
                status = "on disk"
            else:
                status = "missing"
            row = {"id": item["id"], "extId": item.get(k.id_key), "title": item["title"],
                   "year": item.get("year"), "label": k.label(item), "size": size,
                   "sizeHuman": human(size) if size else "", "status": status,
                   "monitored": item.get("monitored"), "poster": poster(item),
                   "torrents": [{"name": t["name"], "state": t["state"],
                                 "progress": t["progress"]} for t in live]}
            if kind == "show":
                st = item.get("statistics") or {}
                row["episodes"] = f"{st.get('episodeFileCount', 0)}/{st.get('episodeCount', 0)}"
            rows.append(row)
        out["movies" if kind == "movie" else "shows"] = rows
    orphans = [t for h, t in torrents.items() if h not in claimed]
    out["orphans"] = {"count": len(orphans), "size": sum(t["size"] for t in orphans),
                      "sizeHuman": human(sum(t["size"] for t in orphans))}
    return out


# --------------------------------------------------------------------------
# Add
# --------------------------------------------------------------------------

def add_title(stack: Stack, kind: str, ext_id: int) -> dict:
    """Add a title and search now; if it's already there, search again."""
    k = stack.kind(kind)
    existing = k.in_library(ext_id)
    if existing:
        if k.kind == "movie":
            k.arr.send("POST", "/command", {"name": "MoviesSearch", "movieIds": [existing["id"]]})
        else:
            k.arr.send("POST", "/command", {"name": "SeriesSearch", "seriesId": existing["id"]})
        return {"id": existing["id"], "label": k.label(existing), "added": False,
                "message": f"{k.label(existing)} was already in {k.arr.name}; searching again."}
    item = k.add(k.lookup_id(ext_id), search=True)
    return {"id": item["id"], "label": k.label(item), "added": True,
            "message": f"Added {k.label(item)} to {k.arr.name}; searching now."}


def grabbed(stack: Stack, kind: str, item_id: int) -> list[str]:
    k = stack.kind(kind)
    return sorted({r.get("title", "?") for r in k.arr.queue() if r.get(k.queue_key) == item_id})


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

def delete_plan(stack: Stack, kind: str, item_id: int) -> dict:
    k = stack.kind(kind)
    item = k.item(item_id)
    torrents = {t["hash"].lower(): t for t in stack.qb.torrents()}
    live = [torrents[h] for h in k.hashes(item) if h in torrents]
    return {"kind": kind, "id": item_id, "label": k.label(item), "path": item["path"],
            "arr": k.arr.name, "size": k.size(item), "sizeHuman": human(k.size(item)),
            "extId": item.get(k.id_key), "tmdbId": item.get("tmdbId"),
            "tvdbId": item.get("tvdbId"),
            "torrents": [{"hash": t["hash"], "name": t["name"], "size": t["size"],
                          "sizeHuman": human(t["size"]), "state": t["state"],
                          "content_path": t.get("content_path")} for t in live]}


def delete_execute(stack: Stack, plan: dict, exclude: bool = False, log=print) -> dict:
    k = stack.kind(plan["kind"])
    item_id = plan["id"]
    # 1. Queue entries first, so the Arr doesn't re-grab or fight the removal.
    q_ids = [r["id"] for r in k.arr.queue() if r.get(k.queue_key) == item_id]
    if q_ids:
        log(f"Removing {len(q_ids)} queue entr{'y' if len(q_ids) == 1 else 'ies'}")
        k.arr.send("DELETE", "/queue/bulk", {"ids": q_ids}, removeFromClient="true",
                   blocklist="false", skipRedownload="true")
    # 2. Torrents and their data (the other half of every hardlink).
    if plan["torrents"]:
        log(f"Deleting {len(plan['torrents'])} torrent(s) and their data")
        stack.qb.delete([t["hash"] for t in plan["torrents"]])
    # 3. The title and its library files.
    log(f"Deleting {plan['label']} from {k.arr.name} with its files")
    excl = "true" if exclude else "false"
    if k.kind == "movie":
        k.arr.send("DELETE", f"/movie/{item_id}", deleteFiles="true", addImportExclusion=excl)
    else:
        k.arr.send("DELETE", f"/series/{item_id}", deleteFiles="true",
                   addImportListExclusion=excl)
    # 4. Seerr (its own media list, so a TMDB hiccup can't block this).
    seerr_note = "skipped (no Seerr API key)"
    if stack.seerr_key:
        try:
            media = stack.seerr("GET", "/media?take=1000&filter=all").get("results", [])
            mtype = "movie" if k.kind == "movie" else "tv"
            hits = [m for m in media if m.get("mediaType") == mtype and (
                (plan.get("tmdbId") and m.get("tmdbId") == plan["tmdbId"])
                or (k.kind == "show" and plan.get("tvdbId")
                    and m.get("tvdbId") == plan["tvdbId"]))]
            for m in hits:
                stack.seerr("DELETE", f"/media/{m['id']}")
            seerr_note = "cleared" if hits else "nothing to clear"
        except LibraryError as e:
            seerr_note = f"failed: {e}"
    log(f"Seerr: {seerr_note}")
    # 5. Verify.
    time.sleep(3)
    problems = []
    if any(i["id"] == item_id for i in k.items()):
        problems.append(f"still listed in {k.arr.name}")
    if stack.volume.exists(plan["path"]):
        problems.append(f"library folder still exists: {plan['path']}")
    remaining = {t["hash"].lower() for t in stack.qb.torrents()}
    for t in plan["torrents"]:
        if t["hash"].lower() in remaining:
            problems.append(f"torrent still in qBittorrent: {t['name']}")
        elif t.get("content_path") and stack.volume.exists(t["content_path"]):
            problems.append(f"torrent data still on disk: {t['content_path']}")
    freed = sum(t["size"] for t in plan["torrents"]) or plan["size"]
    return {"ok": not problems, "problems": problems, "seerr": seerr_note,
            "freed": freed, "freedHuman": human(freed), "label": plan["label"]}


# --------------------------------------------------------------------------
# Releases: see what is downloading, pick another release, or drop one
# --------------------------------------------------------------------------

RELEASE_SEARCH_TIMEOUT = 240  # indexer searches through Prowlarr can be slow


def _eta(seconds: int) -> str:
    if not seconds or seconds >= 8640000:  # qBittorrent's "infinity"
        return ""
    m = seconds // 60
    return f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else f"{m // 60}h {m % 60}m" if m >= 60 \
        else f"{max(m, 1)}m"


def _record_season(r: dict):
    return r.get("seasonNumber", (r.get("episode") or {}).get("seasonNumber"))


def _queue_for(k: Kind, item_id: int, season: int | None = None) -> list[dict]:
    params = {"page": 1, "pageSize": 1000}
    if k.kind == "show":
        params["includeEpisode"] = "true"
    recs = [r for r in k.arr.get("/queue", **params).get("records", [])
            if r.get(k.queue_key) == item_id]
    if k.kind == "show" and season is not None:
        recs = [r for r in recs if _record_season(r) == season]
    return recs


def downloads(stack: Stack, kind: str, item_id: int, season: int | None = None) -> list[dict]:
    """What is downloading for this title right now, with live qBittorrent numbers."""
    k = stack.kind(kind)
    torrents = {t["hash"].lower(): t for t in stack.qb.torrents()}
    groups: dict[str, dict] = {}
    for r in _queue_for(k, item_id, season):
        did = (r.get("downloadId") or "").lower()
        g = groups.setdefault(did, {"downloadId": did, "title": r.get("title", "?"),
                                    "episodes": set(), "status": r.get("trackedDownloadState")})
        ep = r.get("episode") or {}
        if ep:
            g["episodes"].add(f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}")
    out = []
    for did, g in groups.items():
        t = torrents.get(did)
        tags = [x.strip() for x in (t or {}).get("tags", "").split(",") if x.strip()]
        sub = next((x[len("subgate-"):] for x in tags if x.startswith("subgate-")), "")
        out.append({
            "downloadId": did, "title": g["title"], "episodes": sorted(g["episodes"]),
            "inClient": t is not None,
            "progress": t["progress"] if t else None,
            "state": t["state"] if t else g["status"],
            "seeds": t["num_seeds"] if t else None,          # connected to us
            "swarmSeeds": t["num_complete"] if t else None,  # tracker's count
            "speed": human(t["dlspeed"]) + "/s" if t else "",
            "eta": _eta(t["eta"]) if t else "",
            "sizeHuman": human(t["size"]) if t else "",
            "subgate": sub})
    return out


def release_info(stack: Stack, kind: str, item_id: int) -> dict:
    k = stack.kind(kind)
    item = k.item(item_id)
    seasons = []
    if k.kind == "show":
        for s in sorted(item.get("seasons", []), key=lambda x: x["seasonNumber"]):
            st = s.get("statistics") or {}
            seasons.append({"number": s["seasonNumber"],
                            "label": "Specials" if s["seasonNumber"] == 0
                            else f"Season {s['seasonNumber']}",
                            "have": f"{st.get('episodeFileCount', 0)}/{st.get('episodeCount', 0)}"})
    return {"kind": kind, "id": item_id, "arr": k.arr.name, "seasons": seasons,
            "label": f"{item['title']} ({item.get('year') or '?'})",
            "hasFile": bool(item.get("hasFile")) if k.kind == "movie" else None,
            "downloads": downloads(stack, kind, item_id)}


def release_search(stack: Stack, kind: str, item_id: int, season: int | None = None) -> dict:
    """Ask Radarr/Sonarr to search every indexer now and return the releases for this title."""
    k = stack.kind(kind)
    if k.kind == "movie":
        raw = k.arr.get("/release", timeout=RELEASE_SEARCH_TIMEOUT, movieId=item_id)
        mine = [r for r in raw if r.get("mappedMovieId") == item_id]
    else:
        if season is None:
            raise LibraryError("pick a season to search")
        raw = k.arr.get("/release", timeout=RELEASE_SEARCH_TIMEOUT, seriesId=item_id,
                        seasonNumber=season)
        mine = [r for r in raw if r.get("mappedSeriesId") == item_id
                and r.get("mappedSeasonNumber") == season]
    current = {d["downloadId"] for d in downloads(stack, kind, item_id, season)}
    out = []
    for r in mine:
        if not r.get("downloadAllowed"):
            continue
        # "in queue"/"existing file meets cutoff" only say something is already downloading
        # or on disk; the dialog states that once instead of on every release.
        why = [x for x in r.get("rejections") or []
               if not any(t in x.lower() for t in ("in queue", "existing file", "meets cutoff"))]
        eps = r.get("mappedEpisodeNumbers") or []
        out.append({
            "guid": r["guid"], "indexerId": r["indexerId"], "title": r["title"],
            "indexer": (r.get("indexer") or "").replace(" (Prowlarr)", ""),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name", "?"),
            "size": r.get("size") or 0, "sizeHuman": human(r.get("size") or 0),
            "seeders": r.get("seeders"), "leechers": r.get("leechers"),
            "languages": [x["name"] for x in r.get("languages") or [] if x.get("name")],
            "age": r.get("age"), "rejections": why,
            "blocklisted": any("blocklist" in x.lower() or "blacklist" in x.lower() for x in why),
            "current": (r.get("infoHash") or "").lower() in current,
            "scope": ("season pack" if r.get("fullSeason") else
                      ", ".join(f"E{e:02d}" for e in eps)) if k.kind == "show" else ""})
    out.sort(key=lambda r: (r["current"], r["blocklisted"], bool(r["rejections"]),
                            k.kind == "show" and r["scope"] != "season pack",
                            -(r["seeders"] or 0)))
    return {"releases": out, "searched": len(raw)}


def _queue_ids(k: Kind, item_id: int, download_ids) -> list[int]:
    want = {d.lower() for d in download_ids}
    return [r["id"] for r in _queue_for(k, item_id)
            if (r.get("downloadId") or "").lower() in want]


def release_grab(stack: Stack, kind: str, item_id: int, guid: str, indexer_id: int,
                 replace=()) -> dict:
    """Download a specific release. `replace` = downloadIds to drop (and blocklist) after."""
    k = stack.kind(kind)
    old = _queue_ids(k, item_id, replace) if replace else []
    body = {"guid": guid, "indexerId": indexer_id}
    if k.kind == "movie":
        body["movieId"] = item_id
    k.arr.send("POST", "/release", body, timeout=120)
    msg = "Sent to qBittorrent. subgate checks it for English subs/audio first."
    if old:
        # Only after the new grab worked; skipRedownload so the Arr doesn't grab a third one.
        k.arr.send("DELETE", "/queue/bulk", {"ids": old}, removeFromClient="true",
                   blocklist="true", skipRedownload="true")
        msg = "Replaced the old download (blocklisted). " + msg
    return {"message": msg}


def release_drop(stack: Stack, kind: str, item_id: int, download_ids) -> dict:
    """Remove a download, blocklist it, and let the Arr search for the next best release."""
    k = stack.kind(kind)
    ids = _queue_ids(k, item_id, download_ids)
    if not ids:
        raise LibraryError("that download is no longer in the queue; refresh")
    k.arr.send("DELETE", "/queue/bulk", {"ids": ids}, removeFromClient="true",
               blocklist="true", skipRedownload="false")
    return {"message": f"Dropped and blocklisted; {k.arr.name} is searching for another release."}


# --------------------------------------------------------------------------
# Following: shows/anime whose new episodes Sonarr grabs as they come out
# --------------------------------------------------------------------------

FOLLOW_MODES = ("new", "season", "all")  # new episodes only / one season + new / everything


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _aired(ep: dict, now: float) -> bool:
    air = ep.get("airDateUtc")
    if not air:
        return False
    try:
        return calendar.timegm(time.strptime(air[:19], "%Y-%m-%dT%H:%M:%S")) <= now
    except ValueError:
        return False


def follow(stack: Stack, tvdb_id: int, mode: str, season: int | None = None) -> dict:
    """Make Sonarr grab this show's episodes as they air (adding it if needed)."""
    if mode not in FOLLOW_MODES:
        raise LibraryError(f"unknown follow mode {mode!r}")
    k = stack.kind("show")
    series = k.in_library(tvdb_id)
    added = series is None
    if added:
        look = k.lookup_id(tvdb_id)
        profile_id, root = k.profile_and_root()
        body = dict(look, qualityProfileId=profile_id, rootFolderPath=root, monitored=True,
                    seasonFolder=True, monitorNewItems="all",
                    addOptions={"monitor": "none", "searchForMissingEpisodes": False})
        if "anime" in [g.lower() for g in look.get("genres", [])]:
            body["seriesType"] = "anime"
        series = k.arr.send("POST", "/series", body)
    k.arr.run_command({"name": "RefreshSeries", "seriesId": series["id"]})
    series = k.item(series["id"])
    sid = series["id"]
    numbers = [x["seasonNumber"] for x in series.get("seasons", []) if x["seasonNumber"] > 0]
    latest = max(numbers) if numbers else 1
    target = season if season is not None else latest
    if mode == "season" and numbers and target not in numbers:
        raise LibraryError(f"{series['title']} has no season {target} on TVDB "
                           f"(seasons: {', '.join(map(str, numbers))})")

    now = time.time()
    episodes = k.arr.get("/episode", seriesId=sid)
    if mode == "all":
        pick = [e for e in episodes if e["seasonNumber"] > 0 or not _aired(e, now)]
    elif mode == "season":
        pick = [e for e in episodes if e["seasonNumber"] == target or not _aired(e, now)]
    else:
        pick = [e for e in episodes if not _aired(e, now)]
    ids = [e["id"] for e in pick]
    if ids:
        k.arr.send("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": True})

    # Episodes TVDB adds later inherit their season's flag, so the season(s) we care about
    # must be monitored too; monitorNewItems covers seasons that don't exist yet.
    want = {e["seasonNumber"] for e in pick} | {latest} | ({target} if mode == "season" else set())
    for x in series.get("seasons", []):
        if x["seasonNumber"] in want or (mode == "all" and x["seasonNumber"] > 0):
            x["monitored"] = True
    series.update(monitored=True, monitorNewItems="all")
    k.arr.send("PUT", f"/series/{sid}", series)

    if mode == "all":
        k.arr.send("POST", "/command", {"name": "SeriesSearch", "seriesId": sid})
    elif mode == "season":
        k.arr.send("POST", "/command", {"name": "SeasonSearch", "seriesId": sid,
                                        "seasonNumber": target})
    what = {"new": "new episodes as they air",
            "season": f"season {target} now, then new episodes as they air",
            "all": "every episode now, then new ones as they air"}[mode]
    return {"id": sid, "added": added, "label": f"{series['title']} ({series.get('year')})",
            "message": f"Following {series['title']}: Sonarr grabs {what}."}


def unfollow(stack: Stack, series_id: int) -> dict:
    k = stack.kind("show")
    series = k.item(series_id)
    series["monitored"] = False
    k.arr.send("PUT", f"/series/{series_id}", series)
    return {"message": f"Stopped following {series['title']}. Files already downloaded stay."}


def follow_candidates(stack: Stack, tvdb_id: int | None, query: str | None) -> list[dict]:
    """Sonarr matches for a seasonal entry: the mapped TVDB id, else a title search."""
    k = stack.kind("show")
    if tvdb_id:
        try:
            res = [k.lookup_id(tvdb_id)]
        except LibraryError:
            res = []
    else:
        res = []
    if not res and query:
        term = re.sub(r"\b(season|part|cour)\s*\d+\b|\b\d+(st|nd|rd|th)\s+season\b", "", query,
                      flags=re.I)
        term = re.sub(r"[:\-–]\s*$", "", term).strip() or query
        res = k.lookup(term)[:8]
    library = {i["tvdbId"]: i for i in k.items()}
    out = []
    for r in res:
        mine = library.get(r.get("tvdbId"))
        out.append({"tvdbId": r.get("tvdbId"), "title": r.get("title"), "year": r.get("year"),
                    "network": r.get("network"), "status": r.get("status"),
                    "poster": poster(r), "overview": (r.get("overview") or "")[:280],
                    "seasons": sorted(x["seasonNumber"] for x in r.get("seasons", [])
                                      if x["seasonNumber"] > 0),
                    "inLibrary": mine is not None,
                    "following": bool(mine and mine.get("monitored"))})
    return out


def following(stack: Stack, watcher=None, days_back: int = 2, days_ahead: int = 14) -> dict:
    """Followed shows plus recent/upcoming episodes and what is happening with each."""
    k = stack.kind("show")
    now = time.time()
    shows = []
    for s in sorted(k.items(), key=lambda i: i["title"].lower()):
        if not s.get("monitored"):
            continue
        st = s.get("statistics") or {}
        shows.append({"id": s["id"], "title": s["title"], "year": s.get("year"),
                      "poster": poster(s), "status": s.get("status"), "network": s.get("network"),
                      "anime": s.get("seriesType") == "anime",
                      "nextAiring": s.get("nextAiring"), "previousAiring": s.get("previousAiring"),
                      "episodes": f"{st.get('episodeFileCount', 0)}/{st.get('episodeCount', 0)}"})
    cal = k.arr.get("/calendar", start=_iso(now - days_back * 86400),
                    end=_iso(now + days_ahead * 86400), includeSeries="true",
                    unmonitored="false")
    torrents = {t["hash"].lower(): t for t in stack.qb.torrents()}
    queued = {}
    for r in k.arr.get("/queue", page=1, pageSize=1000).get("records", []):
        if r.get("episodeId"):
            t = torrents.get((r.get("downloadId") or "").lower())
            queued[r["episodeId"]] = t["progress"] if t else 0.0
    episodes = []
    for e in sorted(cal, key=lambda e: e.get("airDateUtc") or ""):
        ser = e.get("series") or {}
        if not ser.get("monitored", True):
            continue
        aired = _aired(e, now)
        if e.get("hasFile"):
            status = "downloaded"
        elif e["id"] in queued:
            status = f"downloading {queued[e['id']] * 100:.0f}%"
        elif not e.get("monitored"):
            status = "not wanted"
        elif aired:
            status = "aired, looking"
        else:
            status = "upcoming"
        info = None
        if watcher is not None and aired and status == "aired, looking":
            try:
                from watcher import parse_air
                info = watcher.episode_info(e["id"], parse_air(e.get("airDateUtc")), now)
            except Exception:  # watcher trouble must not break the page
                info = None
        episodes.append({"id": e["id"], "seriesId": e.get("seriesId"),
                         "series": ser.get("title", "?"), "poster": poster(ser),
                         "code": f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}",
                         "absolute": e.get("absoluteEpisodeNumber"),
                         "title": e.get("title") or "", "airDateUtc": e.get("airDateUtc"),
                         "aired": aired, "status": status, "watch": info})
    return {"shows": shows, "episodes": episodes}


def search_episodes(stack: Stack, episode_ids: list[int]) -> dict:
    if not episode_ids:
        raise LibraryError("no episodes given")
    stack.sonarr.send("POST", "/command", {"name": "EpisodeSearch", "episodeIds": episode_ids})
    return {"message": f"Searching every indexer for {len(episode_ids)} episode(s) now."}


# --------------------------------------------------------------------------
# Seasonal anime (AniList) mapped to TVDB/TMDB (Fribb/anime-lists)
# --------------------------------------------------------------------------

ANILIST_URL = "https://graphql.anilist.co"
ANIME_MAP_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json"
SEASONS = ("WINTER", "SPRING", "SUMMER", "FALL")
_UA = {"User-Agent": "media-library/1.0 (self-hosted)", "Accept": "application/json"}
_cache: dict = {}

_SEASON_QUERY = """query($s:MediaSeason,$y:Int,$p:Int){Page(page:$p,perPage:50){pageInfo{hasNextPage}
media(season:$s,seasonYear:$y,type:ANIME,sort:POPULARITY_DESC,isAdult:false){id format status
episodes duration title{romaji english} coverImage{large} genres averageScore popularity
studios(isMain:true){nodes{name}} nextAiringEpisode{episode airingAt} description(asHtml:false)
siteUrl}}}"""


def _cached(key, ttl: float, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fetch()
    except LibraryError:
        if hit:  # serve stale data rather than nothing
            return hit[1]
        raise
    _cache[key] = (time.time(), val)
    return val


def _fetch(url: str, body=None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(_UA, **({"Content-Type": "application/json"} if data else {}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LibraryError(f"{url.split('/')[2]} -> HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LibraryError(f"{url.split('/')[2]} unreachable ({e})") from None


def anime_map() -> dict:
    def load():
        return {e["anilist_id"]: e for e in _fetch(ANIME_MAP_URL) if e.get("anilist_id")}
    return _cached("anime-map", 24 * 3600, load)


def current_season(now: float | None = None) -> tuple[str, int]:
    t = time.localtime(now)
    return SEASONS[(t.tm_mon - 1) // 3], t.tm_year


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\(Source:[^)]*\)\s*$", "", text.strip(), flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def season_guess(title: str) -> int | None:
    m = re.search(r"\bseason\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+season\b", title or "", re.I)
    return int(m.group(1) or m.group(2)) if m else None


def seasonal_anime(stack: Stack, season: str | None = None, year: int | None = None) -> dict:
    cur = current_season()
    season, year = (season or cur[0]).upper(), int(year or cur[1])
    if season not in SEASONS:
        raise LibraryError(f"unknown season {season!r}")

    def load():
        media, page = [], 1
        while page <= 8:
            d = _fetch(ANILIST_URL, {"query": _SEASON_QUERY,
                                     "variables": {"s": season, "y": year, "p": page}})
            if d.get("errors"):
                raise LibraryError("AniList: " + "; ".join(x.get("message", "?") for x in d["errors"]))
            pg = d["data"]["Page"]
            media += pg["media"]
            if not pg["pageInfo"]["hasNextPage"]:
                break
            page += 1
        return media
    media = _cached(("season", season, year), 30 * 60, load)
    try:
        mapping = anime_map()
        map_error = None
    except LibraryError as e:
        mapping, map_error = {}, str(e)
    shows = {s["tvdbId"]: s for s in stack.kind("show").items()}
    movies = {m["tmdbId"]: m for m in stack.kind("movie").items()}
    items = []
    for m in media:
        ids = mapping.get(m["id"], {})
        title = m["title"].get("english") or m["title"].get("romaji") or "?"
        is_movie = m.get("format") == "MOVIE"
        tvdb = ids.get("tvdb_id") if isinstance(ids.get("tvdb_id"), int) else None
        tm = ids.get("themoviedb_id")  # {"movie": [id, ...]} or {"tv": id}
        tm_movies = (tm or {}).get("movie") if isinstance(tm, dict) else None
        tmdb = tm_movies[0] if isinstance(tm_movies, list) and tm_movies else None
        mine = movies.get(tmdb) if is_movie else shows.get(tvdb)
        nxt = m.get("nextAiringEpisode") or {}
        items.append({
            "anilistId": m["id"], "title": title, "romaji": m["title"].get("romaji"),
            "cover": (m.get("coverImage") or {}).get("large"), "format": m.get("format"),
            "status": m.get("status"), "episodes": m.get("episodes"),
            "genres": m.get("genres") or [], "score": m.get("averageScore"),
            "popularity": m.get("popularity"),
            "studios": [n["name"] for n in (m.get("studios") or {}).get("nodes", [])],
            "nextEpisode": nxt.get("episode"), "nextAiringAt": nxt.get("airingAt"),
            "synopsis": _strip_html(m.get("description") or "")[:600], "url": m.get("siteUrl"),
            "movie": is_movie, "tvdbId": tvdb, "tmdbId": tmdb,
            "tvdbSeason": (ids.get("season") or {}).get("tvdb") or season_guess(title),
            "inLibrary": mine is not None, "libraryId": (mine or {}).get("id"),
            "following": bool(mine and mine.get("monitored")) if not is_movie else False})
    i = SEASONS.index(season)
    prev = (SEASONS[i - 1], year - 1 if i == 0 else year)
    nxt = (SEASONS[(i + 1) % 4], year + 1 if i == 3 else year)
    return {"season": season, "year": year, "items": items, "mapError": map_error,
            "prev": {"season": prev[0], "year": prev[1]},
            "next": {"season": nxt[0], "year": nxt[1]},
            "current": {"season": cur[0], "year": cur[1]}}


# --------------------------------------------------------------------------
# Watchlist: titles to remember without downloading (storage in watchlist.py)
# --------------------------------------------------------------------------

def watchlist_view(stack: Stack, wl) -> dict:
    """Watchlist items, each marked with whether Radarr/Sonarr already have it."""
    items = wl.items()
    have: dict[str, dict] = {}
    for kind in {it["kind"] for it in items}:
        k = stack.kind(kind)
        have[kind] = {i.get(k.id_key): i["id"] for i in k.items()}
    for it in items:
        lib_id = have.get(it["kind"], {}).get(it["extId"])
        it["inLibrary"], it["libraryId"] = lib_id is not None, lib_id
    return {"items": items}


def watchlist_send(stack: Stack, wl, keys: list[tuple[str, int]], log=print) -> dict:
    """Add titles to Radarr/Sonarr and search now; each one sent leaves the list."""
    sent, failed = [], []
    for kind, ext_id in keys:
        try:
            res = add_title(stack, kind, ext_id)
        except LibraryError as e:
            failed.append({"kind": kind, "extId": ext_id, "error": str(e)})
            log(f"Failed {kind} {ext_id}: {e}")
            continue
        wl.remove(kind, ext_id)
        sent.append(res["label"])
        log(res["message"])
    return {"ok": not failed, "sent": sent, "failed": failed,
            "message": f"Sent {len(sent)} to Radarr/Sonarr"
                       + (f", {len(failed)} failed" if failed else "") + "."}


# --------------------------------------------------------------------------
# Orphans
# --------------------------------------------------------------------------

def orphan_list(stack: Stack) -> list[dict]:
    claimed: set[str] = set()
    for kind in ("movie", "show"):
        k = stack.kind(kind)
        queue = k.arr.queue()
        claimed |= {r["downloadId"].lower() for r in queue if r.get("downloadId")}
        for item in k.items():
            claimed |= k.hashes(item, queue)
    return [{"hash": t["hash"], "name": t["name"], "size": t["size"],
             "sizeHuman": human(t["size"]), "category": t["category"], "state": t["state"],
             "content_path": t.get("content_path")}
            for t in stack.qb.torrents() if t["hash"].lower() not in claimed]


def orphans_delete(stack: Stack, hashes: list[str]) -> dict:
    current = {o["hash"]: o for o in orphan_list(stack)}
    targets = [current[h] for h in hashes if h in current]
    if len(targets) != len(hashes):
        raise LibraryError("some torrents are no longer orphans; refresh and try again")
    if not targets:
        return {"ok": True, "still": [], "freedHuman": human(0)}
    stack.qb.delete([t["hash"] for t in targets])
    time.sleep(3)
    left = {t["hash"] for t in stack.qb.torrents()}
    still = [t["name"] for t in targets if t["hash"] in left
             or (t.get("content_path") and stack.volume.exists(t["content_path"]))]
    return {"ok": not still, "still": still,
            "freedHuman": human(sum(t["size"] for t in targets if t["name"] not in still))}


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def walk(src: str) -> list[str]:
    if os.path.isfile(src):
        return [src]
    out = []
    for d, dirs, files in os.walk(src):
        dirs.sort()
        out += [os.path.join(d, f) for f in sorted(files)]
    return out


def local_videos(src: str) -> list[str]:
    return [p for p in walk(src) if _ext(p) in VIDEO_EXTS
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
        if _ext(p) not in SUB_EXTS:
            continue
        sub_stem = os.path.splitext(os.path.basename(p))[0]
        if sub_stem.startswith(stem):
            out.append((p, sub_stem[len(stem):]))
        elif whole_dir:
            out.append((p, "." + sub_stem))
    return out


def parse_sxxexx(name: str) -> tuple[int, list[int]] | None:
    m = re.search(r"[Ss](\d{1,2})[ ._-]?((?:[Ee]\d{1,3}[ ._-]?)+)", name)
    if not m:
        return None
    return int(m.group(1)), sorted(int(e) for e in re.findall(r"\d+", m.group(2)))


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
        if k.kind == "movie":
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
    files = [{"src": video, "dest": f"{folder}/{base}{_ext(video)}",
              "size": os.path.getsize(video)}]
    for s, suffix in local_subs(path, video, os.path.isdir(path)):
        files.append({"src": s, "dest": f"{folder}/{base}{suffix}{_ext(s)}",
                      "size": os.path.getsize(s)})
    return files


def _show_files(k: Kind, series: dict, path: str, replace: bool,
                episodes: list[dict] | None) -> tuple[list[dict], list[dict]]:
    known = {(e["seasonNumber"], e["episodeNumber"]) for e in episodes} if episodes else None
    have = {(e["seasonNumber"], e["episodeNumber"]) for e in episodes or [] if e.get("hasFile")}
    title = safe_name(series["title"])
    folder = series.get("path") or f"{k.profile_and_root()[1]}/{title}"
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
        files.append({"src": v, "dest": base + _ext(v), "size": os.path.getsize(v),
                      "tag": tag, "season": season, "episodes": eps})
        for s, suffix in local_subs(path, v, False):
            files.append({"src": s, "dest": f"{base}{suffix}{_ext(s)}",
                          "size": os.path.getsize(s), "sub": True})
    return files, skipped


def import_plan(stack: Stack, kind: str, path: str, ext_id: int, replace: bool = False) -> dict:
    """Preview an import without changing anything."""
    k = stack.kind(kind)
    existing = k.in_library(ext_id)
    item = existing or k.lookup_id(ext_id)
    warnings, skipped = [], []
    if k.kind == "movie":
        base = f"{safe_name(item['title'])} ({item['year']})"
        folder = item.get("path") or f"{k.profile_and_root()[1]}/{base}"
        files = _movie_files(path, base, folder)
        if existing and existing.get("hasFile") and not replace:
            raise LibraryError(f"{k.label(item)} already has a file in the library; "
                               "use replace to swap it")
        if existing and any(r.get("movieId") == existing["id"] for r in k.arr.queue()):
            warnings.append(f"{k.arr.name} is downloading a release for this movie; "
                            "it will be cancelled (not blocklisted).")
    else:
        episodes = k.arr.get("/episode", seriesId=existing["id"]) if existing else None
        files, skipped = _show_files(k, item, path, replace, episodes)
        folder = item.get("path") or f"{k.profile_and_root()[1]}/{safe_name(item['title'])}"
        if not existing:
            warnings.append(f"{item['title']} will be added to Sonarr unmonitored (no downloads).")
        if not [f for f in files if not f.get("sub")]:
            raise LibraryError("nothing to import: " + "; ".join(
                f"{os.path.basename(s['src'])}: {s['why']}" for s in skipped))
    need = sum(f["size"] for f in files)
    free = min(r.get("freeSpace") or 0 for r in k.arr.get("/rootfolder"))
    if free and need > free - 2 * 1024**3:
        raise LibraryError(f"not enough space in the volume: need {human(need)}, "
                           f"{human(free)} free")
    return {"kind": kind, "path": path, "extId": ext_id, "replace": replace,
            "label": k.label(item), "folder": folder, "inLibrary": bool(existing),
            "files": [dict(f, sizeHuman=human(f["size"]), name=os.path.basename(f["src"]))
                      for f in files],
            "skipped": [dict(s, name=os.path.basename(s["src"])) for s in skipped],
            "warnings": warnings, "total": need, "totalHuman": human(need)}


def import_execute(stack: Stack, kind: str, path: str, ext_id: int, replace: bool = False,
                   monitor: bool = False, log=print, progress=None) -> dict:
    """Do the import. Re-plans after adding, so episode matching uses real data."""
    k = stack.kind(kind)
    item = k.in_library(ext_id)
    if not item:
        log(f"Adding to {k.arr.name} (unmonitored, no search)")
        item = k.add(k.lookup_id(ext_id), search=False)
        if k.kind == "show":
            # A new series' episode list loads in the background; matching
            # filenames before it exists finds nothing.
            k.arr.run_command({"name": "RefreshSeries", "seriesId": item["id"]})
    plan = import_plan(stack, kind, path, ext_id, replace)

    if k.kind == "movie":
        queued = [r["id"] for r in k.arr.queue() if r.get("movieId") == item["id"]]
        if queued:
            log("Cancelling the running download for this movie")
            k.arr.send("DELETE", "/queue/bulk", {"ids": queued}, removeFromClient="true",
                       blocklist="false", skipRedownload="true")
        if replace and item.get("hasFile"):
            log("Removing the old library file")
            k.arr.send("DELETE", f"/moviefile/{item['movieFile']['id']}")
    elif replace:
        tags = {(f["season"], e) for f in plan["files"] if not f.get("sub") for e in f["episodes"]}
        old = {e["episodeFileId"] for e in k.arr.get("/episode", seriesId=item["id"])
               if e.get("hasFile") and (e["seasonNumber"], e["episodeNumber"]) in tags}
        for fid in old:
            k.arr.send("DELETE", f"/episodefile/{fid}")

    total = plan["total"] or 1
    copied = 0
    for f in plan["files"]:
        log(f"Copying {f['name']} -> {f['dest']}")
        base = copied

        def prog(done, _size, base=base):
            if progress:
                progress(base + done, total)
        stack.volume.copy_in(kind, f["src"], f["dest"], prog)
        copied += f["size"]

    log(f"Asking {k.arr.name} to register the files")
    if k.kind == "movie":
        k.arr.run_command({"name": "RescanMovie", "movieId": item["id"]})
        item = k.item(item["id"])
        if not monitor and item.get("monitored"):
            k.arr.send("PUT", f"/movie/{item['id']}", dict(item, monitored=False))
        if not item.get("hasFile"):
            raise LibraryError(f"copied, but {k.arr.name} didn't register the file; "
                               f"check {plan['folder']}")
        done = item["movieFile"]["relativePath"]
    else:
        k.arr.run_command({"name": "RescanSeries", "seriesId": item["id"]})
        got = {(e["seasonNumber"], e["episodeNumber"])
               for e in k.arr.get("/episode", seriesId=item["id"]) if e.get("hasFile")}
        missing = [f["tag"] for f in plan["files"] if not f.get("sub")
                   and any((f["season"], e) not in got for e in f["episodes"])]
        if missing:
            raise LibraryError(f"copied, but {k.arr.name} didn't register: {', '.join(missing)}")
        done = f"{len([f for f in plan['files'] if not f.get('sub')])} episode file(s)"
    return {"ok": True, "label": plan["label"], "imported": done,
            "skipped": plan["skipped"], "folder": plan["folder"]}

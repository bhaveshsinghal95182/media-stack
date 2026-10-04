#!/usr/bin/env python3
"""Web page for the media library: list, add, delete, import, clean up, pick releases.

Stdlib only. Password-protected (HTTP Basic, LIBRARY_USERNAME/LIBRARY_PASSWORD) because it is
reachable from the LAN and can delete files. State-changing requests must
carry an X-Requested-With header, which cross-site pages can't send, and run
one at a time as background jobs the page polls.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import watchlist  # noqa: E402
from core import LibraryError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PASSWORD = os.environ.get("LIBRARY_PASSWORD", "")
USERNAME = os.environ.get("LIBRARY_USERNAME", "")  # empty: any username
PORT = int(os.environ.get("LIBRARY_PORT", "8099"))
IMPORT_ROOT = os.path.realpath(os.environ.get("IMPORT_ROOT", "/import"))
BROWSE_SCAN_LIMIT = 5000  # files examined per folder when counting videos

WATCHER = None  # airing watcher (watcher.py), started in main()
WATCHLIST = watchlist.Watchlist(None)  # replaced by the persistent one in main()
_local = threading.local()
_job_lock = threading.Lock()  # one state-changing job at a time
_jobs: dict[str, dict] = {}
_fails: dict[str, list[float]] = {}


def stack() -> core.Stack:
    if not hasattr(_local, "stack"):
        _local.stack = core.Stack()
    return _local.stack


# --------------------------------------------------------------------------
# Import folder browsing (only folders, videos and subtitles are ever shown)
# --------------------------------------------------------------------------

def resolve_import_path(rel: str) -> str:
    path = os.path.realpath(os.path.join(IMPORT_ROOT, rel.lstrip("/")))
    if path != IMPORT_ROOT and not path.startswith(IMPORT_ROOT + os.sep):
        raise LibraryError("path is outside the import folder")
    if not os.path.exists(path):
        raise LibraryError("no such file or folder")
    return path


def _count_videos(path: str) -> tuple[int, int]:
    n = size = seen = 0
    for d, _, files in os.walk(path):
        for f in files:
            seen += 1
            if seen > BROWSE_SCAN_LIMIT:
                return n, size
            if os.path.splitext(f)[1].lower() in core.VIDEO_EXTS:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(d, f))
                except OSError:
                    pass
    return n, size


def browse(rel: str) -> dict:
    path = resolve_import_path(rel)
    if not os.path.isdir(path):
        raise LibraryError("not a folder")
    entries = []
    for name in sorted(os.listdir(path), key=str.lower):
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        ext = os.path.splitext(name)[1].lower()
        real = os.path.realpath(full)
        if not real.startswith(IMPORT_ROOT + os.sep):  # symlinks leading out stay hidden
            continue
        try:
            if os.path.isdir(full):
                n, size = _count_videos(full)
                if n:  # folders without videos stay hidden
                    entries.append({"name": name, "type": "dir", "videos": n,
                                    "sizeHuman": core.human(size)})
            elif ext in core.VIDEO_EXTS:
                entries.append({"name": name, "type": "video",
                                "sizeHuman": core.human(os.path.getsize(full))})
            elif ext in core.SUB_EXTS:
                entries.append({"name": name, "type": "sub"})
        except OSError:
            continue
    relpath = os.path.relpath(path, IMPORT_ROOT)
    relpath = "" if relpath == "." else relpath
    return {"path": relpath, "parent": None if not relpath else os.path.dirname(relpath),
            "entries": entries}


def strip_paths(obj):
    """Show import paths relative to the import folder, not container paths."""
    if isinstance(obj, dict):
        return {k: strip_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_paths(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(IMPORT_ROOT + os.sep):
        return "Downloads/" + obj[len(IMPORT_ROOT) + 1:]
    return obj


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def start_job(title: str, fn) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "title": title, "status": "queued", "log": [], "progress": None,
           "result": None, "error": None, "started": time.time()}
    _jobs[job_id] = job

    def log(msg: str) -> None:
        job["log"].append(strip_paths(msg))

    def progress(done: int, total: int) -> None:
        job["progress"] = {"done": done, "total": total,
                           "text": f"{core.human(done)} / {core.human(total)}"}

    def run() -> None:
        with _job_lock:
            job["status"] = "running"
            try:
                job["result"] = strip_paths(fn(log, progress))
                job["status"] = "done"
            except LibraryError as e:
                job["error"], job["status"] = str(e), "error"
            except Exception as e:  # keep the server alive, show the failure
                traceback.print_exc()
                job["error"], job["status"] = f"{type(e).__name__}: {e}", "error"
        cutoff = time.time() - 6 * 3600
        for k in [k for k, j in _jobs.items() if j["started"] < cutoff]:
            _jobs.pop(k, None)

    threading.Thread(target=run, daemon=True).start()
    return job_id


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "media-library"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers --

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authorized(self) -> bool:
        ip = self.client_address[0]
        recent = [t for t in _fails.get(ip, []) if t > time.time() - 600]
        _fails[ip] = recent
        if len(recent) >= 10:
            self._send(429, b"Too many failed logins; try again in 10 minutes.", "text/plain")
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, given = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                user = given = ""
            user_ok = not USERNAME or hmac.compare_digest(user.encode(), USERNAME.encode())
            if PASSWORD and hmac.compare_digest(given.encode(), PASSWORD.encode()) and user_ok:
                return True
            recent.append(time.time())
            time.sleep(1)
        self._send(401, b"Login required", "text/plain",
                   {"WWW-Authenticate": 'Basic realm="Media Library", charset="UTF-8"'})
        return False

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[-1] for k, v in urllib.parse.parse_qs(q).items()}

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise LibraryError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routes --

    def do_GET(self):
        if not self._authorized():
            return
        route = urllib.parse.urlparse(self.path).path
        q = self._query()
        try:
            if route in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if route == "/api/overview":
                return self._json(200, core.overview(stack()))
            if route == "/api/search":
                k = stack().kind(q.get("kind", ""))
                term = q.get("q", "").strip()
                res = k.lookup(term)[:12] if term else []
                return self._json(200, [dict(core.summarize_lookup(k, r), onWatchlist=WATCHLIST.has(
                    k.kind, r.get(k.id_key))) for r in res])
            if route == "/api/delete-plan":
                return self._json(200, core.delete_plan(stack(), q["kind"], int(q["id"])))
            if route == "/api/orphans":
                return self._json(200, core.orphan_list(stack()))
            if route == "/api/browse":
                return self._json(200, browse(q.get("path", "")))
            if route == "/api/import/candidates":
                path = resolve_import_path(q.get("path", ""))
                year = int(q["year"]) if q.get("year") else None
                return self._json(200, strip_paths(core.import_candidates(
                    stack(), q["kind"], path, q.get("q") or None, year)))
            if route == "/api/import/plan":
                path = resolve_import_path(q.get("path", ""))
                plan = core.import_plan(stack(), q["kind"], path, int(q["extId"]),
                                        q.get("replace") == "1")
                return self._json(200, strip_paths(plan))
            if route == "/api/seasonal":
                year = int(q["year"]) if q.get("year") else None
                data = dict(core.seasonal_anime(stack(), q.get("season"), year))
                data["items"] = [dict(a, onWatchlist=WATCHLIST.has(*(
                    ("movie", a.get("tmdbId")) if a.get("movie") else ("show", a.get("tvdbId")))))
                    for a in data["items"]]
                return self._json(200, data)
            if route == "/api/watchlist":
                return self._json(200, core.watchlist_view(stack(), WATCHLIST))
            if route == "/api/following":
                data = core.following(stack(), WATCHER)
                data["watcher"] = WATCHER.status() if WATCHER else None
                return self._json(200, data)
            if route == "/api/follow/candidates":
                tvdb = int(q["tvdbId"]) if q.get("tvdbId") else None
                return self._json(200, core.follow_candidates(stack(), tvdb, q.get("q")))
            if route == "/api/releases":
                return self._json(200, core.release_info(stack(), q["kind"], int(q["id"])))
            if route == "/api/releases/search":
                season = int(q["season"]) if q.get("season", "") != "" else None
                return self._json(200, core.release_search(stack(), q["kind"], int(q["id"]),
                                                           season))
            if route.startswith("/api/jobs/"):
                job = _jobs.get(route.rsplit("/", 1)[1])
                return self._json(200, job) if job else self._json(404, {"error": "no such job"})
            if route == "/api/jobs":
                return self._json(200, sorted(_jobs.values(), key=lambda j: -j["started"])[:20])
            return self._json(404, {"error": "not found"})
        except (LibraryError, KeyError, ValueError) as e:
            return self._json(400, {"error": str(e) if isinstance(e, LibraryError)
                                    else f"bad request ({e})"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.headers.get("X-Requested-With") != "media-library":
            return self._json(403, {"error": "missing X-Requested-With"})
        route = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if route == "/api/add":
                res = core.add_title(stack(), body["kind"], int(body["extId"]))
                return self._json(200, res)
            if route == "/api/watchlist/add":
                kind, ext_id = str(body["kind"]), body["extId"]
                new = WATCHLIST.add(kind, ext_id, body.get("title") or "", body.get("year"),
                                    body.get("poster"), body.get("overview"))
                title = " ".join(str(body.get("title") or "It").split())
                return self._json(200, {"added": new, "message": f"{title} saved to your watchlist."
                                        if new else f"{title} was already on your watchlist."})
            if route == "/api/watchlist/remove":
                gone = WATCHLIST.remove(str(body["kind"]), body["extId"])
                return self._json(200, {"removed": gone, "message": "Removed from your watchlist."
                                        if gone else "It wasn't on your watchlist."})
            if route == "/api/watchlist/send":
                keys = [(str(i["kind"]), int(i["extId"])) for i in body["items"]]
                if not keys:
                    raise LibraryError("nothing selected")
                return self._json(200, {"job": start_job(
                    f"Download {len(keys)} from the watchlist",
                    lambda log, _p: core.watchlist_send(core.Stack(), WATCHLIST, keys, log))})
            if route == "/api/follow":
                season = body.get("season")
                tvdb, mode = int(body["tvdbId"]), str(body["mode"])
                return self._json(200, {"job": start_job(
                    "Follow show", lambda log, _p: core.follow(
                        core.Stack(), tvdb, mode, int(season) if season not in (None, "") else None))})
            if route == "/api/unfollow":
                with _job_lock:
                    return self._json(200, core.unfollow(stack(), int(body["id"])))
            if route == "/api/episodes/search":
                return self._json(200, core.search_episodes(
                    stack(), [int(i) for i in body["ids"]]))
            if route == "/api/releases/grab":
                with _job_lock:
                    return self._json(200, core.release_grab(
                        stack(), body["kind"], int(body["id"]), str(body["guid"]),
                        int(body["indexerId"]), [str(d) for d in body.get("replace") or []]))
            if route == "/api/releases/drop":
                with _job_lock:
                    return self._json(200, core.release_drop(
                        stack(), body["kind"], int(body["id"]),
                        [str(d) for d in body["downloadIds"]]))
            if route == "/api/delete":
                kind, item_id, exclude = body["kind"], int(body["id"]), bool(body.get("exclude"))
                label = core.delete_plan(stack(), kind, item_id)["label"]

                def do_delete(log, _progress):
                    st = core.Stack()  # job threads get their own clients
                    return core.delete_execute(st, core.delete_plan(st, kind, item_id),
                                               exclude, log)
                return self._json(200, {"job": start_job(f"Delete {label}", do_delete)})
            if route == "/api/orphans/delete":
                hashes = [str(h) for h in body["hashes"]]
                return self._json(200, {"job": start_job(
                    f"Delete {len(hashes)} orphan torrent(s)",
                    lambda log, _p: core.orphans_delete(core.Stack(), hashes))})
            if route == "/api/import":
                kind, ext_id = body["kind"], int(body["extId"])
                path = resolve_import_path(body["path"])
                replace = bool(body.get("replace"))

                def do_import(log, progress):
                    return core.import_execute(core.Stack(), kind, path, ext_id, replace,
                                               False, log, progress)
                return self._json(200, {"job": start_job(
                    f"Import {os.path.basename(path)}", do_import)})
            return self._json(404, {"error": "not found"})
        except (LibraryError, KeyError, ValueError) as e:
            return self._json(400, {"error": str(e) if isinstance(e, LibraryError)
                                    else f"bad request ({e})"})
        except OSError as e:  # the watchlist file couldn't be saved
            return self._json(500, {"error": f"couldn't save: {e}"})


def _quiet(fn) -> None:
    try:
        fn()
    except Exception as e:  # only a warm-up; the tab retries on demand
        print(f"warm-up failed: {e}", flush=True)


def main() -> None:
    if not PASSWORD:
        sys.exit("LIBRARY_PASSWORD is not set")
    core.Stack()  # fail fast on missing settings
    # Warm the AniList->TVDB mapping (5-6 MB) so the Seasonal tab opens quickly.
    threading.Thread(target=lambda: _quiet(core.anime_map), daemon=True).start()
    global WATCHER, WATCHLIST
    WATCHLIST = watchlist.Watchlist(os.environ.get("WATCHLIST_STATE", "/state/watchlist.json"))
    if os.environ.get("LIBRARY_WATCHER", "1") != "0":
        try:
            import watcher
            WATCHER = watcher.AiringWatcher(core.Stack, os.environ.get(
                "WATCHER_STATE", "/state/watcher.json"))
            WATCHER.start()
            print("airing watcher on", flush=True)
        except ImportError:
            print("watcher.py missing; airing watcher off", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"media-library on :{PORT} (import folder {IMPORT_ROOT})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

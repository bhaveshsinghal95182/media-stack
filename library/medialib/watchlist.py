"""Keep a persistent list of movies and shows to remember for later.

Items can also be filed into playlists. The watchlist stays the master list:
deleting a playlist unfiles its titles, it never removes them.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable

from medialib import titles
from medialib.errors import LibraryError
from medialib.state import save_json


KINDS = ("movie", "show")
MAX_TITLE = 200
MAX_OVERVIEW = 300
MAX_PLAYLIST_NAME = 60
MAX_PLAYLIST_KEY = 40
VERSION = 2  # v1 was {version, items}; v2 adds the playlists map and item memberships

_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,%d}" % (MAX_PLAYLIST_KEY - 1))


def _ext_id(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("ext_id must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        try:
            result = int(value)
        except ValueError as exc:
            raise ValueError("ext_id must be a positive integer") from exc
    else:
        raise ValueError("ext_id must be a positive integer")
    if result <= 0:
        raise ValueError("ext_id must be a positive integer")
    return result


def _year(value: int | str | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _keys(value) -> list[str]:
    """Playlist keys from stored/user input: valid, de-duplicated, in order."""
    if not isinstance(value, (list, tuple)):
        return []
    keys: list[str] = []
    for key in value:
        if isinstance(key, str) and _KEY.fullmatch(key) and key not in keys:
            keys.append(key)
    return keys


def _playlist_name(value) -> str:
    name = " ".join(str(value).split())
    if not name:
        raise ValueError("playlist name must not be blank")
    return name[:MAX_PLAYLIST_NAME]


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return (slug or "list")[:MAX_PLAYLIST_KEY]


def _item(kind: str, ext_id: int, title: str, year: int | None,
          poster: str | None, overview: str | None, added: float,
          playlists: list[str] | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ext_id = _ext_id(ext_id)
    title = " ".join(str(title).split())
    if not title:
        raise ValueError("title must not be blank")
    title = title[:MAX_TITLE]
    year = _year(year)
    if poster is not None:
        poster = str(poster).strip() or None
    if overview is not None:
        overview = str(overview).strip()[:MAX_OVERVIEW] or None
    return {"kind": kind, "extId": ext_id, "title": title, "year": year,
            "poster": poster, "overview": overview, "added": added,
            "playlists": _keys(playlists)}


def _copy(item: dict) -> dict:
    return {**item, "playlists": list(item["playlists"])}


def _playlist(key: str, value, created: float) -> dict | None:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    name = value.get("name") if isinstance(value, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    return {"key": key, "name": name.strip()[:MAX_PLAYLIST_NAME], "created": created}


class Watchlist:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self._items: list[dict] = []
        self._playlists: dict[str, dict] = {}
        self._writable = True
        if path is None:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or type(data.get("version")) is not int:
                raise ValueError("invalid watchlist format")
            version = data["version"]
            expected = {"version", "items"} if version == 1 else {"version", "items", "playlists"}
            if version not in (1, 2) or set(data) != expected or not isinstance(data["items"], list):
                raise ValueError("invalid watchlist format")
            if version == 2 and not isinstance(data["playlists"], dict):
                raise ValueError("invalid watchlist format")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            try:
                os.replace(path, f"{path}.corrupt-{int(time.time())}")
            except OSError:
                self._writable = False
            return

        for key, value in (data.get("playlists") or {}).items():
            created = value.get("created") if isinstance(value, dict) else None
            if isinstance(created, bool) or not isinstance(created, (int, float)):
                created = 0.0
            entry = _playlist(key, value, float(created))
            if entry:
                self._playlists[entry["key"]] = entry

        seen: set[tuple[str, int]] = set()
        for entry in data["items"]:
            if not isinstance(entry, dict):
                continue
            try:
                added = entry["added"]
                if isinstance(added, bool) or not isinstance(added, (int, float)):
                    continue
                item = _item(entry.get("kind"), entry.get("extId"),
                             entry.get("title", ""), entry.get("year"),
                             entry.get("poster"), entry.get("overview"), float(added),
                             [k for k in _keys(entry.get("playlists"))
                              if k in self._playlists])
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            identity = (item["kind"], item["extId"])
            if identity not in seen:
                self._items.append(item)
                seen.add(identity)

    # ----------------------------------------------------------------------
    # Titles
    # ----------------------------------------------------------------------

    def items(self) -> list[dict]:
        with self.lock:
            return [_copy(item) for item in sorted(
                self._items, key=lambda item: -item["added"])]

    def get(self, kind: str, ext_id: int) -> dict | None:
        try:
            identity = (kind, _ext_id(ext_id))
        except (ValueError, TypeError, OverflowError):
            return None
        with self.lock:
            return next((_copy(item) for item in self._items
                         if (item["kind"], item["extId"]) == identity), None)

    def has(self, kind: str, ext_id: int) -> bool:
        return self.get(kind, ext_id) is not None

    def add(self, kind: str, ext_id: int, title: str, year: int | None = None,
            poster: str | None = None, overview: str | None = None,
            playlists: list[str] | None = None, now: float | None = None) -> bool:
        """Add a title, or update its metadata. Playlists=None leaves memberships alone."""
        item = _item(kind, ext_id, title, year, poster, overview,
                     now if now is not None else time.time(), playlists)
        with self.lock:
            keys = [k for k in item["playlists"] if k in self._playlists]
            for index, current in enumerate(self._items):
                if (current["kind"], current["extId"]) == (item["kind"], item["extId"]):
                    updated = _copy(current)
                    updated["title"] = item["title"]
                    for field in ("year", "poster", "overview"):
                        if item[field] is not None:
                            updated[field] = item[field]
                    if playlists is not None:
                        updated["playlists"] = keys
                    if updated != current:
                        new_items = self._items.copy()
                        new_items[index] = updated
                        self._commit(new_items)
                    return False
            item["playlists"] = keys
            self._commit(self._items + [item])
            return True

    def remove(self, kind: str, ext_id: int) -> bool:
        try:
            identity = (kind, _ext_id(ext_id))
        except (ValueError, TypeError, OverflowError):
            return False
        with self.lock:
            new_items = [item for item in self._items
                         if (item["kind"], item["extId"]) != identity]
            if len(new_items) == len(self._items):
                return False
            self._commit(new_items)
            return True

    def set_playlists(self, kind: str, ext_id: int, keys: list[str]) -> bool:
        """Replace an item's playlists. True when the membership changed."""
        try:
            identity = (kind, _ext_id(ext_id))
        except (ValueError, TypeError, OverflowError):
            return False
        with self.lock:
            wanted = [k for k in _keys(keys) if k in self._playlists]
            for index, current in enumerate(self._items):
                if (current["kind"], current["extId"]) != identity:
                    continue
                if current["playlists"] == wanted:
                    return False
                new_items = self._items.copy()
                new_items[index] = {**current, "playlists": wanted}
                self._commit(new_items)
                return True
            return False

    # ----------------------------------------------------------------------
    # Playlists
    # ----------------------------------------------------------------------

    def playlists(self) -> list[dict]:
        """Every playlist, oldest first, each with how many titles it holds."""
        with self.lock:
            counts = Counter(key for item in self._items for key in item["playlists"])
            return [{**entry, "count": counts.get(key, 0)}
                    for key, entry in sorted(self._playlists.items(),
                                             key=lambda kv: (kv[1]["created"], kv[1]["name"]))]

    def playlist(self, key: str) -> dict | None:
        with self.lock:
            entry = self._playlists.get(key)
            return dict(entry) if entry else None

    def create_playlist(self, name: str) -> dict:
        """Create a playlist, or return the existing one when the name matches."""
        name = _playlist_name(name)
        with self.lock:
            for entry in self._playlists.values():
                if entry["name"].casefold() == name.casefold():
                    return dict(entry)
            existing = set(self._playlists)
            key = _slug(name)
            suffix = 2
            while key in existing:
                key = f"{_slug(name)[:MAX_PLAYLIST_KEY - 2]}-{suffix}"
                suffix += 1
            entry = {"key": key, "name": name, "created": time.time()}
            self._commit(self._items, {**self._playlists, key: entry})
            return dict(entry)

    def rename_playlist(self, key: str, name: str) -> bool:
        name = _playlist_name(name)
        with self.lock:
            entry = self._playlists.get(key)
            if entry is None or entry["name"] == name:
                return False
            if any(k != key and other["name"].casefold() == name.casefold()
                   for k, other in self._playlists.items()):
                raise ValueError(f"there is already a playlist called {name}")
            renamed = {**entry, "name": name}
            self._commit(self._items, {**self._playlists, key: renamed})
            return True

    def delete_playlist(self, key: str) -> bool:
        """Drop a playlist; its titles stay on the watchlist, just unfiled."""
        with self.lock:
            if key not in self._playlists:
                return False
            playlists = {k: v for k, v in self._playlists.items() if k != key}
            items = [{**item, "playlists": [k for k in item["playlists"] if k != key]}
                     for item in self._items]
            self._commit(items, playlists)
            return True

    def add_to_playlists(self, keys: list[tuple[str, int]], playlist_key: str) -> int:
        """File several titles into one playlist (additive). Returns how many changed."""
        wanted = set(keys)
        with self.lock:
            if playlist_key not in self._playlists:
                raise ValueError("no such playlist")
            new_items = list(self._items)
            changed = 0
            for index, item in enumerate(new_items):
                identity = (item["kind"], item["extId"])
                if identity in wanted and playlist_key not in item["playlists"]:
                    new_items[index] = {**item,
                                        "playlists": item["playlists"] + [playlist_key]}
                    changed += 1
            if changed:
                self._commit(new_items)
            return changed

    # ----------------------------------------------------------------------
    # Saving
    # ----------------------------------------------------------------------

    def _commit(self, items: list[dict], playlists: dict[str, dict] | None = None) -> None:
        playlists = self._playlists if playlists is None else playlists
        self._save_locked(items, playlists)
        self._items, self._playlists = items, playlists

    def _save_locked(self, items: list[dict], playlists: dict[str, dict]) -> None:
        if self.path is None:
            return
        if not self._writable:
            raise OSError("cannot save watchlist because its original file was not preserved")
        save_json(self.path, {"version": VERSION, "playlists": playlists, "items": items},
                  indent=1)


# --------------------------------------------------------------------------
# The watchlist against the library
# --------------------------------------------------------------------------

def watchlist_view(stack, wl: Watchlist) -> list[dict]:
    """Watchlist items, each marked with whether Radarr/Sonarr already have it."""
    items = wl.items()
    have: dict[str, set[int]] = {}
    for kind in {it["kind"] for it in items}:
        k = stack.kind(kind)
        have[kind] = {i.get(k.id_key) for i in k.items()}
    for it in items:
        it["inLibrary"] = it["extId"] in have.get(it["kind"], set())
    return items


def watchlist_sections(stack, wl: Watchlist) -> list[dict]:
    """The list as folder sections: unfiled titles first, then every playlist.

    A title in several playlists appears in each of them; that is what playlists do.
    """
    items = watchlist_view(stack, wl)
    sections = []
    unfiled = [it for it in items if not it["playlists"]]
    if unfiled:
        sections.append({"key": "", "name": "Unfiled", "icon": "bookmark", "titles": unfiled})
    for playlist in wl.playlists():
        sections.append({"key": playlist["key"], "name": playlist["name"], "icon": "folder",
                         "titles": [it for it in items if playlist["key"] in it["playlists"]]})
    return sections


def watchlist_send(stack, wl: Watchlist, keys: list[tuple[str, int]],
                   log: Callable[[str], None] = print) -> dict:
    """Add titles to Radarr/Sonarr and search now; each one sent leaves the list."""
    sent, failed = [], []
    for kind, ext_id in keys:
        try:
            res = titles.add_title(stack, kind, ext_id)
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

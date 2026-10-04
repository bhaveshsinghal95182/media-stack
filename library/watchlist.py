"""Keep a persistent list of movies and shows to remember for later."""

from __future__ import annotations

import json
import os
import threading
import time


KINDS = ("movie", "show")
MAX_TITLE = 200
MAX_OVERVIEW = 300


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


def _item(kind: str, ext_id: int, title: str, year: int | None,
          poster: str | None, overview: str | None, added: float) -> dict:
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
            "poster": poster, "overview": overview, "added": added}


class Watchlist:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self._items: list[dict] = []
        self._writable = True
        if path is None:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if (not isinstance(data, dict) or set(data) != {"version", "items"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["items"], list)):
                raise ValueError("invalid watchlist format")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            try:
                os.replace(path, f"{path}.corrupt-{int(time.time())}")
            except OSError:
                self._writable = False
            return

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
                             entry.get("poster"), entry.get("overview"), float(added))
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            identity = (item["kind"], item["extId"])
            if identity not in seen:
                self._items.append(item)
                seen.add(identity)

    def items(self) -> list[dict]:
        with self.lock:
            return [item.copy() for item in sorted(
                self._items, key=lambda item: -item["added"])]

    def has(self, kind: str, ext_id: int) -> bool:
        try:
            identity = (kind, _ext_id(ext_id))
        except (ValueError, TypeError, OverflowError):
            return False
        with self.lock:
            return any((item["kind"], item["extId"]) == identity
                       for item in self._items)

    def add(self, kind: str, ext_id: int, title: str, year: int | None = None,
            poster: str | None = None, overview: str | None = None,
            now: float | None = None) -> bool:
        item = _item(kind, ext_id, title, year, poster, overview,
                     now if now is not None else time.time())
        with self.lock:
            for index, current in enumerate(self._items):
                if (current["kind"], current["extId"]) == (item["kind"], item["extId"]):
                    updated = current.copy()
                    updated["title"] = item["title"]
                    for field in ("year", "poster", "overview"):
                        if item[field] is not None:
                            updated[field] = item[field]
                    if updated != current:
                        new_items = self._items.copy()
                        new_items[index] = updated
                        self._save_locked(new_items)
                        self._items = new_items
                    return False
            new_items = self._items + [item]
            self._save_locked(new_items)
            self._items = new_items
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
            self._save_locked(new_items)
            self._items = new_items
            return True

    def _save_locked(self, items: list[dict]) -> None:
        if self.path is None:
            return
        if not self._writable:
            raise OSError("cannot save watchlist because its original file was not preserved")
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        temporary_path = self.path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, indent=1, ensure_ascii=False)
        os.replace(temporary_path, self.path)

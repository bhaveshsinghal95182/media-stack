"""Poll Sonarr for aired episodes that still need a file and track search attempts."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone


SCHEDULE_MIN = (5, 20, 45, 90, 180, 360, 720)
REPEAT_MIN = 720
MIN_GAP_MIN = 4
GIVE_UP_DAYS = 7
MAX_PER_CYCLE = 25


def parse_air(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def next_search(entry: dict | None, air_ts: float, now: float) -> float | None:
    if air_ts + GIVE_UP_DAYS * 86400 < now:
        return None
    attempts = entry["attempts"] if entry else 0
    last = entry["last"] if entry else None
    if attempts < len(SCHEDULE_MIN):
        scheduled = air_ts + SCHEDULE_MIN[attempts] * 60
        return max(scheduled, last + MIN_GAP_MIN * 60) if last is not None else scheduled
    return last + REPEAT_MIN * 60


def due_episodes(episodes: list[dict], queued_ids: set[int], state: dict,
                 now: float) -> list[int]:
    due = []
    for ep in episodes:
        episode_id = ep.get("id")
        series = ep.get("series") or {}
        if (episode_id is None or ep.get("hasFile") or ep.get("monitored") is False
                or series.get("monitored") is False or episode_id in queued_ids):
            continue
        air_ts = parse_air(ep.get("airDateUtc"))
        if air_ts is None or air_ts > now or air_ts + GIVE_UP_DAYS * 86400 < now:
            continue
        search_at = next_search(state.get(str(episode_id)), air_ts, now)
        if search_at is not None and search_at <= now:
            due.append((air_ts, episode_id))
    due.sort(key=lambda item: item[0], reverse=True)
    return [episode_id for _, episode_id in due[:MAX_PER_CYCLE]]


def record_search(state: dict, ids: list[int], now: float) -> None:
    for episode_id in ids:
        entry = state.setdefault(str(episode_id), {"attempts": 0, "last": now})
        entry["attempts"] += 1
        entry["last"] = now


def episode_label(ep: dict) -> str:
    title = (ep.get("series") or {}).get("title")
    if not title:
        return f"Episode {ep['id']}"
    return f"{title} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"


class AiringWatcher:
    def __init__(self, make_stack, state_path: str | None, interval: int = 300,
                 log_size: int = 50):
        self.make_stack = make_stack
        self.state_path = state_path
        self.interval = interval
        self.log_size = log_size
        self.lock = threading.Lock()
        self.state: dict = {}
        if state_path is not None:
            try:
                with open(state_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.state = loaded
            except (OSError, ValueError):
                pass
        self.log: list[dict] = []
        self.last_run: float | None = None
        self.last_error: str | None = None

    def _append_log_locked(self, when: float, message: str) -> None:
        self.log.append({"t": when, "msg": message})
        if len(self.log) > self.log_size:
            self.log[:] = self.log[-self.log_size:] if self.log_size > 0 else []

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except Exception as exc:
                    with self.lock:
                        self.last_error = str(exc)
                        self._append_log_locked(time.time(), f"Error: {exc}")
                time.sleep(self.interval)

        threading.Thread(target=loop, daemon=True).start()

    def run_once(self, now: float | None = None) -> list[int]:
        now = now if now is not None else time.time()
        stack = self.make_stack()
        start_iso = datetime.fromtimestamp(now - GIVE_UP_DAYS * 86400,
                                           timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        episodes = stack.sonarr.get("/calendar", start=start_iso, end=end_iso,
                                    includeSeries="true", unmonitored="false")
        queue = stack.sonarr.get("/queue", page=1, pageSize=1000).get("records", [])
        queued_ids = {item["episodeId"] for item in queue if item.get("episodeId")}
        episodes_by_id = {ep["id"]: ep for ep in episodes if "id" in ep}
        episodes_by_state_key = {str(episode_id): ep
                                 for episode_id, ep in episodes_by_id.items()}

        with self.lock:
            for key in list(self.state):
                ep = episodes_by_state_key.get(key)
                if ep is None:
                    del self.state[key]
                elif ep.get("hasFile"):
                    self._append_log_locked(now, f"Got {episode_label(ep)}")
                    del self.state[key]
            state_snapshot = {key: value.copy() for key, value in self.state.items()}

        due = due_episodes(episodes, queued_ids, state_snapshot, now)
        if due:
            stack.sonarr.send("POST", "/command",
                              {"name": "EpisodeSearch", "episodeIds": due})
            with self.lock:
                record_search(self.state, due, now)
                for episode_id in due:
                    ep = episodes_by_id[episode_id]
                    minutes = (now - parse_air(ep["airDateUtc"])) / 60
                    if minutes < 120:
                        aired_part = f"aired {round(minutes)} min ago"
                    elif minutes < 48 * 60:
                        aired_part = f"aired {round(minutes / 60)} h ago"
                    else:
                        aired_part = f"aired {round(minutes / (24 * 60))} d ago"
                    attempt = self.state[str(episode_id)]["attempts"]
                    self._append_log_locked(
                        now, f"Searching {episode_label(ep)} ({aired_part}, try {attempt})")

        if self.state_path is not None:
            try:
                parent = os.path.dirname(os.path.abspath(self.state_path))
                os.makedirs(parent, exist_ok=True)
                with self.lock:
                    saved_state = {key: value.copy() for key, value in self.state.items()}
                temporary_path = self.state_path + ".tmp"
                with open(temporary_path, "w", encoding="utf-8") as f:
                    json.dump(saved_state, f)
                os.replace(temporary_path, self.state_path)
            except OSError as exc:
                with self.lock:
                    self._append_log_locked(now, f"Error saving state: {exc}")

        with self.lock:
            self.last_run = now
            self.last_error = None
        return due

    def status(self) -> dict:
        with self.lock:
            return {"lastRun": self.last_run, "lastError": self.last_error,
                    "interval": self.interval,
                    "log": [entry.copy() for entry in reversed(self.log)]}

    def episode_info(self, episode_id: int, air_ts: float | None,
                     now: float | None = None) -> dict | None:
        if air_ts is None:
            return None
        now = now if now is not None else time.time()
        with self.lock:
            entry = self.state.get(str(episode_id))
            return {"attempts": entry["attempts"] if entry else 0,
                    "last": entry["last"] if entry else None,
                    "next": next_search(entry, air_ts, now)}

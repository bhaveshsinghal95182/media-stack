"""The services of the stack, and what differs between movies (Radarr) and shows (Sonarr)."""

from __future__ import annotations

from medialib.clients import Arr, Jellyfin, QBit, http_json
from medialib.errors import LibraryError
from medialib.settings import Settings
from medialib.state import load_json
from medialib.volume import Volume

KINDS = ("movie", "show")


class Stack:
    """Clients for every service. Not thread-safe: give each thread its own."""

    def __init__(self, settings: Settings | None = None):
        s = settings or Settings.load()
        self.settings = s
        self.radarr = Arr(s.radarr_url, s.radarr_key, "Radarr")
        self.sonarr = Arr(s.sonarr_url, s.sonarr_key, "Sonarr")
        self.qb = QBit(s.qbit_url, s.qbit_user, s.qbit_pass)
        self.volume = Volume(s.local_data)
        self.seerr_settings: dict = load_json(s.seerr_settings_path, {})
        self.seerr_key = self.seerr_settings.get("main", {}).get("apiKey")
        self._jellyfin: Jellyfin | None = None

    @property
    def jellyfin(self) -> Jellyfin:
        if self._jellyfin is None:
            key = self.settings.jellyfin_key or self.seerr_settings.get("jellyfin", {}).get("apiKey")
            if not key:
                raise LibraryError("no Jellyfin API key (set JELLYFIN_API_KEY in .env)")
            self._jellyfin = Jellyfin(self.settings.jellyfin_url, key)
        return self._jellyfin

    def seerr(self, method: str, path: str):
        return http_json(f"{self.settings.seerr_url}/api/v1{path}", method,
                         headers={"X-Api-Key": self.seerr_key})

    def kind(self, kind: str) -> Kind:
        if kind not in KINDS:
            raise LibraryError(f"unknown kind {kind!r}")
        return Kind(self, kind)


class Kind:
    """Movie (Radarr) or show (Sonarr) specifics."""

    def __init__(self, stack: Stack, kind: str):
        self.stack, self.kind = stack, kind
        self.is_movie = kind == "movie"
        if self.is_movie:
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
        if self.is_movie:
            return item.get("sizeOnDisk") or 0
        return (item.get("statistics") or {}).get("sizeOnDisk") or 0

    def history(self, item_id: int) -> list[dict]:
        if self.is_movie:
            return self.arr.get("/history/movie", movieId=item_id)
        return self.arr.get("/history/series", seriesId=item_id)

    def queue_for(self, item_id: int) -> list[dict]:
        return [r for r in self.arr.queue() if r.get(self.queue_key) == item_id]

    def hashes(self, item: dict, queue: list[dict] | None = None) -> set[str]:
        """Torrent hashes that belong to this title (grab history + queue)."""
        out = {h["downloadId"].lower() for h in self.history(item["id"])
               if h.get("eventType") == "grabbed" and h.get("downloadId")}
        queue = self.arr.queue() if queue is None else queue
        out |= {r["downloadId"].lower() for r in queue
                if r.get(self.queue_key) == item["id"] and r.get("downloadId")}
        return out

    def lookup(self, query: str) -> list[dict]:
        if self.is_movie:
            if query.lower().startswith("tmdb:"):
                return [self.arr.get("/movie/lookup/tmdb", tmdbId=int(query[5:]))]
            return self.arr.get("/movie/lookup", term=query)
        return self.arr.get("/series/lookup", term=query)

    def lookup_id(self, ext_id: int) -> dict:
        hit = [r for r in self.lookup(f"{self.id_name}:{ext_id}") if r.get(self.id_key) == ext_id]
        if not hit:
            raise LibraryError(f"{self.id_name}:{ext_id} not found")
        return hit[0]

    def in_library(self, ext_id: int) -> dict | None:
        return next((i for i in self.items() if i.get(self.id_key) == ext_id), None)

    def search(self, item_id: int) -> None:
        """Ask the Arr to search every indexer for this title now."""
        if self.is_movie:
            self.arr.command({"name": "MoviesSearch", "movieIds": [item_id]})
        else:
            self.arr.command({"name": "SeriesSearch", "seriesId": item_id})

    def profile_and_root(self, anime: bool = False) -> tuple[int, str]:
        # Same defaults Seerr uses for requests, so both paths behave alike;
        # Sonarr has separate ones for anime.
        seerr = self.stack.seerr_settings.get("radarr" if self.is_movie else "sonarr") or []
        default = next((s for s in seerr if s.get("isDefault")), seerr[0] if seerr else {})
        prefix = "activeAnime" if anime and not self.is_movie else "active"
        profile_id = default.get(f"{prefix}ProfileId") or default.get("activeProfileId")
        profile_name = default.get(f"{prefix}ProfileName") or default.get("activeProfileName")
        profiles = self.arr.get("/qualityprofile")
        prof = next((p for p in profiles if p["id"] == profile_id), None) \
            or next((p for p in profiles if p["name"] == profile_name), None) \
            or profiles[0]
        root = default.get(f"{prefix}Directory") or default.get("activeDirectory") \
            or self.arr.get("/rootfolder")[0]["path"]
        return prof["id"], root

    def add_body(self, lookup_item: dict, monitored: bool) -> dict:
        """The POST body that adds a looked-up title, without add options."""
        anime = not self.is_movie and "anime" in [g.lower() for g in lookup_item.get("genres", [])]
        profile_id, root = self.profile_and_root(anime)
        body = dict(lookup_item, qualityProfileId=profile_id, rootFolderPath=root,
                    monitored=monitored)
        if self.is_movie:
            body["minimumAvailability"] = "released"
        else:
            body["seasonFolder"] = True
            if anime:
                body["seriesType"] = "anime"
        return body

    def add(self, lookup_item: dict, search: bool) -> dict:
        """Add a looked-up title. search=False adds it unmonitored (for imports)."""
        body = self.add_body(lookup_item, monitored=search)
        if self.is_movie:
            body["addOptions"] = {"searchForMovie": search}
        else:
            body["addOptions"] = {"monitor": "all" if search else "none",
                                  "searchForMissingEpisodes": search}
        return self.arr.send("POST", "/" + self.res, body)


def image(item: dict, cover_type: str) -> str | None:
    """An Arr item's image URL by type: poster, fanart, banner…"""
    for img in item.get("images") or []:
        if img.get("coverType") == cover_type:
            return img.get("remoteUrl") or img.get("url")
    return None


def poster(item: dict) -> str | None:
    return image(item, "poster")


def summarize_lookup(k: Kind, r: dict) -> dict:
    """A search result as the pages show it."""
    return {"kind": k.kind, "extId": r.get(k.id_key), "title": r["title"], "year": r.get("year"),
            "overview": (r.get("overview") or "")[:300], "poster": poster(r),
            "label": k.label(r), "inLibrary": bool(r.get("id"))}

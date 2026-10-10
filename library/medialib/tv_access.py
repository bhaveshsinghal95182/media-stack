"""Hide series from one Jellyfin account (the TV), using a tag that account blocks.

Jellyfin hides every item carrying a tag in a user's BlockedTags, children included.
The tag goes on the series and its Tags field is locked, so a metadata refresh keeps it.
"""

from __future__ import annotations

import os

from medialib.clients import Jellyfin
from medialib.errors import LibraryError
from medialib.stack import Stack

TAG = "hidden-from-tv"
# Everything an item update must send back, or Jellyfin clears it.
ITEM_FIELDS = "Tags,LockedFields,ProviderIds,Genres,Studios,People,Taglines,Overview,OriginalTitle"


def _series(jf: Jellyfin) -> list[dict]:
    return jf.get("/Items", Recursive="true", IncludeItemTypes="Series",
                  Fields="ProviderIds,Tags,Path").get("Items", [])


def _match(series: list[dict], show: dict) -> dict | None:
    """The Jellyfin series for a Sonarr show: by TVDB id, else by folder name."""
    tvdb = str(show.get("tvdbId") or "")
    if tvdb:
        hit = next((s for s in series if (s.get("ProviderIds") or {}).get("Tvdb") == tvdb), None)
        if hit:
            return hit
    folder = os.path.basename((show.get("path") or "").rstrip("/"))
    return next((s for s in series
                 if folder and os.path.basename(s.get("Path") or "") == folder), None)


def _with_tag(tags: list[str], on: bool) -> list[str]:
    return [t for t in tags if t != TAG] + ([TAG] if on else [])


def tv_user(stack: Stack) -> dict:
    name = stack.settings.tv_user.lower()
    user = next((u for u in stack.jellyfin.get("/Users") if u["Name"].lower() == name), None)
    if not user:
        raise LibraryError(f"no Jellyfin user named '{name}' (set TV_USER)")
    return user


def tv_hidden_ids(stack: Stack, shows: list[dict]) -> set[int]:
    """Sonarr ids of the shows hidden from the TV (none when Jellyfin can't be reached)."""
    try:
        series = _series(stack.jellyfin)
    except LibraryError:
        return set()
    return {s["id"] for s in shows
            if (m := _match(series, s)) and TAG in (m.get("Tags") or [])}


def set_tv_hidden(stack: Stack, show_id: int, hidden: bool) -> dict:
    """Hide a show from the TV account, or show it again."""
    jf = stack.jellyfin
    show = stack.sonarr.get(f"/series/{show_id}")
    match = _match(_series(jf), show)
    if not match:
        raise LibraryError(f"{show['title']} isn't in Jellyfin yet; wait for its library scan")
    user = tv_user(stack)
    _block_tag(jf, user, hidden)
    _tag_item(jf, match["Id"], hidden)
    where = "hidden from" if hidden else "visible to"
    return {"hidden": hidden, "message": f"{show['title']} is now {where} {user['Name']}."}


def _block_tag(jf: Jellyfin, user: dict, on: bool) -> None:
    policy = user["Policy"]
    old = policy.get("BlockedTags") or []
    # The tag stays blocked once set: un-hiding removes it from the item instead.
    new = _with_tag(old, True) if on else old
    if new != old:
        jf.send("POST", f"/Users/{user['Id']}/Policy", body={**policy, "BlockedTags": new})


def _tag_item(jf: Jellyfin, item_id: str, on: bool) -> None:
    items = jf.get("/Items", Ids=item_id, Fields=ITEM_FIELDS).get("Items", [])
    if not items:
        raise LibraryError("the series vanished from Jellyfin; try again")
    item = items[0]
    locked = list(item.get("LockedFields") or [])
    if on and "Tags" not in locked:
        locked.append("Tags")
    item.update(Tags=_with_tag(item.get("Tags") or [], on), LockedFields=locked)
    jf.send("POST", f"/Items/{item_id}", body=item)

# library

The web page that sits next to the media stack. It does the jobs the *arr
interfaces don't: browse what is actually on disk, add something, follow an
airing anime, import what finished downloading, and clean up what didn't.

FastAPI + Jinja + htmx, one container, password-protected on the LAN.

## Tabs

- **Library** — movies and shows from Radarr and Sonarr, by season, with the
  files on disk.
- **Add** — search, pick a release, send it to the right arr.
- **Watchlist** — save a title now, download it later.
- **Following** — airing anime, new episodes queued as they appear.
- **Import** — completed downloads into the library, with a rescue path for
  imports that fail halfway.
- **Cleanup** — leftover torrents, orphaned files, watched media the janitor can
  remove.
- **Seasonal** — the current season, for what to watch next.
- **Playlists** — ordered lists that survive a re-import.

Per show there is also **TV access**: hide a title from the TV's Jellyfin user
without touching the rest of the library.

## Running it

Expects the rest of the stack from
[media-stack](https://github.com/bhaveshsinghal95182/media-stack): Radarr,
Sonarr, qBittorrent and Jellyfin on the same Docker network.

```
docker build -t library .
docker run --env-file .env -p 8099:8099 library
```

Environment:

| variable | what it is |
|---|---|
| `LIBRARY_PORT` | port the page listens on, default 8099 |
| `LIBRARY_USERNAME` | accepted user, or empty to accept any |
| `LIBRARY_PASSWORD` | page password |
| `IMPORT_ROOT` | where finished downloads land before import |
| `LIBRARY_WATCHER` | filesystem watcher, `0` to disable |
| `LIBRARY_JANITOR` | watched-media janitor, `0` to disable |
| `LIBRARY_ROTATE` | log rotation for the page's own logs |
| `LIBRARY_LOCAL_DATA` | state directory |

API keys for Radarr, Sonarr, qBittorrent and Jellyfin come from the same `.env`.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests
```

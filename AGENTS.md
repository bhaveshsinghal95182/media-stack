# AGENTS.md: working on media-stack

A self-hosted media stack on one Arch/Omarchy laptop, run with Docker Compose.
Off-the-shelf apps (Radarr, Sonarr, Prowlarr, qBittorrent, Jellyfin, Seerr,
FlareSolverr) plus two custom Python services written for this repo:

- **subgate**: rejects downloads without English subtitles or English audio.
- **library**: a LAN web page and CLI for managing the library, plus an anime
  airing watcher.

The by-hand install guide for humans is `SETUP.md`. Keep it accurate when you
change settings or behaviour. `MEDIA_STACK_AGENT_SETUP.md` is the **historical**
spec the stack was first built from. Parts of it are superseded (for example,
Whisparr is not deployed), so treat the code and live config as the truth.

---

## Safety rules (read first)

1. **Secrets live only in `.env`** (chmod 600, gitignored). Never print, log,
   echo or commit its values. Read them in a subshell
   (`set -a; . ./.env; set +a`) and pass them along without displaying them.
2. **qBittorrent bans an IP for 1 hour after repeated failed logins.** Never
   retry a qB login in a loop. qB 5.x answers a *successful* login with HTTP
   204 and an empty body, not `Ok.`, so don't mistake that for a failure and
   retry. The easiest safe way to talk to qB is through the library container:
   `docker exec media-library python3 -c "from medialib import Stack; print(Stack().qb.call('app/preferences'))"`.
3. **The library page locks a client out for 10 minutes after 10 bad logins.**
   Read `LIBRARY_USERNAME`/`LIBRARY_PASSWORD` from `.env`. Don't guess them.
4. **Never delete the user's media, torrents or "orphans" without explicit
   approval.** Show what would be deleted first (`delete_plan`,
   `./media.sh orphans` without `--delete`).
5. **The user's import folder (`IMPORT_DIR`, usually `~/Downloads`) contains
   sensitive personal files** (ID documents, API-key CSVs). The library must
   only ever list folders, video files and subtitle files there, must never
   serve file contents, and must hide symlinks that resolve outside it
   (`library/medialib/importer.py` `ImportFolder`). Keep it that way.
6. **No commits without the user seeing the file list first.** Never commit
   `.env`, `config/`, `backup/` or `__pycache__/`.
7. **No global `docker system prune` / `volume prune`.** The scratch volume and
   other projects' data live on the same daemon.
8. Before editing a tracked file, copy it to `backup/<name>.before-<what>.<epoch>`.
   That's the repo's convention, and it has saved work before.
9. If you delegate to a sub-agent or another tool, diff its changes afterwards.
   One once ran `git restore` and silently dropped unrelated uncommitted edits.

---

## Architecture

```
            LAN (phone/TV)                     localhost only
   ┌──────────┬──────────┬──────────┐   ┌─────────┬─────────┬──────────┬─────────────┐
   │ Jellyfin │  Seerr   │ library  │   │ Radarr  │ Sonarr  │ Prowlarr │ qBittorrent │
   │  :8096   │  :5055   │  :8099   │   │  :7878  │  :8989  │  :9696   │    :8080    │
   └────┬─────┴────┬─────┴────┬─────┘   └────┬────┴────┬────┴────┬─────┴──────┬──────┘
        │ (+ Intro Skipper plugin)           │ Bazarr :6767 (subs, localhost)  │
        │                                    │ Recyclarr (no UI) → profiles/CFs│
        │ reads    │ requests │ API          │ search via Prowlarr│            │
        │ /data/   └──────────┴──────────────┤  (Prowlarr → FlareSolverr      │
        │ media                              │   for Cloudflare indexers)     │
        │                                    └──── grab → qBittorrent ────────┤
        │                    subgate polls qB, probes the file header,        │
        │                    rejects via Arr queue DELETE (blocklist) ────────┘
        └──── Arr imports completed downloads as HARDLINKS into /data/media
```

The flow for one title:

1. A request comes from Seerr, the library page, or `./media.sh add`, or
   Sonarr's RSS feed or the airing watcher finds a new episode.
2. Radarr/Sonarr search through Prowlarr's synced indexers and grab a release
   into qB, using category `radarr` or `tv-sonarr`.
3. subgate checks the release while it downloads. If the release fails, subgate
   removes it from the Arr queue with `blocklist=true`, and the Arr's "failed
   download → redownload" setting picks the next release.
4. When the download completes, the Arr hardlinks it into
   `/data/media/movies` or `/data/media/tv`. Jellyfin's real-time monitoring
   picks it up; Intro Skipper then fingerprints the season for intros/credits.
5. Bazarr (Sonarr/Radarr SignalR) sees the import and downloads an English
   `.srt` if the file has no English subtitle track.

**Storage.** Everything under `/data` is the named Docker volume
`media-stack-scratch`, mounted at `/data` in every container. Hardlinks only
work because downloads and the library share that one filesystem.

The volume is **disposable**: `./media.sh destroy` deletes it.

App config lives in `./config/<app>/` as bind mounts, and it isn't disposable.
`./media.sh reset` wipes it after a typed confirmation.

**Network.** Compose network `media-stack-network`. Containers address each
other by service name (`radarr:7878`, `qbittorrent:8080`, ...). Only Jellyfin,
Seerr and library publish on all interfaces, plus qB's torrent port 6881. All other web UIs bind to `127.0.0.1`.

---

## File map

```
compose.yaml            all services; subgate + library are built locally
media.sh                lifecycle + `list|add|delete|orphans|import` → library/cli.py
.env / .env.example     secrets and ids (PUID, PGID, TZ, QBIT_*, *_API_KEY, LIBRARY_*, IMPORT_DIR)
SETUP.md                human, no-AI install guide; every UI setting by hand
recyclarr/media.yml     Recyclarr config (TRaSH profiles + our departures), mounted ro at /config/configs
config/<app>/           app state (gitignored). config/library/{watcher,watchlist,janitor,rolling,stalls}.json, config/subgate/subgate.json
backup/                 pre-edit file copies (gitignored)

subgate/
  subgate.py            the gate (stdlib + ffprobe); README.md documents the algorithm
  test_subgate.py       unit tests (pure functions: classification, stream decisions)
gpuswitch/
  gpuswitch.py          sets Jellyfin's encoding config by power source (AC → JELLYFIN_AC_HWACCEL, battery → qsv);
                        on AC loss stops transcodes on the AC hardware. python:3.13-alpine, script bind-mounted, no build
  setup-nvidia-runtime.sh  root one-off: nvidia-container-toolkit + docker runtime (needed before nvenc)
library/
  medialib/             domain logic, STDLIB ONLY (cli.py imports it on the host)
    settings.py stack.py  Settings from env/.env; Stack = the clients; Kind = movie/show specifics
    clients.py volume.py  http_json, Arr, QBit, Jellyfin; Volume (local /data or docker exec)
    titles.py             overview, add_title, delete_plan/delete_execute
    details.py            the details dialog: Arr metadata (+ files/seasons for library titles)
    seasons.py            season_rows (on disk / missing / downloading / to air / removed), get_season.
                          "Removed" = Sonarr history episodeFileDeleted (not Upgrade): watched and
                          cleaned up, so get_season keeps it unmonitored. Monitoring a season in
                          Sonarr monitors all its episodes, hence the re-unmonitor afterwards.
    releases.py           downloads, release_info/search/grab/drop
    following.py          follow/unfollow/following, refresh_series, search_episodes
    seasonal.py           AniList + Fribb's TVDB/TMDB mapping
    importer.py           ImportFolder (the ~/Downloads guard), import_candidates/plan/execute
    orphans.py            claimed_hashes, orphan_list/orphans_delete
    tv_access.py          Hide on TV (Jellyfin BlockedTags)
    rolling.py            rolling window (One Piece-style shows): RollingStore, apply/status,
                          start_window/stop_window, preview (Sonarr, else skyhook), RollingWindow service
    rescue.py             ImportRescuer (watcher's before_cycle)
    stalls.py             StallRotator: drops + blocklists downloads with no progress for STALL_MINUTES
    watcher.py watchlist.py janitor.py jobs.py   background services, JobRunner
    fmt.py files.py state.py errors.py           small helpers
  web/                  FastAPI + Jinja + htmx; a thin layer over medialib
    app.py              create_app(): auth dependency, SecurityMiddleware, error handlers
    services.py         Services: watchlist, JobRunner, op_lock, watcher, janitor, per-thread Stack
    security.py         Basic auth + lockout; HX-Request required on POST
    render.py           render()/notify(): templates, toasts and events via HX-Trigger
    routes/*.py         one router per tab, plus jobs.py
    templates/          base.html, pages/, partials/ (htmx swaps), dialogs/, macros/ui.html
    styles/app.css      Tailwind v4 input (component classes); built into static/app.css by Docker
    static/             app.js (dialog, toasts, filters, multi-select), vendored htmx.min.js
  cli.py                argparse front-end used by media.sh (runs on the HOST)
  tests/                unittest; test_web.py needs requirements-dev.txt and skips without it
```

`medialib` runs in two places:

- **In the library container.** `LIBRARY_LOCAL_DATA=1`; `/data` is mounted, and
  the service URLs come from compose env.
- **On the host via `cli.py`.** It reads `.env` itself and uses `localhost`
  ports. It reaches the volume with `docker exec`/`docker cp` into
  `media-radarr`/`media-sonarr` (the `Volume` class).

Keep both paths working.

---

## subgate

- Polls qB every 15 s (`POLL_SECONDS`) for torrents in the `radarr`/`tv-sonarr`
  categories. It tracks progress with qB tags: `subgate-checking`, `-ok`,
  `-rejected`, `-skipped` and `-would-reject` (the last one appears when
  `DRY_RUN=true`).
- **Pass:**
  - An English or language-less subtitle file in the torrent's file list.
  - Otherwise, a full English subtitle stream or English audio in the
    `ffprobe` of the first 4–16 MiB. While probing, subgate downloads that
    head sequentially, with first/last piece priority.
- **Forced subtitles don't count.** A track is forced if its title contains
  forced/signs/songs, or if `disposition.forced=1` and the `NUMBER_OF_FRAMES`
  tag is missing or below 300 (`FULL_SUB_MIN_EVENTS`).
  - Some release groups flag their *full* English track as forced. The event
    count rule exists so those aren't rejected.
  - ffprobe is asked for all `stream_tags` because of this.
- Untagged (`und`) audio counts as English only when the Arr says the title's
  original language is English.
- **Reject:** `DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true`.
- **Queue lookups pass `includeUnknownSeriesItems=true` /
  `includeUnknownMovieItems=true`.** Downloads Sonarr can't parse ("matched to
  series by ID") are hidden from `/queue` otherwise; subgate then never found
  them and only stopped the torrent.
- State is kept in `/state/subgate.json`.
- Settings are environment variables with defaults; see the `Config` class.
- Because external `.srt` files can pass a release, Radarr/Sonarr have **Import
  Extra Files on** (`srt,ass,ssa,vtt,sub,idx`). Don't turn it off.

## library: core concepts and gotchas

- **Auth:** HTTP Basic. The password must match `LIBRARY_PASSWORD`. If
  `LIBRARY_USERNAME` is empty, any username is accepted.
- **CSRF:** every non-GET request needs `HX-Request: true` (htmx sends it;
  a cross-site form can't). `SecurityMiddleware` returns 403 otherwise.
- **htmx conventions:** routes return HTML fragments. Toasts and refreshes go
  in the `HX-Trigger` header (`render(..., toast=, events=)` / `notify()`);
  sections reload on events like `library-changed from:body`. Errors: an action
  gets a 4xx plus an error toast; an htmx GET gets `partials/error.html` with
  200, so it swaps in; a full page gets `pages/error.html`. Anything swapped
  into `#dialog-body` opens the dialog. Tailwind only sees literal class names
  in templates, so never build classes like `bg-{{ tone }}`.
  An `hx-trigger` filter must evaluate to exactly `true`: htmx 2 drops the
  event on any other value, even a truthy one. Write `change[target.name!='']`,
  not `change[target.name]`. The second form broke the Seasonal tab's
  format and sort controls.
  htmx also **inherits** `hx-include`/`hx-target` down to child elements: a
  container that reloads itself with them needs `hx-disinherit="*"`, or its
  buttons send its fields too. That made the Seasonal tab's Follow send AniList's
  `season=FALL` as the TVDB season (422) on 2026-10-16.
- **Jobs:** long POSTs (add, delete, import, follow, send, orphan delete) run as
  background jobs (`medialib.jobs.JobRunner`). `partials/job.html` polls
  `/jobs/<id>` every second and fires the job's refresh event when done.
  Grab, drop, unfollow and the janitor take `Services.op_lock` directly.
- **Hide on TV** (`tv_access.py`): the tag `hidden-from-tv` goes on the
  Jellyfin series (and is locked against metadata refresh); the `TV_USER`
  account (default `tv`) gets it in `BlockedTags`, so the account can neither
  see nor play the series.
- **Delete** (`delete_plan` → `delete_execute`). Because imports are hardlinks,
  freeing disk space takes all of these:
  - remove the Arr entry with its files;
  - delete the qB torrent **with its data**;
  - delete Seerr's request and media record. Seerr keeps its own record and
    otherwise keeps showing the title as "available".

  `delete_execute` then verifies the files are gone. Seerr's API key comes from
  `config/seerr/settings.json`, which is mounted read-only at
  `/seerr/settings.json` (the whole folder is mounted, not the file).
- **Releases** (`release_info`, `release_search`, `release_grab`,
  `release_drop`):
  - **Search.** Interactive search via `GET /api/v3/release?movieId=`, or
    `?seriesId=&seasonNumber=` for a show. Searches can take minutes
    (`RELEASE_SEARCH_TIMEOUT=240`). Results are filtered to the title through
    `mappedMovieId`, or `mappedSeriesId` + `mappedSeasonNumber`.
  - **Noise rejections are dropped.** Reasons like "in queue", "existing file"
    and "meets cutoff" are removed, so they don't bury usable releases.
  - **Grab.** `POST /api/v3/release` first, then remove the replaced downloads
    with `blocklist=true&skipRedownload=true`.
  - **Drop.** `blocklist=true&skipRedownload=false`, so the Arr picks the next
    release itself.
  - **`downloads()`** joins Arr queue records with qB data. It reports
    `num_seeds` (connected seeds) and `num_complete` (swarm), plus speed, ETA
    and the subgate tag. Indexer seed counts are often stale.
- **Follow** (`follow`, `unfollow`, `following`, `follow_candidates`):
  - **`follow`:**
    - Adds the series if missing: `monitored=true`,
      `monitorNewItems="all"`, `addOptions.monitor="none"`, and
      `seriesType="anime"` when the genres include Anime.
    - Loads episodes with `refresh_series`. **Never send `RefreshSeries` right
      after `POST /series`:** Sonarr starts its own refresh on add, the two race,
      and every episode is inserted twice. Then no release name matches and
      nothing imports by itself. `refresh_series(new=True)` only waits for
      Sonarr's refresh. For existing series it waits out any running refresh first.
    - Monitors episodes by mode:
      - `new`: only unaired episodes.
      - `season`: one season, plus everything new.
      - `all`: everything.
    - Saves with `PUT /episode/monitor` and `PUT /series/{id}`.
    - Searches with `SeasonSearch` or `SeriesSearch`.
  - **`unfollow`** sets the series to unmonitored and keeps the files.
  - **`following`** reads Sonarr's calendar from 2 days back to 14 days ahead
    and combines it with the watcher's per-episode info.
- **Rolling window** (`rolling.py`; for very long shows whose TVDB seasons mean
  nothing, e.g. One Piece, series 27, started at 1157 on 2026-10-16):
  - The user gives an absolute episode number (`absoluteEpisodeNumber`, else the
    position in (season, episode) order, specials excluded) and `ahead` (default
    12, max 50). State: `/state/rolling.json` (`ROLLING_STATE`).
  - **Watch position** = just after the last *done* episode from the start on.
    Done = Sonarr history `episodeFileDeleted` (not Upgrade) or the file path is
    in any Jellyfin user's played list. The window is the next `ahead` episodes
    that aren't done; skipped episodes behind the position don't count.
  - **The window owns monitoring.** `apply` sets the series monitored (Sonarr
    only retries after a subgate reject on a monitored series), every season
    unmonitored and `monitorNewItems="none"`: a monitored season monitors all its
    episodes. Flipping a season flag makes Sonarr reset its episodes, so `apply`
    re-reads the episodes after that PUT. Then it monitors the window, unmonitors
    anything else without a file that isn't queued, and `EpisodeSearch`es aired
    missing window episodes at most every 6 h each.
  - `RollingWindow` (started by `Services.start_background`) applies every
    window every 5 min under `op_lock`. It drops a window whose series is
    deleted or unmonitored (unfollowed).
  - The janitor's grace for these shows' episodes is `JANITOR_ROLLING_GRACE_DAYS`
    (1, the user's choice) instead of 3; the window moves on as soon as Jellyfin
    marks an episode played, before the file is removed.
  - `preview` reads Sonarr's episodes, or for a show not added yet Sonarr's own
    metadata source `skyhook.sonarr.tv/v1/tvdb/shows/en/<tvdb>` (cached 1 h).
- **Seasonal** (`seasonal_anime`):
  - **AniList data.** AniList GraphQL `Page(media: season, year, ANIME)`, up to
    8 pages of 50. It **requires a User-Agent header**; Python's default gets
    403.
  - **Mapping to TVDB/TMDB.** Uses Fribb's `anime-list-mini.json`
    (~6 MB, cached 24 h). Keys:
    - `anilist_id`;
    - `tvdb_id` (int);
    - `themoviedb_id`, which is a **dict**: `{"tv": id}` or
      `{"movie": [ids]}`;
    - `season: {"tvdb": N, "tmdb": N}`.
  - **Unmapped entries** are marked `unsure`. The UI defaults them to "new
    episodes only" with a warning.
  - **Caching.** AniList responses are cached for 30 minutes. `_cached` serves
    stale data when a fetch fails.
- **Watcher** (`watcher.py`, started by `Services.start_background()` unless `LIBRARY_WATCHER=0`):
  - Every 300 s it reads Sonarr's calendar for the last 7 days and the queue.
  - It sends `EpisodeSearch` for monitored, aired episodes that have no file
    and aren't queued.
  - Schedule: 5, 20, 45, 90, 180, 360 and 720 minutes after air, then every
    12 h. It gives up after 7 days, leaves at least 4 minutes between searches
    of the same episode, and searches at most 25 episodes per cycle.
  - State is saved atomically to `/state/watcher.json`.
  - Queue records with no `episodeId` (unknown items) are mapped to episodes
    through `/history?downloadId=` grabbed events, so they count as queued.
  - Before each cycle it runs **`rescue.ImportRescuer`**:
    - Targets downloads that are `importBlocked`, complete and passed by subgate
      (`subgate-ok`/`-skipped`).
    - Takes the episodes from Sonarr's grab history and imports the best copy
      (resolution, then custom format score) with `ManualImport`
      (`importMode=copy`, i.e. a hardlink).
    - One import per season/episode number per pass. It skips episodes that
      already have a file and retries a download at most hourly.
    - A pack whose files don't parse is logged for a manual import.
  - Sonarr's own RSS sync runs at 10 min, the minimum Sonarr allows.
- **Watchlist** (`watchlist.py`: the store, `watchlist_view`, `watchlist_sections` and
  `watchlist_send`):
  - Titles saved for later live only in `/state/watchlist.json`
    (`config/library/`), keyed by (kind, extId). extId is the TMDB id for
    movies and the TVDB id for shows.
  - **Playlists** (state v2): a title can be in any number of playlists.
    `playlists` maps a slug key to `{name, created}`; each item carries
    `playlists: [key, ...]`. Keys stay on rename, so the name is the only thing
    that changes. v1 files (no playlists) still load; the next write is v2.
    Deleting a playlist unfiles its titles, it never removes them. Names are
    unique case-insensitively; `create_playlist` reuses an existing name.
  - **Saving.** "Save" opens `partials/save_dialog.html`
    (`GET /watchlist/save`) with checkboxes for the playlists and a "new
    playlist" field. Nothing ticked means just the watchlist. The POST to
    `/watchlist/items` replaces the membership (the dialog pre-checks it), and
    its response closes the dialog with the `close-dialog` HX-Trigger event
    (app.js listens) and OOB-swaps the button that opened it
    (`id="save-<kind>-<extId>"`). Keep that id scheme when adding another save
    entry point.
  - **The page** shows folder sections: unfiled titles first, then every
    playlist (a title in two playlists appears in both). Empty playlists are
    shown so they can be renamed/deleted from the section header. The header
    actions are icon-only (title + aria-label): pencil renaming, trash
    deleting with `hx-confirm`. Folded sections are remembered in
    `localStorage` (`watchlist-folded`).
  - Filters (`data-filter-section`) hide a whole section while a text/kind
    filter is active if none of its rows match. Selections count unique
    `kind:extId` values, and `/watchlist/send` and `/watchlist/assign` de-dupe,
    because a title can be checked in more than one section.
  - Radarr/Sonarr don't know about them until they're sent. A movie is sent
    with `add_title` (adds and searches). A show never is: its Download
    button opens the follow dialog (`origin=watchlist`) to pick a season, new
    episodes, a rolling window or everything, and leaves the list on success.
    "Download selected" skips shows. The Add tab does the same for shows
    (`origin=add`); only the CLI's `./media.sh add` still gets every episode.
  - Each title that's sent successfully is removed from the list. Failed ones
    stay.
  - Add-tab search results and Seasonal mark titles with `onWatchlist`.
  - The file is on the host disk on purpose, so `destroy` and Docker prunes
    can't touch it.
- **Janitor** (`janitor.py`; it **deletes media automatically**, and the user asked
  for that: either user's watch counts, 3-day grace, episodes and movies, no
  preview mode):
  - **When it runs.** The disk guard runs every 120 s. Cleanup runs every
    30 min, and every tick while space is low. It only runs in the container
    (`LIBRARY_LOCAL_DATA=1`) because it needs `/data` locally for
    `statvfs`/`stat`. `LIBRARY_JANITOR=0` disables it.
  - **Disk guard.** `os.statvfs("/data")` reports the host root filesystem,
    shared with the OS. Below `JANITOR_PAUSE_BELOW_GB` (20) it stops every
    incomplete torrent with qB 5's `torrents/stop` and records the hashes.
    Above `JANITOR_RESUME_ABOVE_GB` (25) it starts **only those** with
    `torrents/start`. Torrents that subgate stopped stay stopped.
  - **Watched state.** Jellyfin `/Items?userId=..&Filters=IsPlayed` for every
    user. `watchedAt` is the latest `LastPlayedDate` across users. Items marked
    played with no date count from when the janitor first saw them (`seen` in
    the state file).
  - **Favourites.** An item is kept if any user favourited it, its season or
    its series. The page's Keep button sets a favourite via
    `POST /UserFavoriteItems/{id}?userId=`.
  - **Episodes.** The order matters:
    1. `PUT /episode/monitor` with `monitored=false`. This comes first, so
       neither Sonarr nor the airing watcher re-fetches the episode.
    2. `DELETE /episodefile/{id}`.
    3. Record the qB torrents holding the file's **inode**.
    4. A recorded torrent is deleted with its data once all its files have
       `st_nlink == 1`, meaning nothing is linked into the library any more.
       Season packs therefore wait for the whole pack.
  - **Movies.** `delete_plan` + `delete_execute`.
  - **Limits.** Paths must be under `/data/media/`; at most 50 removals per
    pass. Holds `Services.op_lock` while deleting.
  - **Jellyfin API.** Jellyfin is 12.x. Use the header
    `Authorization: MediaBrowser Token="<key>"`; `X-Emby-Token` returns 401.
    The key comes from `JELLYFIN_API_KEY`, falling back to the one Seerr keeps
    in `config/seerr/settings.json`. Item ids change when Jellyfin rescans, so
    don't cache them.
- **Stalled downloads** (`stalls.py` `StallRotator`, started by
  `Services.start_background()` unless `LIBRARY_ROTATE=0`; shown on Cleanup):
  - Every 120 s it reads qB. An incomplete torrent in an active state
    (`downloading`, `stalledDL`, `metaDL`, forced) whose `completed` bytes
    grew less than `STALL_MIN_PROGRESS_MB` (10) in `STALL_MINUTES` (60) is
    removed from its Arr queue with `blocklist=true&skipRedownload=false`
    (like `release_drop`), under `op_lock`, at most 10 per pass.
  - Queued, stopped (disk guard, subgate) and complete torrents aren't
    tracked; their clock restarts when they become active. A gap of more than
    10 min between checks (laptop asleep, library down) restarts every clock.
  - A stalled torrent that isn't in the Arr queue is logged once and left
    alone. State: `/state/stalls.json` (`STALL_STATE`).
  - **Fallback** (the user's call: something beats nothing). A rotation
    records what the torrent was for (movieId, or the episodes from the queue
    / grab history). `FALLBACK_AFTER` (10 min) later, if that's still wanted,
    monitored and nothing is queued for it, it runs one interactive search
    (`episodeId=` for one episode, else per season) and grabs via
    `release_grab` the best release from `pick_fallback`: approved first, else
    one whose rejections are all "soft" (`is_soft`: quality not wanted in
    profile, custom format score below minimum, size limits; never language,
    blocklist, seeders, parsing). Never CAM-type or Unknown quality, never 0
    seeders. Rank: approved, ≥3 seeders, wanted episodes covered, resolution,
    format score, seeders. One try per rotation; at most 2 searches a pass.
- `http_json` pulls `message`/`errorMessage`/`title` out of Arr error bodies, so
  the UI shows the real reason.

- **Leftovers** (`claimed_hashes` → `orphan_list` and the overview count):
  - Claimed: imported hashes plus queue hashes.
  - Not claimed: `subgate-rejected` torrents (they're only stopped when
    subgate can't find them in the queue), and complete `importBlocked`
    copies whose episodes/movie already have a file or that have no grab in
    history at all.
  - **Disk beats history** for finished torrents: claimed if any file has
    a hardlink count >1 (`Volume.link_counts`, `docker exec stat` from the
    host). Sonarr deletes history along with deleted episode rows, and keeps
    "imported" history after the library copy is gone. History is only used
    when the files can't be read.
  - Torrents are never deleted automatically. The user deletes them in
    Cleanup → Leftover torrents.

## Live configuration worth knowing

- **Python services need `init: true`** (library, subgate, gpuswitch). Python
  as PID 1 ignores SIGTERM, so a Docker restart SIGKILLs it and then leaves it
  down. This happened on 2026-10-05.
- **Jellyfin runs with `runtime: nvidia`.** Without nvidia-container-toolkit it
  won't start. NVENC is used on AC only; the dGPU runtime-suspends when idle
  (`/sys/bus/pci/devices/0000:01:00.0/power/runtime_status`).
- **Jellyfin transcoding is owned by gpuswitch.** It rewrites
  HardwareAccelerationType, QsvDevice, decode codecs, tone mapping, throttling
  and segment deletion every 15 s, so edit `gpuswitch.py`, not the dashboard.
  Its own Jellyfin key is `JELLYFIN_GPUSWITCH_API_KEY`. Jellyfin wants the key
  in an `Authorization: MediaBrowser Token="…"` header; `api_key=` in the
  query string gets a 401. Intel is the i915 node (vendor 0x8086), and NVIDIA
  holds the other one. The streaming API refuses `StartTimeTicks` on HLS segments.

- **qB:**
  - Paths: automatic torrent management, save `/data/downloads`, incomplete
    `/data/downloads/incomplete`, content layout Original.
  - Network: port 6881, UPnP off, upload limit 100 KiB/s.
  - Queue: 5 downloads / 3 uploads / 8 active, with "don't count slow
    torrents" on, so one dead torrent can't block the queue.
  - No seeding limits.
- **Recyclarr owns quality profiles, custom formats and quality definitions**
  in both Arrs (daily sync; `delete_old_custom_formats` on). Change
  `recyclarr/media.yml`, not the Arr UI, or the next sync reverts it. Sync now:
  `docker compose exec recyclarr recyclarr sync`; dry run: `... sync --preview`
  (details land in `config/recyclarr/logs/cli/*.debug.log`, API keys redacted).
  Image tag is the major version (`:8`); there is no `latest`. v8 gotchas: a
  quality group needs ≥2 qualities; groups a template lists uncommented are
  synced by default, so dropping one means `skip:`, not leaving it out.
- **Radarr:**
  - Profile `HD Bluray + WEB` (Recyclarr; id 7): no Remux, no upgrades, x265
    allowed (Golden Rule HD skipped), language Original. The old `HD-1080p`
    still exists, unused.
  - Root `/data/media/movies`; hardlinks and rename on.
  - Completed download handling plus auto-redownload of failed downloads,
    including ones grabbed from interactive search.
  - qB client: remove completed/failed on.
- **Sonarr:**
  - Anime: `Anime 1080p` (Recyclarr's TRaSH `[Anime] Remux-1080p` minus Remux;
    id 7): no upgrades, min format score 0 (TRaSH says 100, which refuses
    unranked groups), Dubs Only 0. Other shows: `HD - 720p/1080p`.
  - Root `/data/media/tv`, season folders, rename off.
  - RSS every 10 min.
- **Prowlarr:**
  - 21 public indexers, full-synced to both Arrs via `http://prowlarr:9696`.
    Sonarr's anime category is 5070.
  - FlareSolverr proxy at `http://flaresolverr:8191/`, used only by indexers
    tagged `flaresolverr`.
- **Seerr:**
  - Connected to Jellyfin at `jellyfin:8096`.
  - Radarr defaults: `HD Bluray + WEB`, `/data/media/movies`, minimum
    availability released.
  - Sonarr defaults: `HD - 720p/1080p`, anime profile `Anime 1080p`,
    `/data/media/tv`, season folders. `Kind.profile_and_root(anime=)` reads the
    same fields, so the library page matches. Seerr's settings PUT rejects a
    body containing `id`.
- **Jellyfin:** `Movies` and `Shows` libraries with real-time monitoring.
  - Plugin Intro Skipper (repo `https://intro-skipper.org/manifest.json`;
    plain curl gets a GitHub page, send `User-Agent: Jellyfin-Server/<ver>`).
    Auto-detects on new items; needs ≥2 episodes in a season. Task key
    `IntroSkipperDetectSegmentsTask`; results via `GET /MediaSegments/{itemId}`.
- **Bazarr:** `config/bazarr/config/config.yaml` (API key under `auth.apikey`).
  Languages profile 1 `English` is the default for series and movies.
  Providers without accounts: tsukihime (anime; needs only AniDB *series* ids,
  which Bazarr maps itself), gestdown, tvsubtitles, yifysubtitles. Animetosho
  needs an AniDB API client, so it's off. Podnapisi is gone from current
  Bazarr. Settings API: `POST /api/system/settings` form fields
  `settings-<section>-<key>`, header `X-API-KEY`.

Check live values through the Arr APIs (`/api/v3/config/*`, header
`X-Api-Key`) rather than trusting this list.

---

## Build, run, test

```bash
cd ~/media-stack
./media.sh start                 # creates config dirs + volume, then compose up --build
./media.sh status | logs <svc> | doctor
docker compose up -d --build library     # after editing library/*
docker compose up -d --build subgate     # after editing subgate/*
docker compose up -d <svc>               # after editing .env (restart does NOT re-read env)

# tests: unittest
(cd library && python3 -m unittest discover -s tests -t .)   # medialib only; test_web.py skips
(cd library && .venv/bin/python -m unittest discover -s tests -t .)   # all, in a venv with requirements-dev.txt
(cd subgate && python3 -m unittest -q)

# library UI dev loop (venv + tailwindcss standalone binary, v4)
(cd library && tailwindcss --input web/styles/app.css --output web/static/app.css --watch)
(cd library && LIBRARY_PASSWORD=dev LIBRARY_WATCHER=0 LIBRARY_JANITOR=0 IMPORT_ROOT=~/Downloads \
   WATCHLIST_STATE=/tmp/watchlist.json .venv/bin/uvicorn web.app:create_app --factory --host 127.0.0.1 --port 8098)
```

- **Dependencies:** subgate and `library/medialib` are stdlib only (subgate's
  image needs `ffprobe`; medialib must import on the host's plain Python for
  `cli.py`). `library/web` may use the pinned packages in
  `library/requirements.txt`. Keep them pinned.
- **library Dockerfile:** stage 1 downloads the Tailwind standalone CLI and
  builds `app.css` from `web/styles` + `web/templates`; stage 2 copies the
  `medialib/` and `web/` folders whole. `web/static/app.css` is gitignored and
  only built.
- **Restart loops:** if `.env` lacks a key, both custom containers exit at
  startup and restart-loop. `Stack()` and subgate's `Config` fail fast on
  missing settings.
- **Checking the UI:** start headless Chromium with
  `--remote-debugging-port`, set `Network.setExtraHTTPHeaders` with the Basic
  auth header, and screenshot at desktop (1200 px) and phone (390 px) widths.
  Check for horizontal overflow and for dark-mode readability.

## Common tasks

| Task | Where |
|---|---|
| New library feature | function in a `medialib/` module (+ test in `tests/`) → route in `web/routes/<tab>.py` → template in `web/templates/` (+ `tests/test_web.py` for web behaviour) |
| Change what subgate accepts | `decide_streams` / `classify_sub_path` in `subgate.py` + tests + `subgate/README.md` |
| Stuck download | Library → Releases dialog, or `release_drop` |
| Title still "available" in Seerr after delete | delete via library (clears Seerr), or Seerr → Manage → Clear Data |
| Settings changed in an app UI | update `SETUP.md` to match |
| Change quality rules (sizes, groups, upgrades) | `recyclarr/media.yml` → `recyclarr sync --preview` → `sync`; update SETUP.md step 11 |

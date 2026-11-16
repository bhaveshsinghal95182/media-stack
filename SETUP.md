# Media Stack: setting it up by hand

This guide sets up the whole stack from an empty laptop with no AI help. Follow
the steps in order. Every value you need to type is listed. Expect about an
hour the first time.

---

## What you end up with

| App | Address | What it does | Reachable from |
|---|---|---|---|
| **Jellyfin** | http://localhost:8096 | Plays your movies and shows (TV, phone, browser) | LAN |
| **Seerr** | http://localhost:5055 | Netflix-style "request a movie/show" page | LAN |
| **Media library** | http://localhost:8099 | Our own page: library, add, follow anime, releases, import, cleanup | LAN (password) |
| Radarr | http://localhost:7878 | Finds and downloads movies | this laptop only |
| Sonarr | http://localhost:8989 | Finds and downloads TV shows and anime | this laptop only |
| Prowlarr | http://localhost:9696 | Searches the torrent indexers for Radarr/Sonarr | this laptop only |
| Bazarr | http://localhost:6767 | Downloads English subtitles that are still missing | this laptop only |
| qBittorrent | http://localhost:8080 | Downloads the torrents | this laptop only |
| FlareSolverr | (no page) | Helps Prowlarr get past Cloudflare checks | internal |
| subgate | (no page) | Rejects downloads with no English subtitles and no English audio | internal |
| Recyclarr | (no page) | Keeps Radarr/Sonarr's quality rules in line with the TRaSH Guides | internal |

From another device on your Wi-Fi, replace `localhost` with the laptop's LAN
address. `./media.sh lan-ip` prints it.

How a download flows:

```
Seerr / Media library  ->  Radarr or Sonarr  ->  Prowlarr (indexers)
        ->  qBittorrent downloads  ->  subgate checks for English subs/audio
        ->  Radarr/Sonarr move it into the library  ->  Bazarr adds missing subtitles
        ->  Jellyfin shows it (Intro Skipper marks intros and credits)
```

**Media is disposable by design.** `./media.sh destroy` deletes every download
and every movie/show file but keeps all app settings. `./media.sh reset` deletes
the settings too. Settings live in `./config/` and `.env`. Back those two up
(step 13) and you never have to repeat this guide.

---

## 0. What you need

- An Arch-based Linux (built and tested on Omarchy).
- About 20 GB free for the apps, plus space for whatever you download.
- This repository.

Only use indexers and downloads you're legally allowed to use where you live.

---

## 1. Install Docker

```bash
sudo pacman -S --needed docker docker-compose docker-buildx git python
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in (or reboot) so the `docker` group applies, then check:

```bash
docker run --rm hello-world
docker compose version
```

Both should work without `sudo`.

---

## 2. Get the repository

```bash
git clone <your-private-repo-url> ~/media-stack
cd ~/media-stack
```

If you have a backup from step 13, restore it now (step 13 explains how) and
skip straight to step 11.

---

## 3. Create `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Open `.env` in an editor and fill in what you can now:

| Variable | Value |
|---|---|
| `PUID` | output of `id -u` |
| `PGID` | output of `id -g` |
| `TZ` | output of `timedatectl show -p Timezone --value` |
| `LIBRARY_USERNAME` | any name you like (or leave empty to accept any name) |
| `LIBRARY_PASSWORD` | a long password; `openssl rand -base64 12` makes one |
| `IMPORT_DIR` | `/home/<you>/Downloads`, the folder the Import tab may read |

Leave `QBIT_*`, `RADARR_API_KEY` and `SONARR_API_KEY` empty for now. You'll get
them in steps 5–7.

---

## 4. Start the stack

```bash
./media.sh start
./media.sh doctor
```

Always start with `./media.sh start` rather than a bare `docker compose up`. It
first creates the `config/` folders as your user (Docker would create them as
root, and subgate/library could not write to them) and the media volume.

The first start downloads the images and builds two local ones (`subgate` and
`library`), which takes a few minutes.

`subgate` and `library` keep restarting until `.env` is complete (step 11).
That's expected.

---

## 5. qBittorrent

1. Get the one-time admin password: `./media.sh password`.
2. Open http://localhost:8080 and log in as `admin` with that password.
3. **Tools → Options → WebUI → Authentication**: set your own username and a
   strong password. Put the same two values in `.env` as `QBIT_USERNAME` and
   `QBIT_PASSWORD`.

   > qBittorrent bans an IP address for **1 hour** after 5 wrong passwords. If
   > Radarr, Sonarr, subgate or the library page suddenly can't connect, check
   > for a typo in `.env` before retrying.

4. **Options → Downloads**:
   - Default Torrent Management Mode: **Automatic**
   - Default Save Path: `/data/downloads`
   - Keep incomplete torrents in: **on**, `/data/downloads/incomplete`
   - Torrent content layout: **Original**
5. **Options → Connection**: Listening port `6881`. Use UPnP: **off**.
6. **Options → Speed**: Global upload limit `100` KiB/s. This keeps home Wi-Fi
   usable. Leave download unlimited.
7. **Options → BitTorrent**:
   - Torrent Queueing: **on**
   - Maximum active downloads `5`, uploads `3`, torrents `8`
   - Do not count slow torrents in these limits: **on**. Without it, one dead
     torrent can block new downloads for days.
   - Seeding limits: off
8. Save. Don't create categories; Radarr (`radarr`) and Sonarr (`tv-sonarr`)
   create them on their own.

---

## 6. Radarr (movies)

Open http://localhost:7878. On first run it asks you to pick a login method:
choose **Forms**, then set a username and password.

1. **Settings → Media Management** (click "Show Advanced" at the top):
   - Rename Movies: **on**
   - Standard Movie Format: `{Movie Title} ({Release Year}) {Quality Full}`
   - Movie Folder Format: `{Movie Title} ({Release Year})`
   - Use Hardlinks instead of Copy: **on**. Without hardlinks every movie uses
     double the disk space.
   - Import Extra Files: **on**, extensions `srt,ass,ssa,vtt,sub,idx`. subgate
     accepts releases whose English subtitles are a separate file, and without
     this Radarr leaves that file behind.
   - Root Folders → Add Root Folder: `/data/media/movies`
2. **Settings → Download Clients → +** → qBittorrent:
   - Host `qbittorrent`, Port `8080`, your qBittorrent username and password
   - Category `radarr`
   - Remove Completed: **on**, Remove Failed: **on**
   - Test, then Save
3. On the same page, under **Completed Download Handling**, keep **Enable**
   on. Under **Failed Download Handling**, turn on **Redownload** and **Redownload
   from Interactive Search**. That's how subgate's rejections lead to a new
   search.
4. **Settings → Profiles**: nothing to do. Recyclarr creates **HD Bluray + WEB**
   in step 11.
5. **Settings → General → Security → API Key**: copy it into `.env` as
   `RADARR_API_KEY`.

---

## 7. Sonarr (TV and anime)

Open http://localhost:8989 and set up the login (Forms) the same way.

1. **Settings → Media Management** (Show Advanced):
   - Use Hardlinks instead of Copy: **on**
   - Import Extra Files: **on**, extensions `srt,ass,ssa,vtt,sub,idx`
   - Root Folders → Add: `/data/media/tv`
   - Season Folder Format: `Season {season}`
2. **Settings → Download Clients → +** → qBittorrent: Host `qbittorrent`, Port
   `8080`, your login, Category `tv-sonarr`, Remove Completed/Failed **on**.
3. Turn on Completed Download Handling and Failed Download Handling → Redownload,
   as in Radarr.
4. **Settings → Indexers** (Show Advanced): RSS Sync Interval `10` minutes, the
   lowest Sonarr allows. New anime episodes get picked up faster.
5. **Settings → Profiles**: regular shows use the built-in **HD - 720p/1080p**.
   Anime gets **Anime 1080p**, which Recyclarr creates in step 11.
6. **Settings → General → API Key**: copy it into `.env` as `SONARR_API_KEY`.

---

## 8. Prowlarr (indexers)

Open http://localhost:9696 and set up the login (Forms).

1. **Settings → Indexers → Indexer Proxies → +** → FlareSolverr:
   - Name `FlareSolverr`, Host `http://flaresolverr:8191/`, Tags: type
     `flaresolverr` and press Enter. Save.
2. **Indexers → Add Indexer**. These public indexers are currently set up:
   - General: 0Magnet, 1337x, ExtraTorrent.st, EZTV, Internet Archive,
     kickasstorrents.to, kickasstorrents.ws, Knaben, LimeTorrents, The Pirate
     Bay, TorrentDownload, Uindex, World-torrent, YTS
   - Anime: ACG.RIP, Anime Tosho, Bangumi Moe, BigFANGroup, nekoBT, Shana
     Project, Tokyo Toshokan

   Add each with default settings and press Test. If a test fails with a
   Cloudflare or "blocked" error, edit that indexer and add the tag
   `flaresolverr`.
3. **Settings → Apps → +** → Radarr:
   - Sync Level **Full Sync**, Prowlarr Server `http://prowlarr:9696`,
     Radarr Server `http://radarr:7878`, API Key = Radarr's key. Test, then Save.
4. **Settings → Apps → +** → Sonarr: same, Sonarr Server
   `http://sonarr:8989`, Sonarr's key. Keep **Anime Sync Categories** at
   `5070` (TV/Anime).
5. Press **Sync App Indexers** (the button at the top of the Indexers page). In
   Radarr and Sonarr, **Settings → Indexers** now lists every indexer with
   "(Prowlarr)" after its name.

---

## 9. Jellyfin (playback)

Open http://localhost:8096 and go through the wizard:

1. Pick your language and create the admin user.
2. **Add Media Library**:
   - Content type **Movies**, display name `Movies`, folder `/data/media/movies`
   - Content type **Shows**, display name `Shows`, folder `/data/media/tv`
   - In both, turn on **Enable real-time monitoring**. New files then appear
     within a minute without a manual scan.
3. Finish the wizard. Allow remote connections: **on**. That only means your
   LAN, since nothing is forwarded on the router.
4. On the TV/phone app, add the server `http://<laptop LAN IP>:8096`.

### 9a. Hardware transcoding (Intel Quick Sync, NVIDIA on AC)

Without this, a transcode runs on the CPU at full load. A phone that can't
draw styled subtitles makes Jellyfin burn them in, which means a transcode.

The `gpuswitch` container sets all of this for you and keeps it set. Here is
what it changes in **Dashboard → Playback → Transcoding**, if you do it by hand:

- **Hardware acceleration:** Intel QuickSync (QSV). QSV device: the Intel
  render node (check with `ls -l /dev/dri/by-path/`, the `pci-0000:00:02.0-render`
  entry; gpuswitch finds it by vendor on every check, since numbering can change).
- **Enable hardware decoding for:** H264, HEVC, MPEG2, VC1, VP8, VP9, AV1;
  10-bit HEVC and VP9 on.
- **Hardware encoding:** on. **Tone mapping** and **VPP tone mapping:** on.
- **Throttle transcodes** and **Delete segments:** on. Without throttling, one
  stream encodes the whole rest of the film at full power.

The compose file gives Jellyfin `/dev/dri`. On battery gpuswitch always uses
QSV. On AC it uses `JELLYFIN_AC_HWACCEL` from `.env` (`qsv` or `nvenc`).
When AC is lost, it stops NVENC transcodes so the NVIDIA GPU can sleep, and
the player restarts on QSV.

**`compose.yaml` expects an NVIDIA GPU.** The `jellyfin` service has
`runtime: nvidia`, and Jellyfin won't start until Docker knows that runtime:

1. As root, once: `bash gpuswitch/setup-nvidia-runtime.sh`. This installs
   `nvidia-container-toolkit`, registers it with Docker and restarts Docker.
2. Set `JELLYFIN_AC_HWACCEL=nvenc` in `.env`, then run
   `docker compose up -d jellyfin gpuswitch`.

On a machine without an NVIDIA GPU, delete `runtime: nvidia` and the two
`NVIDIA_*` lines from the `jellyfin` service, and set `JELLYFIN_AC_HWACCEL=qsv`.

### 9b. Intro Skipper (skip openings and credits)

1. **Dashboard → Plugins → Repositories → +**: name `Intro Skipper`, URL
   `https://intro-skipper.org/manifest.json`. Save.
2. **Plugins → Catalog → Intro Skipper → Install**, then **Dashboard → Restart**.
3. **Dashboard → Scheduled Tasks → Detect and Analyze Media Segments → ▶** for
   what you already have. New episodes are analysed when they arrive.

It compares the episodes of a season to find the shared opening and ending, so a
season needs at least two episodes before it finds anything. Players then show
a **Skip Intro** button. To skip without pressing anything: your user menu →
**Settings → Playback → Media Segments**, set Intro/Outro to **Skip**.

---

## 10. Seerr (requests)

Open http://localhost:5055:

1. Choose **Jellyfin**. Server: hostname `jellyfin`, port `8096`, no SSL. Use
   `http://localhost:8096` as the external URL. Sign in with the Jellyfin admin
   account.
2. Sync libraries and enable **Movies** and **Shows**.
3. **Radarr**: hostname `radarr`, port `7878`, API key = Radarr's key, Test,
   then profile **HD-1080p** for now (step 11 switches it), root folder
   `/data/media/movies`, minimum availability **Released**. Tick **Default server**.
4. **Sonarr**: hostname `sonarr`, port `8989`, API key = Sonarr's key, Test,
   then quality profile and anime profile **HD - 720p/1080p** (step 11 switches
   the anime one), root folder (and anime root) `/data/media/tv`, Season Folders
   **on**. Tick **Default server**.
5. Finish. Other people on your Wi-Fi can now request at `http://<LAN IP>:5055`.

---

## 11. Finish `.env` and start the custom parts

`.env` should now have every value filled in. Recreate the containers that
read it:

```bash
docker compose up -d --build subgate library recyclarr
./media.sh logs subgate     # expect "subgate started"; Ctrl+C to leave
./media.sh logs library     # expect "media-library on :8099" and "airing watcher on"
docker compose exec recyclarr recyclarr sync   # first sync now instead of tonight
```

**Recyclarr** applies the [TRaSH Guides](https://trash-guides.info) quality
rules: which release groups are good, which encodes to avoid, sensible file
sizes. Its settings are `recyclarr/media.yml` (in git); it syncs daily. It
creates **HD Bluray + WEB** in Radarr and **Anime 1080p** in Sonarr. Both are
tuned for a small disk: no upgrades once something is downloaded, no Remux,
x265 allowed. After the first sync:

1. Seerr → **Settings → Services**: Radarr profile **HD Bluray + WEB**; Sonarr
   anime profile **Anime 1080p** (leave the normal profile as is).
2. Existing titles: Radarr **Movies → Select Movies** (all) → Quality Profile
   **HD Bluray + WEB**; Sonarr **Series → Select Series** (the anime) → **Anime
   1080p**. Nothing re-downloads, since upgrades are off.

The library page follows Seerr, so it uses the same profiles.

### 11a. Bazarr (subtitles)

Open http://localhost:6767:

1. **Settings → Languages**: Languages Filter **English**. Add a Languages
   Profile named `English` with English in it, then under **Default Settings**
   turn on Series and Movies with that profile.
2. **Settings → Providers**: add these; none needs an account:
   - **TsukiHime** (anime), **Gestdown** and **TVSubtitles** (TV),
     **YIFY Subtitles** (movies)
   - Optional, with a free account: **OpenSubtitles.com**, the biggest source.
3. **Settings → Sonarr**: on, address `sonarr`, port `8989`, Sonarr's key. Test,
   Save. **Settings → Radarr**: same with `radarr`, `7878`, Radarr's key.
4. **Settings → General → Analytics**: off.

It runs on its own: new imports get English subtitles if they lack them, and it
searches again every few hours for anything still missing. **Wanted** lists what
it couldn't find; **History** shows what it fetched.

Open http://localhost:8099 and log in with `LIBRARY_USERNAME` /
`LIBRARY_PASSWORD`.

> If you change `.env` later, run `docker compose up -d <service>` (for example
> `docker compose up -d library`). `docker compose restart` keeps the old
> values. Your browser may remember an old page login, so use a private window
> after changing it.

---

## 12. Using it

**Media library page** (http://<LAN IP>:8099):

- **Library**: everything you have, with status.
  - **Releases**: see the current download (real seed count, speed, ETA). Drop
    it and let Radarr/Sonarr try another, or pick a specific release yourself.
    Use it when a download crawls.
  - **Delete**: removes the movie/show, its files, its torrent and the Seerr
    request, then checks that it's really gone.
  - Click a title for its details. For a show, each season lists what's on
    disk, missing, downloading and still to air. **Get** on a season makes
    Sonarr download its missing episodes (and the rest as they air). Episodes
    you watched and the cleanup removed are left out. To start partway
    through a long show, use **Watch from an episode, a few at a time** above
    the seasons (it opens by itself when lots of episodes are missing).
  - **Hide on TV** (the TV icon on a show): the Jellyfin `tv` account can no
    longer see or play that show. Press it again (crossed-out eye) to undo.
    Another account name goes in `.env` as `TV_USER`.
- **Add**: search and download a movie or show, or **Save for later**. A show
  first asks what to get: the latest season, only new episodes, a few at a
  time from an episode number (a rolling window), or every episode.
- **Watchlist**: titles you saved for later. Nothing downloads until you press
  **Download** (one title) or tick several and **Download selected**. They
  then go to Radarr/Sonarr and leave the list. A show's **Download** asks what
  to get, the same choice as Add; **Download selected** sends only movies. The list is a file,
  `config/library/watchlist.json`, so `./media.sh destroy` doesn't touch it.
  The backup in step 13 includes it.
- **Seasonal**: this season's anime from AniList. **Follow** makes Sonarr grab
  each new episode. Choose this season so far + new episodes, only new
  episodes, or everything.
- **Following**: what's coming up and what just aired, plus everything you
  follow. The airing watcher searches every indexer 5 min, 20 min, 45 min,
  1.5 h, 3 h, 6 h and 12 h after an episode airs, then twice a day for a week,
  until it's found.
- **Rolling window** (for very long shows like One Piece, where TVDB's seasons
  don't match arcs): Following → Follow → **Watch from an episode, a few at a
  time**, or a show's details dialog → **Watch from an episode**. Type the
  absolute episode number (the dialog shows which episode that is) and how many
  to keep ahead (default 12). Only those episodes are wanted in Sonarr; as you
  watch in Jellyfin, the next ones are grabbed, and watched ones are removed
  after 1 day instead of 3. Settings live in `config/library/rolling.json`.
- **Import**: copy a video from `IMPORT_DIR` into the library.
- **Cleanup**:
  - **Disk:** how much space is left. Downloads pause on their own below 20 GB
    free and resume above 25 GB. The media shares the laptop's disk, so
    filling it would break more than the stack.
  - **Watched · removed automatically:** anything either Jellyfin user has
    watched is removed 3 days after it was last played (1 day for shows on a
    rolling window), and straight away when space runs low.
    - Episodes are unmonitored first, so Sonarr won't download them again, and
      the show stays followed. Movies are deleted completely.
    - The torrent's data is deleted too. That's what actually frees the space.
      A season pack waits until all its episodes are watched.
    - **Favourites in Jellyfin are never removed.** **Keep** marks an item as a
      favourite.
    - Change the timing in `compose.yaml` → `library` → `environment` with
      `JANITOR_GRACE_DAYS`, `JANITOR_ROLLING_GRACE_DAYS`, `JANITOR_PAUSE_BELOW_GB` and
      `JANITOR_RESUME_ABOVE_GB`. Set `LIBRARY_JANITOR: "0"` to turn it off.
  - **Stalled downloads · replaced automatically:** a download that gets less
    than 10 MiB further in 60 minutes is removed with its data and
    blocklisted, the same as **Drop & try another**, and Radarr/Sonarr grab the
    next release. If 10 minutes later they've found nothing their quality
    settings accept, the library searches once more and grabs the best release
    they turned down only for its quality, size or format score: something
    beats nothing. Cam/telesync and unknown-quality releases are never taken,
    and subgate still checks for English subtitles/audio. Downloads paused for low disk space or waiting in
    qBittorrent's queue don't count, and the clock restarts after the laptop
    was off or asleep. Change it in `compose.yaml` → `library` → `environment`
    with `STALL_MINUTES` and `STALL_MIN_PROGRESS_MB`. Set `LIBRARY_ROTATE: "0"`
    to turn it off.
  - **Leftover torrents:** torrents nothing uses. That covers ones subgate
    rejected, extra copies of an episode you already have, and old downloads.
    Tick them and delete. Nothing here is deleted automatically.

**Terminal**:

```bash
./media.sh status            # what's running
./media.sh urls              # all addresses, including the LAN ones
./media.sh logs [service]    # follow logs, e.g. ./media.sh logs subgate
./media.sh stop              # stop, keep everything
./media.sh start             # start again
./media.sh update            # pull new images, rebuild subgate/library
./media.sh list              # library overview
./media.sh add movie "Spirited Away" --year 2001
./media.sh delete show "Some Show"
./media.sh import movie ~/Downloads/SomeMovie.mkv
./media.sh orphans --delete  # remove leftover torrents
./media.sh destroy           # DELETE all downloads/media, keep settings
```

**subgate** checks every Radarr/Sonarr download while it's still downloading.
It keeps it if there's a full English subtitle track (inside the file or as a
separate `.srt`) **or** English audio. Otherwise Radarr/Sonarr remove it,
blocklist it and search for the next release. See what it decided with
`./media.sh logs subgate`.

**Bazarr** then fills gaps after the import: a movie added by hand or a release
whose subtitles were only partial gets an `.en.srt` next to it.

---

## 13. Back up and restore

Everything that took you an hour lives in `.env` and `config/`. Back them up,
skipping caches:

```bash
cd ~/media-stack
./media.sh stop
tar --exclude=config/jellyfin/cache --exclude=config/jellyfin/log \
    --exclude=config/recyclarr/resources \
    -czf ~/media-stack-backup-$(date +%F).tar.gz .env config
./media.sh start
```

That's about 200 MB. Keep it somewhere safe (USB drive, cloud drive). It
contains passwords and API keys, so don't post it anywhere.

Restore on a new machine: do steps 1–2, then:

```bash
cd ~/media-stack
tar -xzf /path/to/media-stack-backup-YYYY-MM-DD.tar.gz
chmod 600 .env
./media.sh start
```

Your settings come back exactly as they were (downloads don't; they're
disposable). If your new user isn't uid/gid 1000, update `PUID`/`PGID` in `.env`
and run `sudo chown -R "$(id -u):$(id -g)" config`.

---

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| subgate/library keep restarting | `.env` is missing a value; `./media.sh logs subgate` names it. Then `docker compose up -d subgate library`. |
| Everything says qBittorrent login failed | Wrong `QBIT_PASSWORD`, and qBittorrent has now banned the IP for 1 hour. Fix `.env`, wait the hour or restart qBittorrent (`docker compose restart qbittorrent`), then `docker compose up -d subgate library`. |
| A download is stuck at 0 KB/s for days | Library → **Releases** → **Drop & try another**. Indexer seed counts are often stale. |
| Movie finished but Jellyfin doesn't show it | Radarr → Activity → Queue shows why it didn't import. Check that real-time monitoring is on in the Jellyfin library. |
| Seerr says "available" for something you deleted | Delete through the library page (it clears Seerr), or Seerr → the title → Manage → Clear Data. |
| Library page says "too many failed logins" | Wait 10 minutes. It locks after 10 wrong passwords. |
| subgate rejected something that did have English subs | `./media.sh logs subgate` shows what it saw. Releases → pick it manually and it's checked again. Or set `DRY_RUN: "true"` for subgate in `compose.yaml` to only log decisions. |
| Subtitles from Bazarr are out of sync | Bazarr → the movie/series → the subtitle's menu → **Sync**, or delete it and pick another under **Manual Search**. |
| No Skip Intro button | The season needs two or more episodes, and the scan must have run (Dashboard → Scheduled Tasks). Single-episode seasons usually get nothing until episode 2 arrives. |
| Radarr/Sonarr say "custom format score below minimum" or "not an upgrade" | Expected: Recyclarr's profiles refuse known bad groups and never upgrade. Pick a release by hand under Releases, or change `recyclarr/media.yml` and run `docker compose exec recyclarr recyclarr sync`. |
| A new anime episode isn't downloading | Following tab: status and watcher activity. Press **Search now**. If nothing is found, no indexer has it yet. |
| TV can't find Jellyfin | Use `http://<LAN IP>:8096` directly. The laptop and TV must be on the same Wi-Fi. Allow port 8096 in any firewall. |
| Cleanup says "no Jellyfin API key" or Jellyfin errors | Jellyfin → Dashboard → API Keys → +, put it in `.env` as `JELLYFIN_API_KEY`, then `docker compose up -d library`. |
| Downloads stopped and Cleanup says "paused" | The disk is nearly full. They resume by themselves above 25 GB free. Free space or watch/remove things. |
| Sonarr queue: "matched to series by ID. Automatic import is not possible" | The library imports these by itself within ~5 minutes (best copy subgate passed). If the watcher log says "import it by hand", use Sonarr → Activity → Queue → the person icon (Manual Import). |
| Sonarr shows every episode of a show twice | Sonarr refreshed it twice at once when it was added (the library no longer causes this). Fix: Series → the show → **Refresh & Scan**. Sonarr drops the extra entries and re-links the files. If two files are left for one episode, delete the worse one in Sonarr; its torrent then shows under Leftover torrents. |
| Wi-Fi gets slow while downloading | Lower the upload limit in qBittorrent (step 5.6). |

Full reset (deletes **all settings** and media, back to step 4):
`./media.sh reset`.

# media-stack

One laptop, seven apps, zero subscriptions. A self-hosted media stack: ask for a
film, it downloads, it lands in the library, it plays on the TV.

```
Seerr ─→ Radarr ─┐
         Sonarr ─┴→ Prowlarr → qBittorrent → Radarr/Sonarr import → Jellyfin
```

`subgate` sits between the Arrs and qBittorrent and rejects a release that has no
English subtitles and no English audio before most of it downloads. `library` is
the page on top: browse, add, follow, import, clean up.

## Services

| service | port | what it does |
|---|---:|---|
| Prowlarr | 9696 | indexers, handed to Radarr and Sonarr |
| Radarr | 7878 | films |
| Sonarr | 8989 | shows |
| Bazarr | 6767 | fills in missing subtitles |
| qBittorrent | 8080 | downloads |
| Jellyfin | 8096 | plays it |
| Seerr | 5055 | the request page |
| library | 8099 | our own page: add, watchlist, follow, import, cleanup |
| subgate | — | English-subtitle gate on the download queue |
| FlareSolverr | — | helps Prowlarr past Cloudflare |
| Recyclarr | — | keeps the quality profiles current |
| gpuswitch | — | Jellyfin transcoding on AC, off on battery |

The Arrs and qBittorrent are bound to `127.0.0.1`; Jellyfin, Seerr and the library
page are on the LAN.

## Install

`SETUP.md` is the step-by-step, including the one-time passwords and the order to
add the apps to each other. The short version:

```
cp .env.example .env      # fill in the keys and paths
docker compose up -d
```

Configs live in `config/` and are not in the repo. `.env` is not in the repo.

## Related

- [subgate](https://github.com/bhaveshsinghal95182/subgate) — the subtitle gate
- [library](https://github.com/bhaveshsinghal95182/library) — the web page

## Notes

This is one household's stack, written for one machine, not a distribution. The
paths assume a single disk and a `scratch_data` volume for downloads in progress.
Read `SETUP.md` before changing ports: several services address each other by
container name.

# Ephemeral Media Stack for Omarchy — Agent Build Specification

> **Purpose:** This file is meant to be dropped into an empty project folder and given to a coding/terminal agent.  
> The agent should read this entire document, create the stack, validate it, start it, and leave the user with working local URLs plus the remaining one-time UI configuration steps.
>
> **Platform target:** Omarchy / Arch Linux laptop using Docker Engine + Docker Compose.
>
> **Design goal:** One command turns the laptop into a temporary media server. App configuration survives restarts and `destroy`; downloaded media does not. Another command destroys all temporary downloads/media while preserving the app configuration.
>
> Only configure indexers/download sources for content the user is authorized to access. Do not automatically add piracy-specific indexers, scrape protected sites, alter the router, or expose admin interfaces to the public Internet.

---

## 0. Agent operating rules

The agent MUST:

1. Read this document fully before changing anything.
2. Work only inside the directory containing this file, except for:
   - installing/enabling Docker if it is missing;
   - enabling the Docker system service;
   - adding the current user to the `docker` group if required.
3. Detect values such as UID, GID, timezone, and current LAN IP instead of hard-coding them.
4. Never delete unrelated Docker containers, images, networks, or volumes.
5. Never run `docker system prune`, `docker volume prune`, or similar global cleanup commands.
6. Never run the stack's `reset` operation unless the user explicitly asks for a full reset.
7. Do not expose Radarr, Sonarr, Prowlarr, qBittorrent WebUI, Whisparr, Seerr, or FlareSolverr outside the host by default.
8. Expose Jellyfin to the LAN so the user's TV/phone can connect.
9. Do not configure router port forwarding, UPnP, public DNS, Cloudflare tunnels, or remote Internet access.
10. Validate the generated Compose file before starting it.
11. If Docker setup requires the user to log out/in for group membership to apply, say so clearly rather than attempting dangerous permission workarounds.
12. Preserve existing files if this folder is not empty. Before overwriting an existing `compose.yaml`, `.env`, or `media.sh`, make a timestamped backup in `./backup/`.

---

# 1. Architecture

The stack is:

```text
                         ┌─────────────┐
                         │    Seerr    │
                         │  localhost  │
                         └──────┬──────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
                 Radarr                   Sonarr
                 Movies                    TV
                    │                       │
                    └───────────┬───────────┘
                                │
                            Prowlarr
                                │
                         configured indexers
                                │
                                ▼
                          qBittorrent
                                │
                                ▼
                    ┌─────────────────────┐
                    │  scratch_data       │
                    │                     │
                    │ /data/downloads     │
                    │ /data/media/movies  │
                    │ /data/media/tv      │
                    │ /data/media/adult   │
                    └──────────┬──────────┘
                               │
                               ▼
                            Jellyfin
                               │
                     LAN :8096 / TV / phone


Whisparr
   │
   ├── Prowlarr / configured compatible indexers
   ├── qBittorrent
   └── /data/media/adult
```

Important behavior:

```text
./media.sh stop
    → stop containers
    → preserve temporary media

./media.sh destroy
    → remove containers/network
    → delete scratch_data volume
    → delete ALL downloads/media
    → PRESERVE app configurations

./media.sh reset
    → delete downloads/media
    → delete ALL app configurations
    → full first-run state
    → MUST require explicit confirmation
```

---

# 2. Files the agent must create

After setup, the directory should look like:

```text
media-stack/
├── AGENT_SETUP.md               # this document, filename may differ
├── compose.yaml
├── .env
├── media.sh
├── .gitignore
├── config/
│   ├── jellyfin/
│   ├── prowlarr/
│   ├── radarr/
│   ├── sonarr/
│   ├── qbittorrent/
│   ├── seerr/
│   └── whisparr/
└── backup/                      # only if old files had to be backed up
```

Actual downloaded media MUST NOT live under this project directory. It belongs in a Docker named volume named:

```text
media-stack-scratch
```

This makes destruction deterministic and prevents accidental deletion of config.

---

# 3. Docker installation on Omarchy / Arch

First check:

```bash
docker --version
docker compose version
```

If both commands work, do not reinstall Docker.

If Docker is missing, Omarchy is Arch-based, so prefer:

```bash
sudo pacman -S --needed docker docker-compose
sudo systemctl enable --now docker
```

Check access:

```bash
docker info
```

If the Docker daemon is running but the current user receives a socket permission error:

```bash
sudo usermod -aG docker "$USER"
```

Then tell the user that a logout/login (or reboot) may be required before non-root Docker access works.

Do NOT `chmod 666 /var/run/docker.sock`.

After Docker access works:

```bash
docker info >/dev/null
docker compose version
```

Both must succeed.

---

# 4. Generate `.env`

Detect values:

```bash
id -u
id -g
timedatectl show -p Timezone --value
```

Create `.env`:

```dotenv
PUID=<output of id -u>
PGID=<output of id -g>
TZ=<output of timedatectl show -p Timezone --value>
```

Example only:

```dotenv
PUID=1000
PGID=1000
TZ=Asia/Kolkata
```

Never assume UID/GID is `1000`.

---

# 5. Create persistent config directories

Create:

```bash
mkdir -p \
  config/jellyfin \
  config/prowlarr \
  config/radarr \
  config/sonarr \
  config/qbittorrent \
  config/seerr \
  config/whisparr
```

They should belong to the current user:

```bash
chown -R "$(id -u):$(id -g)" config
chmod -R u+rwX,g+rwX config
```

Do not make the config world-writable.

---

# 6. `compose.yaml`

Create exactly this baseline Compose file unless a current image name is demonstrably invalid.

```yaml
name: media-stack

x-linuxserver-env: &linuxserver-env
  PUID: ${PUID}
  PGID: ${PGID}
  TZ: ${TZ}
  UMASK: "002"

services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: media-prowlarr
    environment:
      <<: *linuxserver-env
    volumes:
      - ./config/prowlarr:/config
    ports:
      - "127.0.0.1:9696:9696"
    networks:
      - media_net
    restart: unless-stopped

  radarr:
    image: lscr.io/linuxserver/radarr:latest
    container_name: media-radarr
    environment:
      <<: *linuxserver-env
    volumes:
      - ./config/radarr:/config
      - scratch_data:/data
    ports:
      - "127.0.0.1:7878:7878"
    networks:
      - media_net
    restart: unless-stopped

  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    container_name: media-sonarr
    environment:
      <<: *linuxserver-env
    volumes:
      - ./config/sonarr:/config
      - scratch_data:/data
    ports:
      - "127.0.0.1:8989:8989"
    networks:
      - media_net
    restart: unless-stopped

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: media-qbittorrent
    environment:
      <<: *linuxserver-env
      WEBUI_PORT: "8080"
      TORRENTING_PORT: "6881"
    volumes:
      - ./config/qbittorrent:/config
      - scratch_data:/data
    ports:
      # Web UI: host only
      - "127.0.0.1:8080:8080"

      # BitTorrent peer port.
      # This does NOT itself configure router port forwarding.
      - "6881:6881/tcp"
      - "6881:6881/udp"
    networks:
      - media_net
    stop_grace_period: 30s
    restart: unless-stopped

  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: media-jellyfin
    environment:
      <<: *linuxserver-env
    volumes:
      - ./config/jellyfin:/config
      - scratch_data:/data:ro
    ports:
      # Intentionally LAN-accessible for TV / phone playback.
      - "8096:8096"

      # Jellyfin client discovery on the LAN.
      - "7359:7359/udp"
    networks:
      - media_net
    restart: unless-stopped

  seerr:
    image: ghcr.io/seerr-team/seerr:latest
    container_name: media-seerr
    init: true
    user: "${PUID}:${PGID}"
    environment:
      LOG_LEVEL: info
      TZ: ${TZ}
      PORT: "5055"
    volumes:
      - ./config/seerr:/app/config
    ports:
      - "127.0.0.1:5055:5055"
    networks:
      - media_net
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "wget --no-verbose --tries=1 --spider http://localhost:5055/api/v1/settings/public || exit 1"
        ]
      start_period: 20s
      timeout: 3s
      interval: 15s
      retries: 3
    security_opt:
      - no-new-privileges
    cap_drop:
      - ALL
    restart: unless-stopped

  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: media-flaresolverr
    environment:
      LOG_LEVEL: info
      LOG_HTML: "false"
      CAPTCHA_SOLVER: none
      TZ: ${TZ}
    # No host port. Prowlarr reaches it internally at:
    # http://flaresolverr:8191
    networks:
      - media_net
    restart: unless-stopped

  whisparr:
    image: ghcr.io/hotio/whisparr:v3
    container_name: media-whisparr
    environment:
      PUID: ${PUID}
      PGID: ${PGID}
      UMASK: "002"
      TZ: ${TZ}
      WEBUI_PORTS: "6969/tcp"
    volumes:
      - ./config/whisparr:/config
      - scratch_data:/data
    ports:
      - "127.0.0.1:6969:6969"
    networks:
      - media_net
    restart: unless-stopped

networks:
  media_net:
    name: media-stack-network

volumes:
  scratch_data:
    name: media-stack-scratch
```

Notes:

- `scratch_data` is one filesystem shared by qBittorrent, Radarr, Sonarr, Whisparr, and Jellyfin.
- This allows hardlinks between `/data/downloads` and `/data/media/...`.
- Jellyfin receives the media volume read-only.
- Only Jellyfin is intentionally LAN-facing.
- FlareSolverr has no host port.
- Do not add Watchtower. Updates are manual through `media.sh update`.

Validate before starting:

```bash
docker compose config --quiet
```

If validation fails, fix it before doing anything else.

---

# 7. Create `media.sh`

Create an executable Bash script with this content:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="$ROOT/compose.yaml"
ENV_FILE="$ROOT/.env"
SCRATCH_VOLUME="media-stack-scratch"

declare -A URLS=(
  [Seerr]="http://localhost:5055"
  [Jellyfin]="http://localhost:8096"
  [Radarr]="http://localhost:7878"
  [Sonarr]="http://localhost:8989"
  [Prowlarr]="http://localhost:9696"
  [qBittorrent]="http://localhost:8080"
  [Whisparr]="http://localhost:6969"
)

header() {
  printf '\n===================================================\n'
  printf '  %s\n' "$1"
  printf '===================================================\n\n'
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is required but was not found."
}

load_env() {
  [[ -f "$ENV_FILE" ]] || die ".env is missing."
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  : "${PUID:?PUID missing from .env}"
  : "${PGID:?PGID missing from .env}"
  : "${TZ:?TZ missing from .env}"
}

check_docker() {
  need docker
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable or current user lacks permission."
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable."
}

check_compose() {
  [[ -f "$COMPOSE_FILE" ]] || die "compose.yaml is missing."
  docker compose -f "$COMPOSE_FILE" config --quiet
}

init_scratch() {
  echo "Preparing disposable media filesystem..."

  docker volume create "$SCRATCH_VOLUME" >/dev/null

  docker run --rm \
    -v "${SCRATCH_VOLUME}:/data" \
    alpine:latest \
    sh -c "mkdir -p \
      /data/downloads \
      /data/media/movies \
      /data/media/tv \
      /data/media/adult \
      && chown -R ${PUID}:${PGID} /data \
      && chmod -R 775 /data"

  echo "Disposable filesystem ready."
}

show_urls() {
  echo
  echo "Local interfaces:"
  for name in Seerr Jellyfin Radarr Sonarr Prowlarr qBittorrent Whisparr; do
    printf '  %-12s %s\n' "$name" "${URLS[$name]}"
  done

  local lan_ip
  lan_ip="$(lan_ip 2>/dev/null || true)"
  if [[ -n "$lan_ip" ]]; then
    echo
    echo "Jellyfin on LAN:"
    echo "  http://${lan_ip}:8096"
  fi
  echo
}

lan_ip() {
  local ip
  ip="$(
    ip route get 1.1.1.1 2>/dev/null |
      awk '{
        for (i = 1; i <= NF; i++) {
          if ($i == "src") {
            print $(i+1)
            exit
          }
        }
      }'
  )"

  [[ -n "$ip" ]] || return 1
  printf '%s\n' "$ip"
}

remove_scratch() {
  if docker volume inspect "$SCRATCH_VOLUME" >/dev/null 2>&1; then
    echo "Removing disposable volume: $SCRATCH_VOLUME"
    docker volume rm "$SCRATCH_VOLUME" >/dev/null
  fi
}

doctor() {
  header "Media Stack Doctor"

  echo "[1/7] Docker..."
  check_docker
  echo "  OK"

  echo "[2/7] Compose..."
  check_compose
  echo "  OK"

  echo "[3/7] Environment..."
  echo "  UID=$PUID GID=$PGID TZ=$TZ"
  echo "  OK"

  echo "[4/7] Containers..."
  docker compose ps -a

  echo
  echo "[5/7] Scratch volume..."
  if docker volume inspect "$SCRATCH_VOLUME" >/dev/null 2>&1; then
    docker run --rm \
      -v "${SCRATCH_VOLUME}:/data:ro" \
      alpine:latest \
      sh -c 'find /data -maxdepth 3 -type d -print | sort'
  else
    echo "  Scratch volume does not exist yet."
  fi

  echo
  echo "[6/7] Disk..."
  df -h "$ROOT" || true
  docker system df || true

  echo
  echo "[7/7] LAN..."
  local ip
  ip="$(lan_ip 2>/dev/null || true)"
  if [[ -n "$ip" ]]; then
    echo "  LAN IP: $ip"
    echo "  Jellyfin: http://${ip}:8096"
  else
    echo "  Could not determine LAN IP."
  fi

  echo
  echo "Doctor finished."
}

usage() {
  cat <<'EOF'
Usage:
  ./media.sh start
  ./media.sh stop
  ./media.sh destroy
  ./media.sh reset
  ./media.sh status
  ./media.sh urls
  ./media.sh open
  ./media.sh logs [service]
  ./media.sh password
  ./media.sh update
  ./media.sh doctor
  ./media.sh lan-ip

Meaning:
  start     Create scratch storage and start the stack.
  stop      Stop containers but preserve downloaded media.
  destroy   Remove containers/network and DELETE temporary media/downloads.
            App configuration is preserved.
  reset     FULL RESET. Deletes media AND ./config. Requires confirmation.
  status    Show container status.
  urls      Show service URLs.
  open      Open Seerr and Jellyfin in the desktop browser.
  logs      Follow all logs or one service's logs.
  password  Show qBittorrent's generated temporary admin password line.
  update    Pull current images and recreate containers.
  doctor    Run diagnostics.
  lan-ip    Print the current LAN IPv4 address.
EOF
}

load_env
check_docker
check_compose

ACTION="${1:-start}"
SERVICE="${2:-}"

case "$ACTION" in
  start)
    header "Starting Ephemeral Media Stack"
    init_scratch
    docker compose up -d --remove-orphans
    show_urls
    ;;

  stop)
    header "Stopping Media Stack"
    docker compose stop --timeout 30
    echo "Containers stopped. Temporary media was preserved."
    ;;

  destroy)
    header "Destroying Temporary Media Stack"
    docker compose down --remove-orphans --timeout 30
    remove_scratch
    echo
    echo "Deleted:"
    echo "  downloads"
    echo "  movies"
    echo "  TV episodes"
    echo "  adult library media"
    echo
    echo "Preserved:"
    echo "  ./config/*"
    echo "  Docker images"
    ;;

  reset)
    header "FULL RESET"
    echo "This will delete:"
    echo "  - every temporary download/media file"
    echo "  - ALL application configuration under ./config"
    echo "  - Jellyfin users/settings"
    echo "  - Radarr/Sonarr/Prowlarr/qBittorrent/Seerr/Whisparr settings"
    echo
    read -r -p "Type RESET to continue: " confirmation
    [[ "$confirmation" == "RESET" ]] || {
      echo "Cancelled."
      exit 0
    }

    docker compose down --remove-orphans --timeout 30
    remove_scratch

    rm -rf \
      "$ROOT/config/jellyfin" \
      "$ROOT/config/prowlarr" \
      "$ROOT/config/radarr" \
      "$ROOT/config/sonarr" \
      "$ROOT/config/qbittorrent" \
      "$ROOT/config/seerr" \
      "$ROOT/config/whisparr"

    mkdir -p \
      "$ROOT/config/jellyfin" \
      "$ROOT/config/prowlarr" \
      "$ROOT/config/radarr" \
      "$ROOT/config/sonarr" \
      "$ROOT/config/qbittorrent" \
      "$ROOT/config/seerr" \
      "$ROOT/config/whisparr"

    chown -R "${PUID}:${PGID}" "$ROOT/config"
    chmod -R u+rwX,g+rwX "$ROOT/config"

    echo "Full reset complete."
    ;;

  status)
    docker compose ps
    show_urls
    ;;

  urls)
    show_urls
    ;;

  open)
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "${URLS[Seerr]}" >/dev/null 2>&1 &
      xdg-open "${URLS[Jellyfin]}" >/dev/null 2>&1 &
      echo "Opened Seerr and Jellyfin."
    else
      show_urls
    fi
    ;;

  logs)
    if [[ -n "$SERVICE" ]]; then
      docker compose logs --follow --tail 200 "$SERVICE"
    else
      docker compose logs --follow --tail 100
    fi
    ;;

  password)
    header "qBittorrent Initial Login"
    echo "Username: admin"
    echo
    docker compose logs qbittorrent --tail 400 2>&1 |
      grep -iE 'temporary password|password' || {
        echo "Temporary password was not found in recent logs."
        echo "Run: ./media.sh logs qbittorrent"
      }
    ;;

  update)
    header "Updating Media Stack"
    docker compose pull
    init_scratch
    docker compose up -d --remove-orphans
    echo "Update complete."
    ;;

  doctor)
    doctor
    ;;

  lan-ip)
    lan_ip || die "Could not determine LAN IPv4 address."
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    usage
    exit 1
    ;;
esac
```

Then:

```bash
chmod +x media.sh
```

---

# 8. `.gitignore`

Create:

```gitignore
.env
config/
backup/
*.log
```

The Compose file and script may be committed. Config databases, credentials, API keys, cookies, and `.env` should not.

---

# 9. First startup and validation

Run:

```bash
./media.sh doctor
./media.sh start
```

The first run may take a while because images must be pulled.

Then:

```bash
./media.sh status
```

Expected services:

```text
media-prowlarr
media-radarr
media-sonarr
media-qbittorrent
media-jellyfin
media-seerr
media-flaresolverr
media-whisparr
```

All should be `Up`; Seerr should eventually become `healthy`.

Show URLs:

```bash
./media.sh urls
```

Expected host-side interfaces:

```text
Seerr        http://localhost:5055
Jellyfin     http://localhost:8096
Radarr       http://localhost:7878
Sonarr       http://localhost:8989
Prowlarr     http://localhost:9696
qBittorrent  http://localhost:8080
Whisparr     http://localhost:6969
```

Jellyfin LAN URL is:

```bash
echo "http://$(./media.sh lan-ip):8096"
```

---

# 10. Critical Docker networking rule

Inside containers:

```text
localhost = the current container
```

Therefore service-to-service connections MUST use Docker service names:

```text
qBittorrent  http://qbittorrent:8080
Prowlarr     http://prowlarr:9696
Radarr       http://radarr:7878
Sonarr       http://sonarr:8989
Jellyfin     http://jellyfin:8096
Seerr        http://seerr:5055
Whisparr     http://whisparr:6969
FlareSolverr http://flaresolverr:8191
```

The user's browser uses `localhost:<port>`.

This is one of the most common setup mistakes. Do not use `localhost` inside another application's integration settings.

---

# 11. One-time qBittorrent setup

Open:

```text
http://localhost:8080
```

Get the initial credentials:

```bash
./media.sh password
```

Initial username:

```text
admin
```

qBittorrent prints a temporary password in its startup logs. Immediately set a permanent password in:

```text
Tools → Options → Web UI
```

Set the default save path to:

```text
/data/downloads
```

Do not use:

```text
/home/...
~/Downloads
/downloads
```

The Arr services share `/data`, so qBittorrent must use `/data/downloads`.

Recommended connection hygiene for a home laptop:

- Do NOT let qBittorrent use absurd numbers of peer connections.
- A reasonable starting point is:
  - global connections: ~200
  - per torrent: ~50
  - global upload slots: ~8
  - upload slots per torrent: ~4
- If home Internet becomes sluggish, cap qBittorrent:
  - download to roughly 75–80% of measured downstream;
  - upload to roughly 50–60% of measured upstream.
- A torrent at 100% may still be seeding.
- To test whether qBittorrent is hurting the network:

```bash
docker compose stop qbittorrent
```

If the network immediately recovers, tune rate/connection limits.

Do not automatically configure a VPN. If the user wants one later, treat that as a separate task.

---

# 12. Radarr setup

Open:

```text
http://localhost:7878
```

## Root folder

```text
Settings → Media Management → Root Folders
```

Add:

```text
/data/media/movies
```

Enable:

```text
Use Hardlinks instead of Copy
```

Completed Download Handling should be enabled.

## qBittorrent download client

```text
Settings → Download Clients → + → qBittorrent
```

Use:

```text
Host: qbittorrent
Port: 8080
SSL: off
Username: <qBittorrent username>
Password: <qBittorrent password>
Category: radarr
```

Test and save.

Copy Radarr's API key from:

```text
Settings → General → Security
```

---

# 13. Sonarr setup

Open:

```text
http://localhost:8989
```

Root folder:

```text
/data/media/tv
```

Enable hardlinks.

Add qBittorrent:

```text
Host: qbittorrent
Port: 8080
SSL: off
Username: <qBittorrent username>
Password: <qBittorrent password>
Category: sonarr
```

Test and save.

Copy the Sonarr API key from:

```text
Settings → General → Security
```

For anime, use Sonarr's appropriate Anime series type when the show's numbering requires it.

---

# 14. Prowlarr setup

Open:

```text
http://localhost:9696
```

Only add indexers/sources the user is authorized to use.

Do not automatically configure specific piracy-oriented trackers.

## Connect Radarr

```text
Settings → Apps → + → Radarr
```

Use:

```text
Name: Radarr
Sync Level: Full Sync
Prowlarr Server: http://prowlarr:9696
Radarr Server: http://radarr:7878
API Key: <Radarr API key>
```

Test and save.

## Connect Sonarr

```text
Settings → Apps → + → Sonarr
```

Use:

```text
Name: Sonarr
Sync Level: Full Sync
Prowlarr Server: http://prowlarr:9696
Sonarr Server: http://sonarr:8989
API Key: <Sonarr API key>
```

Test and save.

## Optional FlareSolverr proxy

FlareSolverr is already in the Compose stack but has no host port.

If an authorized indexer legitimately requires it and its terms permit this use:

```text
Settings → Indexers → Indexer Proxies → + → FlareSolverr
```

Use:

```text
Host: http://flaresolverr:8191
```

Prefer assigning a tag such as:

```text
cloudflare
```

and attach the same tag only to affected indexers rather than routing all indexers through FlareSolverr.

---

# 15. Whisparr setup

Open:

```text
http://localhost:6969
```

This stack uses Whisparr v3.

If Whisparr asks for allowed hosts, use:

```text
localhost,127.0.0.1,whisparr
```

Root folder:

```text
/data/media/adult
```

Add qBittorrent:

```text
Host: qbittorrent
Port: 8080
SSL: off
Username: <qBittorrent username>
Password: <qBittorrent password>
Category: whisparr
```

Test and save.

Copy Whisparr's API key.

In Prowlarr:

```text
Settings → Apps → + → Whisparr
```

Use:

```text
Prowlarr Server: http://prowlarr:9696
Whisparr Server: http://whisparr:6969
API Key: <Whisparr API key>
Sync Level: Full Sync
```

Use Prowlarr tags if adult-specific indexers should sync only to Whisparr.

Seerr is not the frontend for Whisparr. Whisparr manages its own library.

---

# 16. Jellyfin setup

Open:

```text
http://localhost:8096
```

Complete the first-run wizard and create the main admin user.

Create libraries:

## Movies

```text
Name: Movies
Content type: Movies
Folder: /data/media/movies
```

## TV

```text
Name: TV Shows
Content type: Shows
Folder: /data/media/tv
```

## Adult library, if wanted

```text
Name: Adult
Folder: /data/media/adult
```

Keep it separate from the normal Movies library.

Enable real-time monitoring for each library.

If newly imported media does not appear promptly, configure a periodic library scan as a fallback. Manual rescanning should not be required during normal operation.

## Restricted TV user

A useful setup is:

```text
Admin user:
  Movies ✅
  TV Shows ✅
  Adult ✅

tv user:
  Movies ✅
  TV Shows ✅
  Adult ❌
```

In Jellyfin's user settings, restrict the `tv` user to only the non-adult libraries.

## LAN / TV access

The server is published on port `8096`.

Print the current LAN address:

```bash
./media.sh lan-ip
```

If it prints, for example:

```text
192.168.1.67
```

then the TV/phone server URL is:

```text
http://192.168.1.67:8096
```

The TV and laptop must be on the same LAN/Wi-Fi.

Do not configure router port forwarding merely for TV access.

In Jellyfin's Networking settings:

- HTTP: 8096
- HTTPS: off unless explicitly configured later
- local IP override: blank
- LAN networks: blank is normally fine because RFC1918 private ranges are treated as local
- allow remote connections: enabled if clients require it
- remote IP filter: blank
- IPv4: enabled
- auto discovery: enabled

If a LAN client can load the login page but authentication fails while `localhost` login works, inspect Jellyfin logs before changing firewall settings:

```bash
docker logs media-jellyfin --since 5m
```

---

# 17. Seerr setup

Open:

```text
http://localhost:5055
```

Connect it to Jellyfin using the Docker-internal URL:

```text
http://jellyfin:8096
```

Use:

```text
http://localhost:8096
```

only as an external/browser URL if Seerr asks for one.

Add Radarr:

```text
Hostname/IP: radarr
Port: 7878
SSL: no
API Key: <Radarr API key>
Root Folder: /data/media/movies
Default Server: yes
Automatic Search: yes
Scan: yes
```

Add Sonarr:

```text
Hostname/IP: sonarr
Port: 8989
SSL: no
API Key: <Sonarr API key>
Root Folder: /data/media/tv
Default Server: yes
Automatic Search: yes
Scan: yes
```

Normal day-to-day workflow:

```text
Seerr → search/request
      → Radarr/Sonarr
      → Prowlarr
      → qBittorrent
      → import into /data/media
      → Jellyfin
      → play
```

The user should normally need only:

```text
Seerr     → discover/request
Jellyfin  → watch
```

Radarr/Sonarr/Prowlarr/qBittorrent are backend/admin interfaces.

---

# 18. End-to-end verification

After the UI configuration is complete, verify using authorized/test content.

The expected sequence is:

```text
Seerr request
    ↓
Radarr or Sonarr receives item
    ↓
Prowlarr-backed search returns a valid release/source
    ↓
qBittorrent starts transfer
    ↓
qBittorrent reaches 100%
    ↓
Radarr/Sonarr imports or hardlinks the file
    ↓
file appears under /data/media/movies or /data/media/tv
    ↓
Jellyfin detects it
    ↓
content appears in Jellyfin
```

Useful diagnostics:

```bash
docker exec media-radarr \
  sh -c "find /data/downloads -maxdepth 3 -type f | head -30"

docker exec media-radarr \
  sh -c "find /data/media/movies -maxdepth 3 -type f | head -30"

docker exec media-sonarr \
  sh -c "find /data/media/tv -maxdepth 3 -type f | head -30"

docker exec media-jellyfin \
  sh -c "find /data/media -maxdepth 3 -type f | head -50"
```

Interpretation:

```text
file in /data/downloads but not /data/media
    → Arr import problem

file in /data/media and Jellyfin container can see it
    → Jellyfin scan/library problem

file not visible to Arr at all
    → qBittorrent save path problem
```

Radarr/Sonarr are not playback interfaces. Playback belongs in Jellyfin.

---

# 19. Lifecycle commands

## Start

```bash
./media.sh start
```

Creates/initializes disposable storage and starts everything.

## Stop without deleting media

```bash
./media.sh stop
```

## Status

```bash
./media.sh status
```

## Logs

All:

```bash
./media.sh logs
```

One service:

```bash
./media.sh logs jellyfin
./media.sh logs radarr
./media.sh logs qbittorrent
```

## Destroy downloaded media but preserve configuration

```bash
./media.sh destroy
```

This MUST remove:

```text
/data/downloads
/data/media/movies
/data/media/tv
/data/media/adult
```

by removing the entire `media-stack-scratch` named volume.

It MUST preserve:

```text
./config/jellyfin
./config/prowlarr
./config/radarr
./config/sonarr
./config/qbittorrent
./config/seerr
./config/whisparr
```

## Full reset

```bash
./media.sh reset
```

This is destructive and requires typing `RESET`.

## Update images

```bash
./media.sh update
```

Do not add automatic Watchtower updates.

---

# 20. Troubleshooting quick reference

## qBittorrent finished, Jellyfin shows nothing

Check Radarr/Sonarr History and import status.

Then:

```bash
docker exec media-radarr \
  sh -c "find /data/downloads -maxdepth 3 -type f"

docker exec media-radarr \
  sh -c "find /data/media/movies -maxdepth 3 -type f"
```

If imported media exists, trigger a Jellyfin scan once and ensure real-time monitoring is enabled.

---

## Queue is empty after download

That can be normal if import completed successfully. Check History and the media folder.

---

## Service integration test fails

Remember:

```text
Wrong inside Docker:
http://localhost:7878

Correct from another container:
http://radarr:7878
```

Same principle applies to every service.

---

## qBittorrent WebUI password unknown

```bash
./media.sh password
```

or:

```bash
docker compose logs qbittorrent --tail 300
```

---

## Home Wi-Fi suddenly becomes slow

Test:

```bash
docker compose stop qbittorrent
```

If the network improves, tune qBittorrent's speed and peer connection limits.

Even at `0 B/s` upload, very high peer counts can burden weaker home routers.

---

## TV cannot reach Jellyfin

Verify Jellyfin is listening:

```bash
docker compose ps jellyfin
ss -ltn | grep ':8096'
```

Get LAN IP:

```bash
./media.sh lan-ip
```

From another LAN device:

```text
http://<LAN_IP>:8096
```

If `localhost:8096` works but LAN does not:

1. verify the host firewall;
2. allow TCP 8096 only for the local network if necessary;
3. allow UDP 7359 if discovery is wanted;
4. do not modify router/NAT settings.

The agent should ask before making firewall changes requiring root.

---

## Jellyfin library UI throws a JavaScript null metadata error

Try a private/incognito browser window first.

If the error occurs only for one library, that library's metadata options may be malformed. Recreate the Jellyfin library entry pointing at the same `/data/media/...` path; do not delete the underlying media merely to repair Jellyfin metadata settings.

---

# 21. Optional hardware transcoding — do NOT enable initially

Get the basic stack working first.

On Omarchy/Linux, hardware acceleration can be added later.

Before changing Compose, detect hardware:

```bash
lspci | grep -Ei 'vga|3d|display'
ls -l /dev/dri 2>/dev/null || true
nvidia-smi 2>/dev/null || true
```

Possible paths:

- Intel/AMD: pass `/dev/dri` to Jellyfin and ensure container permissions are correct.
- NVIDIA: install/configure NVIDIA Container Toolkit and then give Jellyfin GPU access.

Do not add GPU configuration blindly. Direct Play requires little server processing and is preferable when the TV/client supports the media codec.

---

# 22. Optional: make Seerr reachable from phone

Default:

```yaml
- "127.0.0.1:5055:5055"
```

If the user explicitly wants to request content from a phone on the home LAN, it may be changed to:

```yaml
- "5055:5055"
```

Do not make this change automatically.

Radarr, Sonarr, Prowlarr, qBittorrent WebUI, Whisparr, and FlareSolverr should remain host-only/internal unless the user explicitly requests otherwise.

---

# 23. Final acceptance checklist for the agent

Before saying setup is complete, verify:

```text
[ ] Docker Engine works without global insecure permissions.
[ ] `docker compose config --quiet` passes.
[ ] `.env` contains detected PUID, PGID, TZ.
[ ] Config directories exist and are writable by the detected UID/GID.
[ ] `media.sh` is executable.
[ ] `./media.sh start` succeeds.
[ ] `docker compose ps` shows all expected services.
[ ] Seerr becomes healthy.
[ ] qBittorrent WebUI opens on localhost:8080.
[ ] Prowlarr opens on localhost:9696.
[ ] Radarr opens on localhost:7878.
[ ] Sonarr opens on localhost:8989.
[ ] Jellyfin opens on localhost:8096.
[ ] Whisparr opens on localhost:6969.
[ ] `./media.sh lan-ip` returns the current LAN IPv4.
[ ] Jellyfin responds on http://<LAN_IP>:8096 from the host.
[ ] scratch volume contains:
      /data/downloads
      /data/media/movies
      /data/media/tv
      /data/media/adult
[ ] `./media.sh destroy` removes ONLY media-stack-scratch and containers/network,
    while preserving ./config.
```

After infrastructure validation, print a concise message telling the user to perform the one-time UI integrations from sections 11–17.

---

# 24. What NOT to add unless explicitly requested

Do not silently add:

- Watchtower
- Dozzle
- Profilarr
- Qui
- NewtArr
- VPN containers
- reverse proxies
- public domains
- Cloudflare Tunnel
- router port forwarding
- network-wide DNS modifications
- Samba/NFS
- Kubernetes
- automatic indexer lists
- automatic firewall changes
- global Docker pruning

Keep the baseline stack understandable and disposable.

---

# 25. Expected finished user experience

Starting:

```bash
./media.sh start
```

Normal use:

```text
Seerr → request
Jellyfin → watch
```

Stopping for later without deleting media:

```bash
./media.sh stop
```

Completely discarding the temporary library:

```bash
./media.sh destroy
```

Starting again later:

```bash
./media.sh start
```

All application setup survives, while `/data/downloads` and all media libraries start empty again.

That is the intended system.

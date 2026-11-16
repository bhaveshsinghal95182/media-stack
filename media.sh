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
  [Library]="http://localhost:8099"
  [Bazarr]="http://localhost:6767"
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
      && chown -R ${PUID}:${PGID} /data \
      && chmod -R 775 /data"

  echo "Disposable filesystem ready."
}

# Bind-mount folders must exist and belong to the user before compose starts:
# Docker creates missing ones as root, and subgate/library run as PUID.
init_config() {
  mkdir -p "$ROOT"/config/{jellyfin,prowlarr,radarr,sonarr,bazarr,recyclarr,qbittorrent,seerr,subgate,library}
}

show_urls() {
  echo
  echo "Local interfaces:"
  for name in Seerr Jellyfin Library Radarr Sonarr Prowlarr Bazarr qBittorrent; do
    printf '  %-12s %s\n' "$name" "${URLS[$name]}"
  done

  local lan_ip
  lan_ip="$(lan_ip 2>/dev/null || true)"
  if [[ -n "$lan_ip" ]]; then
    echo
    echo "On LAN (TV / phone):"
    echo "  Jellyfin     http://${lan_ip}:8096"
    echo "  Seerr        http://${lan_ip}:5055"
    echo "  Library      http://${lan_ip}:8099"
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
  ./media.sh list
  ./media.sh add movie|show <title | tmdb:ID | tvdb:ID> [--year YEAR] [--yes]
  ./media.sh delete movie|show <title | tmdb:ID | tvdb:ID> [--yes] [--exclude]
  ./media.sh orphans [--delete] [--yes]
  ./media.sh import movie|show <local file or folder> [--title T] [--year Y] [--replace] [--yes]

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
  list      Movies/shows with size on disk and torrent state; flags orphans.
  add       Add a movie (Radarr) or show (Sonarr) and search for it now.
  delete    Delete a movie/show completely: Arr entry + library files,
            the torrent AND its data (imports are hardlinks), Seerr request.
            Verifies the files are gone.
  orphans   Torrents not linked to any title; --delete removes them + data.
  import    Copy a local movie file/folder or episodes into the library via
            Radarr/Sonarr (unmonitored, never re-downloaded); Jellyfin sees it.
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
    init_config
    init_scratch
    docker compose up -d --build --remove-orphans
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
    echo "  - Radarr/Sonarr/Prowlarr/Bazarr/Recyclarr/qBittorrent/Seerr settings"
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
      "$ROOT/config/bazarr" \
      "$ROOT/config/recyclarr" \
      "$ROOT/config/qbittorrent" \
      "$ROOT/config/seerr"

    mkdir -p \
      "$ROOT/config/jellyfin" \
      "$ROOT/config/prowlarr" \
      "$ROOT/config/radarr" \
      "$ROOT/config/sonarr" \
      "$ROOT/config/bazarr" \
      "$ROOT/config/recyclarr" \
      "$ROOT/config/qbittorrent" \
      "$ROOT/config/seerr"

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
    docker compose pull --ignore-buildable
    docker compose build --pull subgate library
    init_config
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

  list|add|delete|orphans|import)
    need python3
    exec python3 "$ROOT/library/cli.py" "$@"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    usage
    exit 1
    ;;
esac

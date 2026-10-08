#!/usr/bin/env bash
# One-time root step so Jellyfin can use the NVIDIA GPU (NVENC) on AC power.
# Installs the NVIDIA container runtime and registers it with Docker.
# Restarting Docker restarts every container (AdGuard DNS drops for a few seconds).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root: bash $0" >&2; exit 1; }
pacman -S --needed nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
docker info 2>/dev/null | grep -i 'runtimes'
echo "Done. Next (as your user): add the nvidia lines to the jellyfin service and set JELLYFIN_AC_HWACCEL=nvenc (see SETUP.md 9a)."

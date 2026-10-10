"""Domain logic for the media library: Radarr/Sonarr/qBittorrent/Jellyfin/Seerr glue.

Standard library only, so the host CLI (`./media.sh list|add|...`) runs with the system
Python. The web app in `web/` is a thin layer over these modules.

Runs on the host (talks to localhost ports, touches the volume via `docker exec`) or
inside the media-library container (LIBRARY_LOCAL_DATA=1: service hostnames, volume
mounted at /data).
"""

from medialib.errors import LibraryError
from medialib.stack import Kind, Stack

__all__ = ["Kind", "LibraryError", "Stack"]

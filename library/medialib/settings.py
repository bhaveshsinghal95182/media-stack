"""Service URLs and credentials, from the environment or the stack's .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from medialib.errors import LibraryError

STACK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REQUIRED = ("QBIT_USERNAME", "QBIT_PASSWORD", "RADARR_API_KEY", "SONARR_API_KEY")


def _read_env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = os.path.join(STACK_ROOT, ".env")
    if os.path.exists(dotenv):
        with open(dotenv, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env.setdefault(key.strip(), value.strip())
    return env


def _url(env: dict[str, str], key: str, default: str) -> str:
    return env.get(key, default).rstrip("/")


@dataclass(frozen=True)
class Settings:
    local_data: bool        # inside the container, with the volume mounted at /data
    radarr_url: str
    radarr_key: str
    sonarr_url: str
    sonarr_key: str
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    seerr_url: str
    seerr_settings_path: str
    jellyfin_url: str
    jellyfin_key: str       # empty: reuse Seerr's key
    tv_user: str            # the Jellyfin account "Hide on TV" blocks

    @classmethod
    def load(cls) -> Settings:
        env = _read_env()
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise LibraryError(f"{', '.join(missing)} not set (.env)")
        return cls(
            local_data=env.get("LIBRARY_LOCAL_DATA") == "1",
            radarr_url=_url(env, "RADARR_URL", "http://localhost:7878"),
            radarr_key=env["RADARR_API_KEY"],
            sonarr_url=_url(env, "SONARR_URL", "http://localhost:8989"),
            sonarr_key=env["SONARR_API_KEY"],
            qbit_url=_url(env, "QBIT_URL", "http://localhost:8080"),
            qbit_user=env["QBIT_USERNAME"],
            qbit_pass=env["QBIT_PASSWORD"],
            seerr_url=_url(env, "SEERR_URL", "http://localhost:5055"),
            seerr_settings_path=env.get(
                "SEERR_SETTINGS", os.path.join(STACK_ROOT, "config/seerr/settings.json")),
            jellyfin_url=_url(env, "JELLYFIN_URL", "http://localhost:8096"),
            jellyfin_key=env.get("JELLYFIN_API_KEY", ""),
            tv_user=env.get("TV_USER", "tv"))

"""The media volume: mounted locally (container) or reached through `docker exec` (host)."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable

from medialib.errors import LibraryError

CONTAINER = {"movie": "media-radarr", "show": "media-sonarr"}
COPY_CHUNK = 8 * 1024 * 1024

Progress = Callable[[int, int], None]  # (bytes done, bytes total)


class Volume:
    def __init__(self, local: bool):
        self.local = local

    def _docker(self, kind: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["docker", "exec", "-u", "abc", CONTAINER[kind], *cmd],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise LibraryError(f"{cmd[0]} in {CONTAINER[kind]} failed: {r.stderr.strip()}")
        return r

    def exists(self, path: str) -> bool:
        if self.local:
            return os.path.exists(path)
        return self._docker("movie", "test", "-e", path, check=False).returncode == 0

    def link_counts(self, paths: list[str]) -> list[int] | None:
        """Hardlink count of each file, or None if any can't be read."""
        if not paths:
            return None
        if self.local:
            try:
                return [os.stat(p).st_nlink for p in paths]
            except OSError:
                return None
        r = self._docker("movie", "stat", "-c", "%h", *paths, check=False)
        out = r.stdout.split()
        return [int(n) for n in out] if r.returncode == 0 and len(out) == len(paths) else None

    def copy_in(self, kind: str, src: str, dest: str, progress: Progress | None = None) -> None:
        """Copy a file into the volume and verify its size."""
        size = os.path.getsize(src)
        got = self._copy_local(src, dest, progress) if self.local \
            else self._copy_docker(kind, src, dest)
        if progress:
            progress(size, size)
        if got != size:
            raise LibraryError(f"size mismatch after copying {src} ({got} of {size} bytes)")

    @staticmethod
    def _copy_local(src: str, dest: str, progress: Progress | None) -> int:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size, done, tmp = os.path.getsize(src), 0, dest + ".partial"
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(COPY_CHUNK):
                fo.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size)
        shutil.copystat(src, tmp)
        os.replace(tmp, dest)
        return os.path.getsize(dest)

    def _copy_docker(self, kind: str, src: str, dest: str) -> int:
        self._docker(kind, "mkdir", "-p", os.path.dirname(dest))
        # -a keeps the owner: host uid 1000 == PUID, so the Arrs can manage it.
        r = subprocess.run(["docker", "cp", "-a", src, f"{CONTAINER[kind]}:{dest}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise LibraryError(f"docker cp failed for {src}: {r.stderr.strip()}")
        return int(self._docker(kind, "stat", "-c", "%s", dest).stdout.strip())

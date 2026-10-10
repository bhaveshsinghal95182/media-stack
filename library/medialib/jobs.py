"""Long-running work (delete, import, follow…) run one at a time in the background."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from medialib.errors import LibraryError
from medialib.fmt import human

KEEP_FOR = 6 * 3600  # finished jobs are forgotten after this long


class Outcome(NamedTuple):
    ok: bool
    message: str


Log = Callable[[str], None]
Progress = Callable[[int, int], None]
Work = Callable[[Log, Progress], Outcome]


@dataclass
class Job:
    id: str
    title: str
    refresh: str | None  # page event to fire when it finishes, e.g. "library-changed"
    status: str = "queued"  # queued -> running -> done | error
    log: list[str] = field(default_factory=list)
    progress: tuple[int, int] | None = None  # (bytes done, bytes total)
    outcome: Outcome | None = None
    started: float = field(default_factory=time.time)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "error")

    @property
    def progress_text(self) -> str:
        return f"{human(self.progress[0])} / {human(self.progress[1])}" if self.progress else ""


class JobRunner:
    """Starts jobs on threads; `lock` keeps them (and the janitor) from overlapping."""

    def __init__(self, lock: threading.Lock, scrub: Callable[[str], str] = str):
        self.lock, self.scrub = lock, scrub
        self._jobs: dict[str, Job] = {}

    def start(self, title: str, work: Work, refresh: str | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], title=title, refresh=refresh)
        self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def _run(self, job: Job, work: Work) -> None:
        def log(msg: str) -> None:
            job.log.append(self.scrub(msg))

        def progress(done: int, total: int) -> None:
            job.progress = (done, total)

        with self.lock:
            job.status = "running"
            try:
                ok, message = work(log, progress)
                job.outcome, job.status = Outcome(ok, self.scrub(message)), "done"
            except LibraryError as e:
                job.outcome, job.status = Outcome(False, self.scrub(str(e))), "error"
            except Exception as e:  # noqa: BLE001 - keep the server alive, show the failure
                traceback.print_exc()
                job.outcome, job.status = Outcome(False, f"{type(e).__name__}: {e}"), "error"
        self._forget_old()

    def _forget_old(self) -> None:
        cutoff = time.time() - KEEP_FOR
        for job_id in [j.id for j in self._jobs.values() if j.started < cutoff]:
            self._jobs.pop(job_id, None)

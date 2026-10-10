"""Run a periodic task on a daemon thread."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


def run_forever(task: Callable[[], None], interval: float,
                on_error: Callable[[Exception], None]) -> threading.Thread:
    """Call `task` every `interval` seconds; a failure is reported and the loop goes on."""
    def loop() -> None:
        while True:
            try:
                task()
            except Exception as e:  # noqa: BLE001 - a background service must keep running
                on_error(e)
            time.sleep(interval)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

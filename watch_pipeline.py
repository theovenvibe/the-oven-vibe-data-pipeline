"""Watch data/ for new/changed order-history CSVs or menu.csv and rebuild.

Debounces bursts (e.g. extracting many files from a zip at once) so the
bronze -> silver -> menu -> gold pipeline runs once per batch, not once per
file.

Usage:
    uv run python watch_pipeline.py
"""

import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from main import main as run_pipeline

ROOT_DIR = Path(__file__).parent
WATCH_DIR = ROOT_DIR / "data"
DEBOUNCE_SECONDS = 3


class RebuildOnChange(FileSystemEventHandler):
    def __init__(self):
        self._pending = False
        self._last_event = 0.0

    def on_any_event(self, event):
        if event.is_directory or not event.src_path.endswith(".csv"):
            return
        self._pending = True
        self._last_event = time.monotonic()

    def poll(self):
        if self._pending and (time.monotonic() - self._last_event) >= DEBOUNCE_SECONDS:
            self._pending = False
            print(f"[watch] change detected, rebuilding warehouse...")
            try:
                run_pipeline()
            except Exception as e:
                print(f"[watch] pipeline run failed: {e}")
            print("[watch] watching for more changes...")


def main():
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    handler = RebuildOnChange()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=True)
    observer.start()
    print(f"[watch] watching {WATCH_DIR} for new/changed CSVs (ctrl-c to stop)")
    try:
        while True:
            time.sleep(1)
            handler.poll()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()

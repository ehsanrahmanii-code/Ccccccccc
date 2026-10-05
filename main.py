"""TITAN Android entry point with safe startup."""
from __future__ import annotations
import os
import threading
import time
import traceback

__version__ = "45.0.4"
os.environ.setdefault("TITAN_PORT", "8080")

LOG = os.path.join(os.environ.get("ANDROID_PRIVATE", "/tmp"), "titan_startup.log")


def log_error(exc):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
            f.write("\n" + repr(exc) + "\n")
    except Exception:
        pass


# Import errors must not kill the Android activity silently.
try:
    import TITAN_V45_COHERENT as titan
except Exception as e:
    log_error(e)
    titan = None


def _run():
    if titan is None:
        return
    try:
        titan.run_titan(open_browser=False)
    except Exception as e:
        log_error(e)


def main():
    # Start engine after launcher has a chance to stay alive.
    worker = threading.Thread(target=_run, name="titan-engine", daemon=True)
    worker.start()
    while True:
        time.sleep(5)


if __name__ == "__main__":
    main()

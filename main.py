"""TITAN Android entry point with crash protection."""
from __future__ import annotations
import os
import sys
import threading
import time
import traceback

__version__ = "45.0.2"
os.environ.setdefault("TITAN_PORT", "8080")

LOG = os.path.join(os.environ.get("ANDROID_PRIVATE", "/tmp"), "titan_startup.log")

def log_error(exc):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
            f.write("\n" + str(exc) + "\n")
    except Exception:
        pass

try:
    _android_private = (os.environ.get("ANDROID_PRIVATE") or "").strip()
    if _android_private:
        os.environ.setdefault("TITAN_HOME", os.path.join(_android_private, "titan"))
    import TITAN_V45_COHERENT as titan
except Exception as e:
    log_error(e)
    raise

def _run():
    try:
        titan.run_titan(open_browser=False)
    except Exception as e:
        log_error(e)
        raise

def main():
    try:
        worker = threading.Thread(target=_run, name="titan-main", daemon=True)
        worker.start()
        while worker.is_alive():
            time.sleep(1)
    except Exception as e:
        log_error(e)
        raise

if __name__ == "__main__":
    main()

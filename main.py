"""TITAN Android WebView entry point."""
from __future__ import annotations
import os
import threading
import time
__version__ = "45.0.1"
os.environ.setdefault("TITAN_PORT", "8080")
_android_private = (os.environ.get("ANDROID_PRIVATE") or "").strip()
if _android_private:
    os.environ.setdefault("TITAN_HOME", os.path.join(_android_private, "titan"))
import TITAN_V45_COHERENT as titan

def _run() -> None:
    titan.run_titan(open_browser=False)

def main() -> None:
    worker = threading.Thread(target=_run, name="titan-android-main", daemon=True)
    worker.start()
    while worker.is_alive():
        time.sleep(1.0)

if __name__ == "__main__":
    main()

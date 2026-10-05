"""TITAN Android entry point — fail-open WebView startup."""
from __future__ import annotations
import os
import threading
import time
import traceback

__version__ = "60.0.2"
PORT = int(os.environ.get("TITAN_PORT", "8080") or "8080")
LOG = "/storage/emulated/0/TAITAN/data/titan_startup.log"


def log_error(exc):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
            f.write("\n" + repr(exc) + "\n")
    except Exception:
        pass


try:
    import TITAN_V45_COHERENT as titan
except Exception as e:
    titan = None
    log_error(e)


def _engine():
    if titan is None:
        return
    try:
        titan.run_titan(open_browser=False)
    except Exception as e:
        log_error(e)


def _fallback_server():
    """Never leave the Android WebView on an endless native Loading screen."""
    try:
        from flask import Flask
        app = Flask("titan_startup_error")

        @app.get("/")
        def error_page():
            return """<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<title>TITAN startup</title>
<body style="background:#050812;color:#eef2ff;font-family:Tahoma;padding:28px">
<h2 style="color:#38bdf8">⚡ TITAN AI</h2>
<p>موتور TITAN در شروع برنامه متوقف شد.</p>
<p>فایل خطا:</p><code>/storage/emulated/0/TAITAN/data/titan_startup.log</code>
<p style="opacity:.75">برنامه دیگر روی Loading بی‌نهایت قفل نمی‌شود.</p>
</body></html>"""
        app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)
    except Exception as e:
        log_error(e)
        while True:
            time.sleep(5)


def main():
    if titan is None:
        _fallback_server()
        return
    # run_titan now opens Flask immediately and performs heavy engine startup
    # in the background, so python-for-android WebView can connect at once.
    _engine()


if __name__ == "__main__":
    main()

"""TITAN Android entry point — storage-permission aware, fail-open startup."""
from __future__ import annotations
import os
import time
import traceback

__version__ = "60.0.3"
PORT = int(os.environ.get("TITAN_PORT", "8080") or "8080")
ROOT = "/storage/emulated/0/TAITAN"
LOG = ROOT + "/data/titan_startup.log"


def log_error(exc):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
            f.write("\n" + repr(exc) + "\n")
    except Exception:
        pass


def _android_storage_ready():
    try:
        from jnius import autoclass
        Environment = autoclass("android.os.Environment")
        if hasattr(Environment, "isExternalStorageManager"):
            return bool(Environment.isExternalStorageManager())
    except Exception:
        pass
    try:
        os.makedirs(ROOT, exist_ok=True)
        for name in ("data", "cache", "memory", "secrets"):
            os.makedirs(os.path.join(ROOT, name), exist_ok=True)
        p = os.path.join(ROOT, "data", ".titan_probe")
        with open(p, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(p)
        return True
    except Exception:
        return False


def _open_storage_settings():
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        activity = PythonActivity.mActivity
        package_name = activity.getPackageName()
        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        intent.setData(Uri.parse("package:" + package_name))
        activity.startActivity(intent)
        return True
    except Exception as e:
        log_error(e)
        return False


def _prepare_storage():
    if _android_storage_ready():
        return True
    _open_storage_settings()
    deadline = time.time() + 180
    while time.time() < deadline:
        if _android_storage_ready():
            try:
                for name in ("data", "cache", "memory", "secrets"):
                    os.makedirs(os.path.join(ROOT, name), exist_ok=True)
            except Exception as e:
                log_error(e)
                return False
            return True
        time.sleep(1.5)
    return False


def _fallback_server(message):
    try:
        from flask import Flask
        app = Flask("titan_startup_error")

        @app.get("/")
        def error_page():
            return f"""<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<title>TITAN startup</title>
<body style="background:#050812;color:#eef2ff;font-family:Tahoma;padding:28px">
<h2 style="color:#38bdf8">⚡ TITAN AI</h2>
<p>{message}</p>
<p>پوشه موردنیاز: <b>/storage/emulated/0/TAITAN</b></p>
<p>مجوز «مدیریت همه فایل‌ها» برای TITAN را فعال کنید و به برنامه برگردید.</p>
<p style="opacity:.75">گزارش: /storage/emulated/0/TAITAN/data/titan_startup.log</p>
</body></html>"""
        app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)
    except Exception as e:
        log_error(e)
        while True:
            time.sleep(5)


def _load_engine():
    global titan
    try:
        import TITAN_V45_COHERENT as titan_module
        titan = titan_module
        return True
    except Exception as e:
        titan = None
        log_error(e)
        return False


titan = None


def _engine():
    try:
        titan.run_titan(open_browser=False)
    except Exception as e:
        log_error(e)


def main():
    if not _prepare_storage():
        _fallback_server("برای ذخیره‌سازی مستقیم در پوشه TAITAN، مجوز حافظه لازم است.")
        return
    if not _load_engine():
        _fallback_server("موتور TITAN در شروع برنامه متوقف شد. گزارش خطا در پوشه TAITAN ذخیره شده است.")
        return
    _engine()


if __name__ == "__main__":
    main()

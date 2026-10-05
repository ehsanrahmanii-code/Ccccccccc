"""TITAN Android entry point — start WebView immediately, then unlock storage and engine."""
from __future__ import annotations
import os, time, threading, traceback

__version__ = "60.0.5"
PORT = int(os.environ.get("TITAN_PORT", "8080") or "8080")
ROOT = "/storage/emulated/0/TAITAN"
LOG = ROOT + "/data/titan_startup.log"

def log_error(exc):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f); f.write("\n"+repr(exc)+"\n")
    except Exception: pass

def storage_ready():
    try:
        from jnius import autoclass
        E=autoclass("android.os.Environment")
        if hasattr(E,"isExternalStorageManager") and not E.isExternalStorageManager():
            return False
    except Exception: pass
    try:
        os.makedirs(ROOT,exist_ok=True)
        for n in ("data","cache","memory","secrets"): os.makedirs(os.path.join(ROOT,n),exist_ok=True)
        p=os.path.join(ROOT,"data",".titan_probe")
        with open(p,"w",encoding="utf-8") as f: f.write("ok")
        os.remove(p); return True
    except Exception: return False

def open_storage_settings():
    try:
        from jnius import autoclass
        A=autoclass("org.kivy.android.PythonActivity").mActivity
        I=autoclass("android.content.Intent")
        S=autoclass("android.provider.Settings")
        U=autoclass("android.net.Uri")
        i=I(S.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        i.setData(U.parse("package:"+A.getPackageName())); A.startActivity(i); return True
    except Exception as e: log_error(e); return False

STATE={"ready":False,"message":"در حال آماده‌سازی TITAN…","app":None}
def startup():
    global STATE
    if not storage_ready(): open_storage_settings()
    for _ in range(300):
        if storage_ready(): break
        STATE["message"]="مجوز «مدیریت همه فایل‌ها» را برای TITAN فعال کنید؛ سپس به برنامه برگردید."
        time.sleep(1)
    else:
        STATE["message"]="مجوز حافظه داده نشد. از تنظیمات Android دسترسی TITAN را فعال کنید."
        return
    try:
        import TITAN_V45_COHERENT as titan
        titan.run_titan(open_browser=False, serve=False)
        STATE["app"]=titan.app; STATE["ready"]=True
        STATE["message"]="TITAN آماده است."
    except Exception as e:
        log_error(e); STATE["message"]="خطای راه‌اندازی TITAN؛ گزارش در TAITAN/data/titan_startup.log"

def make_startup_app():
    from flask import Flask, jsonify
    app=Flask("titan_boot")
    @app.get("/startup-status")
    def status(): return jsonify({"ready":STATE["ready"],"message":STATE["message"]})
    @app.get("/")
    def index():
        return """<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN AI</title><style>body{margin:0;background:#050812;color:#eef2ff;font-family:Tahoma,Arial;padding:28px;text-align:center}h2{color:#38bdf8} .box{margin:12vh auto;max-width:520px;padding:28px;border:1px solid #23304d;border-radius:20px;background:#0b1222} </style>
<div class="box"><h2>⚡ TITAN AI V60</h2><p id="m">در حال راه‌اندازی موتور…</p><p style="opacity:.7">/storage/emulated/0/TAITAN</p></div>
<script>
async function p(){try{let r=await fetch('/startup-status');let j=await r.json();document.getElementById('m').textContent=j.message;if(j.ready) location.reload()}catch(e){}setTimeout(p,1200)}p()
</script></body></html>"""
    @app.route("/",defaults={"path":""},methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"])
    def root2(path): return index()
    return app

def dispatch_titan(boot_app):
    from werkzeug.wrappers import Response
    def wsgi(environ,start_response):
        target=STATE.get("app") or boot_app
        return target(environ,start_response)
    return wsgi

def main():
    boot=make_startup_app()
    threading.Thread(target=startup,name="titan-bootstrap",daemon=True).start()
    from werkzeug.serving import run_simple
    run_simple("127.0.0.1",PORT,dispatch_titan(boot),threaded=True,use_reloader=False)
if __name__=="__main__": main()

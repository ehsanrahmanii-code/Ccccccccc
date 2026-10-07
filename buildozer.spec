[app]
title = TITAN AI V60 FINAL
package.name = titanai
package.domain = ai.titan
source.dir = .
source.include_exts = py,json,txt,html,css,js,png,jpg,jpeg,ico,db
source.exclude_dirs = .git,.github,.buildozer,bin,__pycache__
version = 60.1.0
requirements = python3,flask,numpy,pandas,requests,websocket-client
orientation = portrait
fullscreen = 0
p4a.bootstrap = webview
p4a.branch = v2024.01.21
p4a.port = 8080
android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE
android.debug_artifact = apk
android.release_artifact = apk
android.accept_sdk_license = True
android.enable_androidx = True
android.no-byte-compile-python = True

[buildozer]
log_level = 2
warn_on_root = 0
bin_dir = ./bin

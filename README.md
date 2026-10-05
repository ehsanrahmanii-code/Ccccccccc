# TITAN AI V45 — Android APK

This project packages the supplied `TITAN_V45_COHERENT.py` as an Android app using **python-for-android's WebView bootstrap** and **Buildozer**.

The original TITAN Flask dashboard remains the UI. Android starts the Flask server locally and the WebView bootstrap displays it on-device.

## Build
`buildozer -v android debug`

The APK is written to `bin/`.

## GitHub Actions
Push this repository and run **Actions → Build TITAN Android APK**. The workflow builds a debug APK and uploads it as the `TITAN-AI-V45-APK` artifact.

## API keys
Do not commit API keys. The TITAN engine reads keys from its `secrets/` directory at runtime.

## Android storage
The launcher uses `ANDROID_PRIVATE`/`TITAN_HOME` so SQLite, cache, logs and memory stay in an app-private writable directory.

The supplied engine is analysis-only; this packaging does not add order execution.


<!-- Android build trigger: 2026-10-05 -->

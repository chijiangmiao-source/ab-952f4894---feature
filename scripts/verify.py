#!/usr/bin/env python3
"""Entrypoint for the one-shot Compose ``verify`` service.

Runs, in order:
  1. build check        - byte-compile all sources and import the app
  2. code tests         - pytest (reverse-order compensation, revision
                          fences, stale-snapshot rejection and post-restart
                          receipt reconciliation included)
  3. HTTP smoke         - live checks against the ``web`` service (stale
                          snapshot fence, compensation fence, web console
                          revision UI)

The process exits non-zero (and reports the code) as soon as any stage
fails; exits 0 only when every stage passes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx

WEB_URL = os.environ.get("WEB_URL", "http://web:8080")
# In the image the project lives at /app; fall back to the repo root when
# verify.py is executed outside the container.
_APP_DIR = "/app" if os.path.isdir("/app") else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))


def run(title: str, cmd: list[str], env: dict | None = None) -> int:
    print(f"\n=== verify stage: {title} ===", flush=True)
    print(f"$ {' '.join(cmd)}", flush=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    rc = subprocess.run(cmd, cwd=_APP_DIR, env=full_env).returncode
    if rc != 0:
        print(f"!!! stage FAILED with exit code {rc}: {title}", flush=True)
    return rc


def wait_for_web(timeout: float = 60.0) -> bool:
    print(f"--- waiting for {WEB_URL}/health ...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{WEB_URL}/health", timeout=2.0)
            if r.status_code == 200:
                print("--- web is healthy", flush=True)
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    # 1. build check ---------------------------------------------------------
    rc = run("build check (compileall)",
             [sys.executable, "-m", "compileall", "-q", "app", "tests",
              "scripts"])
    if rc:
        return rc
    rc = run("build check (import app.main)",
             [sys.executable, "-c",
              "from app.main import app; print('app import ok:', app.title)"],
             env={"VALVE_DB_DIR": "/tmp/verify-import-db"})
    if rc:
        return rc

    # 2. code tests ----------------------------------------------------------
    rc = run("code tests (pytest: reverse compensation + revision fences "
             "+ restart receipt reconciliation)",
             [sys.executable, "-m", "pytest", "-q", "--tb=short", "tests"])
    if rc:
        return rc

    # 3. HTTP smoke against the running web service --------------------------
    if not wait_for_web():
        print("!!! web service did not become healthy", flush=True)
        return 1
    rc = run("HTTP smoke", [sys.executable, "scripts/smoke.py", WEB_URL])
    if rc:
        return rc

    print("\n=== VERIFY OK: build + tests + HTTP smoke all passed ===",
          flush=True)
    return 0


if __name__ == "__main__":
    code = main()
    print(f"verify exit code: {code}", flush=True)
    sys.exit(code)

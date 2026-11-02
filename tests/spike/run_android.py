"""Drive spike.html in Chrome on an adb-connected Android phone.

Python Playwright has no `_android` API, so this attaches to the phone's Chrome over
CDP (adb forward to chrome_devtools_remote). Needs USB debugging enabled and Chrome
installed; the phone reaches the local spike server via `adb reverse`.

Usage: python tests/spike/run_android.py [--share]
  SPIKE_CDN_URLS="<url> ..." adds real Discord CDN URLs (needed for --share, since
  the mock files are not real media).
  --share  taps "Share files" and leaves the share sheet open for you to pick Gallery/Photos.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
PAGE = "http://localhost:8080/spike.html"


def adb(*args: str) -> str:
    return subprocess.run(["adb", *args], check=True, capture_output=True, text=True).stdout


def downloads() -> set[str]:
    return set(adb("shell", "ls", "/sdcard/Download").split())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    devices = [line for line in adb("devices").splitlines()[1:] if line.strip().endswith("device")]
    if not devices:
        print("no adb device connected")
        return 2

    server = subprocess.Popen(
        [sys.executable, str(HERE / "serve.py")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for port in ("8080", "8081"):
            adb("reverse", f"tcp:{port}", f"tcp:{port}")
        adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")
        adb(
            "shell", "am", "start", "-a", "android.intent.action.VIEW", "-p", "com.android.chrome", "-d", PAGE
        )
        for _ in range(50):
            try:
                urllib.request.urlopen("http://localhost:9222/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.2)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            # Playwright redirects downloads to a host-side temp dir that doesn't exist on the
            # phone, so Chrome silently drops them; restore Chrome's own download handling.
            browser.new_browser_cdp_session().send("Browser.setDownloadBehavior", {"behavior": "default"})
            page = next(pg for ctx in browser.contexts for pg in ctx.pages if "spike.html" in pg.url)
            page.reload()
            page.wait_for_selector("#log:has-text('ready')")
            print(page.text_content("#env"))

            page.click("#mock")
            real = os.environ.get("SPIKE_CDN_URLS", "").split()
            if real:
                page.fill("#urls", page.input_value("#urls") + "\n" + "\n".join(real))

            before = downloads()
            page.click("#zip")
            page.wait_for_selector("#log:has-text('download triggered')", timeout=120_000)
            new: set[str] = set()
            for _ in range(60):
                new = downloads() - before
                if any(n.endswith(".zip") for n in new):
                    break
                time.sleep(1)
            print(f"new files in /sdcard/Download: {sorted(new) or 'NONE'}")

            if args.share:
                if not real:
                    print("--share needs SPIKE_CDN_URLS")
                else:
                    page.fill("#urls", "\n".join(real))
                    page.click("#share-files")
                    page.wait_for_selector(
                        "#log:has-text('calling navigator.share'), #log:has-text('tap Share files again')"
                    )
                    if "tap Share files again" in (page.text_content("#log") or ""):
                        page.click("#share-files")  # activation expired while fetching
                    print(
                        "share sheet should be open on the phone: choose Gallery/Photos, then check the log"
                    )
            print(page.text_content("#log"))
            shot = HERE / "android-last.png"
            shot.write_bytes(
                subprocess.run(["adb", "exec-out", "screencap", "-p"], check=True, capture_output=True).stdout
            )
            print(f"screenshot: {shot}")
        return 0 if any(n.endswith(".zip") for n in new) else 1
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())

"""Drive spike.html in desktop browsers and verify the downloaded ZIP.

Usage:
  python tests/spike/run_desktop.py                 # mock CDN only
  SPIKE_CDN_URLS="<url> <url>" python tests/spike/run_desktop.py   # + real Discord CDN URLs

Engines: Playwright Chromium/Firefox/WebKit plus installed Chrome/Edge if present.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

HERE = Path(__file__).parent
PAGE = "http://localhost:8080/spike.html"


def wait_for_server() -> None:
    for _ in range(50):
        try:
            urllib.request.urlopen(PAGE, timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("spike server did not start")


def run_one(p, label: str, launch, out_dir: Path) -> str:
    try:
        browser = launch()
    except Error as e:
        return f"{label}: SKIPPED ({str(e).splitlines()[0]})"
    try:
        page = browser.new_page(accept_downloads=True)
        violations: list[str] = []
        page.on(
            "console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None
        )
        page.add_init_script(
            "document.addEventListener('securitypolicyviolation',"
            " e => console.log('Content Security Policy violation: ' + e.violatedDirective + ' ' + e.blockedURI))"
        )
        page.goto(PAGE)
        page.click("#mock")
        real = os.environ.get("SPIKE_CDN_URLS", "").split()
        if real:
            page.fill("#urls", page.input_value("#urls") + "\n" + "\n".join(real))
        with page.expect_download(timeout=60_000) as dl:
            page.click("#zip")
        target = out_dir / f"{label}.zip"
        dl.value.save_as(target)
        with zipfile.ZipFile(target) as z:
            bad = z.testzip()
            names = z.namelist()
        env = page.text_content("#env") or ""
        share = [line for line in env.splitlines() if line.startswith("canShare")]
        status = "OK" if bad is None and not violations else "FAIL"
        return (
            f"{label}: {status} zip={target.stat().st_size}B entries={names} crc_bad={bad} "
            f"csp_violations={violations} {share}"
        )
    except Error as e:
        return f"{label}: FAIL {str(e).splitlines()[0]}"
    finally:
        browser.close()


def main() -> int:
    server = subprocess.Popen(
        [sys.executable, str(HERE / "serve.py")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_for_server()
        out_dir = Path(tempfile.mkdtemp(prefix="spike-"))
        with sync_playwright() as p:
            engines = {
                "chromium": lambda: p.chromium.launch(),
                "firefox": lambda: p.firefox.launch(),
                "webkit": lambda: p.webkit.launch(),
                "chrome": lambda: p.chromium.launch(channel="chrome"),
                "msedge": lambda: p.chromium.launch(channel="msedge"),
            }
            results = [run_one(p, label, launch, out_dir) for label, launch in engines.items()]
        print("\n".join(results))
        return 1 if any(": FAIL" in r for r in results) else 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())

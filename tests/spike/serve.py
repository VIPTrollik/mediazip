"""Phase 0 spike server.

:8080  serves this directory with the CSP planned for the real app (PLAN.md §6)
:8081  mock CDN: generated files with `Access-Control-Allow-Origin: *`, like Discord's CDN

Usage: python tests/spike/serve.py [--bind 127.0.0.1]
"""

from __future__ import annotations

import argparse
import struct
import threading
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
CDN_HOSTS = "https://cdn.discordapp.com https://media.discordapp.net"


def _png(width: int = 64, height: int = 64) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(
        b"\x00" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 160)) for x in range(width))
        for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


MOCK_FILES: dict[str, tuple[str, bytes]] = {
    "photo.png": ("image/png", _png()),
    "clip.mp4": ("video/mp4", b"\x00\x00\x00\x18ftypmp42" + bytes(200_000)),
    "big.bin": ("application/octet-stream", bytes(range(256)) * (40 * 1024 * 1024 // 256)),
}


class PageHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        host = (self.headers.get("Host") or "localhost").split(":")[0]
        mock = f"http://{host}:8081"
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; img-src 'self' blob: {CDN_HOSTS} {mock}; media-src 'self' blob: {CDN_HOSTS} {mock}; "
            f"connect-src 'self' {CDN_HOSTS} {mock}; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class MockCdnHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        name = self.path.split("?")[0].removeprefix("/files/")
        if name not in MOCK_FILES:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        ctype, body = MOCK_FILES[name]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    page = ThreadingHTTPServer((args.bind, 8080), partial(PageHandler, directory=str(HERE)))
    cdn = ThreadingHTTPServer((args.bind, 8081), MockCdnHandler)
    threading.Thread(target=cdn.serve_forever, daemon=True).start()
    print(f"spike: http://{args.bind}:8080/spike.html  (mock CDN on :8081)", flush=True)
    page.serve_forever()


if __name__ == "__main__":
    main()

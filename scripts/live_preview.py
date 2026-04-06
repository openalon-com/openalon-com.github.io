#!/usr/bin/env python3
"""Local static-site preview server with live reload."""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_EXTENSIONS = {
    ".html",
    ".css",
    ".js",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".txt",
    ".xml",
}

RELOAD_SNIPPET = """
<script>
(function () {
  var currentVersion = null;
  async function checkForUpdates() {
    try {
      var response = await fetch("/__live_reload__", { cache: "no-store" });
      if (!response.ok) return;
      var data = await response.json();
      if (currentVersion === null) {
        currentVersion = data.version;
        return;
      }
      if (data.version !== currentVersion) {
        window.location.reload();
      }
    } catch (error) {
      console.debug("live preview check failed", error);
    }
  }
  setInterval(checkForUpdates, 1000);
  checkForUpdates();
})();
</script>
""".strip()


class ChangeTracker:
    def __init__(self, root: Path, extensions: set[str], interval: float) -> None:
        self.root = root
        self.extensions = {ext.lower() for ext in extensions}
        self.interval = interval
        self.version = 0
        self._snapshot = self._build_snapshot()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            snapshot = self._build_snapshot()
            if snapshot != self._snapshot:
                self._snapshot = snapshot
                self.version += 1
                print(f"[live-preview] detected change, version={self.version}", flush=True)

    def _build_snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "__pycache__"} for part in path.parts):
                continue
            if self.extensions and path.suffix.lower() not in self.extensions:
                continue
            stat = path.stat()
            snapshot[str(path.relative_to(self.root))] = (stat.st_mtime_ns, stat.st_size)
        return snapshot


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class LiveReloadHandler(http.server.SimpleHTTPRequestHandler):
    tracker: ChangeTracker
    root: Path

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        safe_path = super().translate_path(parsed_path)
        return safe_path

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path).path
        if parsed_path == "/__live_reload__":
            payload = ('{"version": %d}\n' % self.tracker.version).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if "/.git/" in parsed_path or parsed_path.startswith("/.git"):
            self.send_error(404)
            return

        super().do_GET()

    def send_head(self):  # type: ignore[override]
        parsed_path = urlparse(self.path).path
        if parsed_path.endswith(".html") or parsed_path in {"", "/"}:
            return self._send_html_with_reload(parsed_path)
        return super().send_head()

    def _send_html_with_reload(self, parsed_path: str):
        relative_path = parsed_path.lstrip("/") or "index.html"
        file_path = self.root / relative_path
        if file_path.is_dir():
            file_path = file_path / "index.html"
        if not file_path.exists():
            return super().send_head()

        content = file_path.read_text(encoding="utf-8")
        if "/__live_reload__" not in content:
            if "</body>" in content:
                content = content.replace("</body>", f"{RELOAD_SNIPPET}\n</body>")
            else:
                content = f"{content}\n{RELOAD_SNIPPET}\n"
        encoded = content.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return io.BytesIO(encoded)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview this static site locally with auto reload."
    )
    parser.add_argument(
        "--root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Site root to serve. Defaults to the repository root.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind.")
    parser.add_argument(
        "--interval",
        default=0.75,
        type=float,
        help="How often to poll for file changes, in seconds.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    tracker = ChangeTracker(root=root, extensions=DEFAULT_EXTENSIONS, interval=args.interval)

    class Handler(LiveReloadHandler):
        pass

    Handler.tracker = tracker
    Handler.root = root

    server = ThreadingHTTPServer((args.host, args.port), functools.partial(Handler, directory=str(root)))
    tracker.start()

    print(f"[live-preview] serving {root}")
    print(f"[live-preview] open http://{args.host}:{args.port}/")
    print("[live-preview] watching for changes. Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[live-preview] shutting down")
    finally:
        server.server_close()
        tracker.stop()


if __name__ == "__main__":
    main()

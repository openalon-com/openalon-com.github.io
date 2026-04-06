#!/usr/bin/env python3
"""Download external static assets referenced by a mirrored site and rewrite them locally."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "Mozilla/5.0 (compatible; OpenAlonMirrorAssets/1.0; +https://openalon.com)"
ABSOLUTE_URL_RE = re.compile(r"https?://[^\s\"'()<>{}]+")
CSS_URL_RE = re.compile(r"url\((?P<quote>[\"']?)(?P<url>.+?)(?P=quote)\)")
ASSET_EXTENSIONS = {
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".webm",
    ".avif",
}
ALLOWED_HOSTS = {
    "cdn.prod.website-files.com",
    "d3e54v103j8qbb.cloudfront.net",
    "cdn.cookie-script.com",
    "report.cookie-script.com",
    "www.google-analytics.com",
}
MANIFEST_NAME = "asset-manifest.json"


def safe_local_asset_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path.endswith("/"):
        path += "index"
    relative = Path("__external__") / parsed.netloc / path.lstrip("/")
    if not relative.suffix:
        relative = relative.with_suffix(".bin")
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        relative = relative.with_name(f"{relative.stem}--q-{digest}{relative.suffix}")
    return relative


def relative_path(from_file: Path, to_file: Path) -> str:
    return os.path.relpath(to_file, start=from_file.parent).replace(os.sep, "/")


def should_download(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in ALLOWED_HOSTS:
        return True
    suffix = Path(parsed.path).suffix.lower()
    return suffix in ASSET_EXTENSIONS


def fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return response.read(), response.headers.get_content_type()


def find_initial_assets(root: Path) -> set[str]:
    discovered: set[str] = set()
    for file_path in root.rglob("*"):
        if file_path.is_dir():
            continue
        if "__external__" in file_path.parts:
            continue
        if file_path.suffix.lower() not in {".html", ".css"}:
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for match in ABSOLUTE_URL_RE.findall(text):
            url = match.rstrip(".,;")
            if should_download(url):
                discovered.add(url)
    return discovered


def load_manifest(root: Path) -> dict[str, Path]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    loaded: dict[str, Path] = {}
    for entry in payload.get("assets", []):
        url = entry.get("url")
        local_path = entry.get("local_path")
        if not url or not local_path:
            continue
        loaded[url] = Path(local_path)
    return loaded


def save_manifest(root: Path, asset_map: dict[str, Path]) -> None:
    manifest_path = root / MANIFEST_NAME
    payload = {
        "assets": [
            {"url": url, "local_path": str(path)}
            for url, path in sorted(asset_map.items())
        ]
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def expand_css_dependencies(url: str, css_text: str) -> set[str]:
    found: set[str] = set()
    for match in CSS_URL_RE.finditer(css_text):
        candidate = match.group("url").strip()
        if candidate.startswith(("data:", "#")):
            continue
        resolved = urljoin(url, candidate)
        if should_download(resolved):
            found.add(resolved)
    return found


def load_existing_content(full_path: Path) -> bytes | None:
    try:
        return full_path.read_bytes()
    except OSError:
        return None


def download_assets(root: Path, initial_urls: set[str], existing_map: dict[str, Path]) -> dict[str, Path]:
    asset_map: dict[str, Path] = dict(existing_map)
    pending = deque(sorted(initial_urls))
    seen: set[str] = set()

    while pending:
        url = pending.popleft()
        if url in seen:
            continue
        seen.add(url)

        local_path = asset_map.get(url, safe_local_asset_path(url))
        full_path = root / local_path
        content = load_existing_content(full_path)
        mime_type = ""

        if content is None:
            try:
                content, mime_type = fetch(url)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"[assets] failed {url}: {exc}", flush=True)
                continue

            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_bytes(content)
            print(f"[assets] {url} -> {local_path}", flush=True)
        else:
            print(f"[assets] reuse {url} -> {local_path}", flush=True)

        asset_map[url] = local_path
        if mime_type.startswith("text/css") or local_path.suffix.lower() == ".css":
            css_text = content.decode("utf-8", errors="ignore")
            for child_url in sorted(expand_css_dependencies(url, css_text)):
                if child_url not in seen:
                    pending.append(child_url)

    return asset_map


def rewrite_file(file_path: Path, asset_map: dict[str, Path]) -> bool:
    if file_path.suffix.lower() not in {".html", ".css"}:
        return False
    original = file_path.read_text(encoding="utf-8", errors="ignore")
    rewritten = original
    for remote_url, local_path in asset_map.items():
        rewritten = rewritten.replace(remote_url, relative_path(file_path, file_path.parents[0] / local_path))
    if rewritten != original:
        file_path.write_text(rewritten, encoding="utf-8")
        return True
    return False


def rewrite_all(root: Path, asset_map: dict[str, Path]) -> int:
    changed = 0
    for file_path in root.rglob("*"):
        if file_path.is_dir():
            continue
        if rewrite_file(file_path, asset_map):
            changed += 1
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Localize external static assets inside a mirrored site.")
    parser.add_argument("root", type=Path, help="Mirror root directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.expanduser().resolve()
    initial_urls = find_initial_assets(root)
    print(f"[assets] found {len(initial_urls)} candidate asset urls", flush=True)
    existing_map = load_manifest(root)
    if existing_map:
        print(f"[assets] loaded {len(existing_map)} cached asset mappings", flush=True)
    asset_map = download_assets(root, initial_urls, existing_map)
    changed = rewrite_all(root, asset_map)
    save_manifest(root, asset_map)
    print(f"[assets] downloaded {len(asset_map)} assets and rewrote {changed} files", flush=True)


if __name__ == "__main__":
    main()

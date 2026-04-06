#!/usr/bin/env python3
"""Mirror a static website for local inspection and preview."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import posixpath
import queue
import re
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


USER_AGENT = "Mozilla/5.0 (compatible; OpenAlonMirror/1.0; +https://openalon.com)"
HTML_TYPES = ("text/html", "application/xhtml+xml")
CSS_TYPES = ("text/css",)
ASSET_ATTRS = {"src", "href", "srcset", "poster", "data-src", "data-href"}
SKIP_SCHEMES = {"mailto", "tel", "javascript", "data"}
CSS_URL_RE = re.compile(r"url\((?P<quote>[\"']?)(?P<url>.+?)(?P=quote)\)")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = posixpath.normpath(path)
        if not path.startswith("/"):
            path = "/" + path
    query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}"
        for k, v in sorted(parse_qsl(parsed.query, keep_blank_values=True))
    )
    return urlunparse((scheme, netloc, path, "", query, ""))


def ensure_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def safe_name(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return value or "index"


def local_path_for_url(parsed, mime_type: str | None) -> Path:
    path = unquote(parsed.path or "/")
    if path.endswith("/"):
        path = path + "index.html"
    elif path == "/":
        path = "/index.html"

    suffix = Path(path).suffix.lower()
    if not suffix:
        if mime_type and any(mime_type.startswith(item) for item in HTML_TYPES):
            path = path.rstrip("/") + ".html"
        else:
            path = path.rstrip("/") + "/index"

    relative = Path(path.lstrip("/"))

    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        stem = relative.stem or "index"
        suffix = relative.suffix or ""
        relative = relative.with_name(f"{stem}--q-{digest}{suffix}")

    return relative


def relative_link(from_path: Path, to_path: Path) -> str:
    rel = os.path.relpath(to_path, start=from_path.parent)
    return rel.replace(os.sep, "/")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if not value or name not in ASSET_ATTRS:
                continue
            if name == "srcset":
                parts = [item.strip().split()[0] for item in value.split(",")]
                self.links.extend(part for part in parts if part)
            else:
                self.links.append(value.strip())


@dataclass
class DownloadedFile:
    url: str
    local_path: Path
    mime_type: str
    status: int


class SiteMirror:
    def __init__(
        self,
        start_url: str,
        destination: Path,
        max_workers: int = 8,
        same_origin_only: bool = True,
    ) -> None:
        self.start_url = canonicalize_url(start_url)
        self.destination = destination.expanduser().resolve()
        self.max_workers = max_workers
        self.same_origin_only = same_origin_only
        self.origin = urlparse(self.start_url).netloc.lower()
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        self.results: list[DownloadedFile] = []
        self.failures: dict[str, str] = {}
        self.asset_map: dict[str, Path] = {}

    def should_visit(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme.lower() in SKIP_SCHEMES:
            return False
        if not parsed.netloc:
            return False
        if self.same_origin_only and parsed.netloc.lower() != self.origin:
            return False
        return True

    def fetch(self, url: str) -> tuple[bytes, str]:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=30) as response:
            data = response.read()
            mime_type = response.headers.get_content_type()
            return data, mime_type

    def rewrite_html(self, html: str, page_url: str, page_path: Path) -> tuple[str, list[str]]:
        parser = LinkParser()
        parser.feed(html)
        discovered: list[str] = []

        def replace_value(value: str) -> str:
            raw = value.strip()
            if not raw:
                return value
            if raw.startswith("#"):
                return value
            if raw.startswith(("mailto:", "tel:", "javascript:", "data:")):
                return value

            urls: list[str] = []
            if "," in raw and any(part.strip().split()[0] for part in raw.split(",")):
                is_srcset = True
                parts = [part.strip() for part in raw.split(",")]
                rewritten_parts = []
                for part in parts:
                    if not part:
                        continue
                    tokens = part.split()
                    candidate = tokens[0]
                    absolute = canonicalize_url(urljoin(page_url, candidate))
                    urls.append(absolute)
                    rewritten = self.rewrite_reference(absolute, page_path)
                    rewritten_parts.append(" ".join([rewritten, *tokens[1:]]).strip())
                discovered.extend(urls)
                return ", ".join(rewritten_parts) if rewritten_parts else value

            absolute = canonicalize_url(urljoin(page_url, raw))
            discovered.append(absolute)
            return self.rewrite_reference(absolute, page_path)

        def attr_sub(match: re.Match[str]) -> str:
            attr = match.group("attr")
            quote_char = match.group("quote")
            original = match.group("value")
            return f'{attr}={quote_char}{replace_value(original)}{quote_char}'

        rewritten = re.sub(
            r'(?P<attr>href|src|poster|data-src|data-href)=(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
            attr_sub,
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        def srcset_sub(match: re.Match[str]) -> str:
            quote_char = match.group("quote")
            original = match.group("value")
            return f'srcset={quote_char}{replace_value(original)}{quote_char}'

        rewritten = re.sub(
            r'srcset=(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
            srcset_sub,
            rewritten,
            flags=re.IGNORECASE | re.DOTALL,
        )

        rewritten = self.inject_base_marker(rewritten)
        return rewritten, discovered

    def rewrite_css(self, css: str, css_url: str, css_path: Path) -> tuple[str, list[str]]:
        discovered: list[str] = []

        def repl(match: re.Match[str]) -> str:
            original = match.group("url").strip()
            if original.startswith(("data:", "#")):
                return match.group(0)
            absolute = canonicalize_url(urljoin(css_url, original))
            discovered.append(absolute)
            rewritten = self.rewrite_reference(absolute, css_path)
            quote_char = match.group("quote") or ""
            return f"url({quote_char}{rewritten}{quote_char})"

        return CSS_URL_RE.sub(repl, css), discovered

    def inject_base_marker(self, html: str) -> str:
        marker = '<meta name="x-openalon-mirror" content="true" />'
        if marker in html:
            return html
        if "<head" in html and "</head>" in html:
            return html.replace("</head>", f"  {marker}\n</head>", 1)
        return html

    def rewrite_reference(self, absolute_url: str, current_path: Path) -> str:
        if not self.should_visit(absolute_url):
            return absolute_url
        parsed = urlparse(absolute_url)
        known_path = self.asset_map.get(absolute_url)
        if known_path is None:
            guessed = local_path_for_url(parsed, None)
            known_path = guessed
        return relative_link(current_path, known_path)

    def save_file(self, local_path: Path, content: bytes) -> None:
        full_path = self.destination / local_path
        ensure_directory(full_path)
        full_path.write_bytes(content)

    def record(self, url: str, local_path: Path, mime_type: str, status: int = 200) -> None:
        with self.lock:
            self.asset_map[url] = local_path
            self.results.append(DownloadedFile(url, local_path, mime_type, status))

    def mark_failure(self, url: str, error: Exception) -> None:
        with self.lock:
            self.failures[url] = str(error)

    def crawl(self) -> None:
        pending: queue.Queue[str] = queue.Queue()
        pending.put(self.start_url)

        while not pending.empty():
            batch: list[str] = []
            while not pending.empty() and len(batch) < self.max_workers * 4:
                url = pending.get()
                with self.lock:
                    if url in self.seen:
                        continue
                    self.seen.add(url)
                batch.append(url)

            if not batch:
                continue

            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_map = {executor.submit(self.process_url, url): url for url in batch}
                for future in concurrent.futures.as_completed(future_map):
                    url = future_map[future]
                    try:
                        discovered = future.result()
                    except Exception as exc:  # pragma: no cover
                        self.mark_failure(url, exc)
                        continue
                    for discovered_url in discovered:
                        canonical = canonicalize_url(discovered_url)
                        if not self.should_visit(canonical):
                            continue
                        with self.lock:
                            if canonical in self.seen:
                                continue
                        pending.put(canonical)

        self.write_manifest()

    def process_url(self, url: str) -> list[str]:
        try:
            data, mime_type = self.fetch(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            self.mark_failure(url, exc)
            return []

        parsed = urlparse(url)
        local_path = local_path_for_url(parsed, mime_type)
        discovered: list[str] = []

        if any(mime_type.startswith(item) for item in HTML_TYPES):
            text = data.decode("utf-8", errors="ignore")
            text, discovered = self.rewrite_html(text, url, local_path)
            data = text.encode("utf-8")
        elif any(mime_type.startswith(item) for item in CSS_TYPES):
            text = data.decode("utf-8", errors="ignore")
            text, discovered = self.rewrite_css(text, url, local_path)
            data = text.encode("utf-8")

        self.save_file(local_path, data)
        self.record(url, local_path, mime_type)
        print(f"[mirror] {url} -> {local_path}", flush=True)
        return discovered

    def write_manifest(self) -> None:
        manifest = {
            "start_url": self.start_url,
            "destination": str(self.destination),
            "downloaded": [
                {
                    "url": item.url,
                    "local_path": str(item.local_path),
                    "mime_type": item.mime_type,
                    "status": item.status,
                }
                for item in sorted(self.results, key=lambda item: item.url)
            ],
            "failures": self.failures,
            "generated_at": int(time.time()),
        }
        manifest_path = self.destination / "mirror-manifest.json"
        ensure_directory(manifest_path)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror a public website locally.")
    parser.add_argument("url", help="Start URL, for example https://www.example.com/")
    parser.add_argument("destination", type=Path, help="Directory to write mirrored files into.")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent download workers.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mirror = SiteMirror(args.url, args.destination, max_workers=args.workers)
    mirror.destination.mkdir(parents=True, exist_ok=True)
    mirror.crawl()
    print(
        f"[mirror] completed: {len(mirror.results)} files, {len(mirror.failures)} failures, output={mirror.destination}",
        flush=True,
    )


if __name__ == "__main__":
    main()

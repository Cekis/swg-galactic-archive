"""Resolve archive evidence against pinned public files or the indexed checkout."""

import gzip
from html import escape
import json
from pathlib import PurePosixPath
import re
import subprocess
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from catalog_sources import PROJECT, ROOT


UPSTREAM_INDEX = PROJECT / "upstream_source_index.json.gz"
HASH = re.compile(r"[0-9a-f]{40}")
PUBLISHED_REPOSITORY = "SWG-Source/whitengold"
SOURCE_ROOTS = {"dsrc", "src", "client-tools", "clientdata"}
TEXT_TYPES = {".tab", ".tpf", ".txt", ".csv", ".java", ".script", ".scriptlib",
              ".cpp", ".c", ".h", ".md", ".cfg", ".xml", ".py"}


def valid_source(path):
    parts = path.split("/")
    return (len(parts) > 1 and parts[0] in SOURCE_ROOTS
            and all(part and part not in {".", ".."} for part in parts)
            and not re.search(r"[:\\\x00-\x1f\x7f]", path))


class SourceCatalog:
    def __init__(self, revision, index=UPSTREAM_INDEX):
        self.revision = revision
        self.local_text = index.with_name("local_source_text.zip")
        self.published_revision = None
        self.published_paths = set()
        self.matching_paths = set()
        try:
            with gzip.open(index, "rt", encoding="utf-8") as stream:
                published = json.load(stream)
        except (OSError, ValueError):
            return
        if (published.get("repository") == PUBLISHED_REPOSITORY
                and HASH.fullmatch(published.get("revision") or "")):
            self.published_revision = published["revision"]
            self.published_paths = set(published.get("paths", ()))
            if published.get("snapshot_revision") == revision:
                self.matching_paths = set(published.get("matching_paths", ()))

    def github(self, path, line=None):
        if path not in self.published_paths or not valid_source(path):
            return None
        anchor = f"#L{line}" if line and path in self.matching_paths and PurePosixPath(path).suffix.lower() in TEXT_TYPES else ""
        return f"https://github.com/{PUBLISHED_REPOSITORY}/blob/{self.published_revision}/{quote(path)}{anchor}"

    def excerpt(self, path, line=None):
        if (not valid_source(path) or not path.startswith(("dsrc/sku.0/", "src/"))
                or PurePosixPath(path).suffix.lower() not in TEXT_TYPES or not HASH.fullmatch(self.revision)):
            return ()
        source = self._source_text(path)
        if source is None:
            return ()
        lines = source.decode("utf-8-sig", errors="replace").splitlines()
        if not lines:
            return ()
        center = min(line or 1, len(lines))
        start, stop = max(1, center - 10), min(center + 10, len(lines))
        return tuple((number, lines[number - 1]) for number in range(start, stop + 1))

    def _source_text(self, path):
        if self.local_text.is_file():
            try:
                with ZipFile(self.local_text) as archive:
                    if archive.comment == self.revision.encode("ascii"):
                        return archive.read(path)
            except (OSError, BadZipFile, KeyError):
                pass
            return None
        try:
            result = subprocess.run(["git", "cat-file", "blob", f"{self.revision}:{path}"], cwd=ROOT,
                                    capture_output=True, timeout=8, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout

    def local_page(self, path, line=None):
        label = path + (f":{line}" if line else "")
        excerpt = self.excerpt(path, line)
        if excerpt:
            content = '<pre class="source-excerpt">' + "\n".join(
                f'<span class="source-line{" selected" if number == line else ""}">'
                f'<span class="source-line-number">{number:>5}</span>  {escape(text)}</span>' for number, text in excerpt
            ) + "</pre>"
        else:
            content = '<p class="muted">Local archive reference. A text excerpt is unavailable for this record.</p>'
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>Local source · Galactic Archive</title><link rel="stylesheet" href="/site.css"></head>'
                f'<body><main class="app-shell"><section class="panel"><h1>Local source</h1>'
                f'<p><code>{escape(label)}</code></p><p class="muted">Game source {escape(self.revision[:9])}</p>'
                f'{content}</section></main></body></html>').encode("utf-8")
